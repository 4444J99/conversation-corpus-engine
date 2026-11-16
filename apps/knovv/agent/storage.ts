import * as fs from "node:fs/promises";
import { constants } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { randomUUID } from "node:crypto";
import type { StoragePort } from "../src/storage";
import { sha256, encoder } from "../src/crypto";

export async function noLinks(path: string) {
  let current = resolve(path);
  while (true) {
    const stat = await fs.lstat(current).catch((error) => {
      if (error.code === "ENOENT") return undefined;
      throw error;
    });
    if (stat?.isSymbolicLink()) throw new Error("symlink-refused");
    if (current === dirname(current)) break;
    current = dirname(current);
  }
}
export async function privateRead(path: string, limit = 32 * 1024 * 1024) {
  await noLinks(path);
  const handle = await fs.open(
    path,
    constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK,
  );
  try {
    const stat = await handle.stat();
    if (
      !stat.isFile() ||
      stat.uid !== process.getuid!() ||
      stat.mode & 0o077 ||
      stat.size > limit
    )
      throw new Error("private-file-permissions-or-bound");
    return await handle.readFile();
  } finally {
    await handle.close();
  }
}
export async function privateOutput(path: string, value: unknown) {
  await noLinks(path);
  const handle = await fs.open(path, "wx", 0o600);
  try {
    await handle.writeFile(JSON.stringify(value));
    await handle.sync();
  } finally {
    await handle.close();
  }
}
// A guard is held for microseconds; one this old with no readable owner is a crash remnant.
const STALE_GUARD_MS = 60_000;
// Remove a reclaim guard only when it is provably abandoned: its owner is dead, or it is an
// unreadable (for example empty, crashed between create and write) private file of ours that has
// not changed for STALE_GUARD_MS. The identity is re-checked just before the unlink so a guard
// another opener created in between is left alone; the remaining window is a stat-to-unlink gap.
async function removeAbandonedGuard(guard: string): Promise<boolean> {
  const before = await fs.lstat(guard).catch(() => undefined);
  if (!before?.isFile() || before.uid !== process.getuid!()) return false;
  const holder = await lockOwner(guard);
  const abandoned =
    holder === undefined
      ? Date.now() - before.mtimeMs > STALE_GUARD_MS
      : dead(holder);
  if (!abandoned) return false;
  const now = await fs.lstat(guard).catch(() => undefined);
  if (!now || now.ino !== before.ino || now.mtimeMs !== before.mtimeMs)
    return false;
  await fs.unlink(guard).catch(() => undefined);
  return true;
}
async function lockOwner(path: string): Promise<number | undefined> {
  let pid: unknown;
  try {
    pid = JSON.parse((await privateRead(path, 1024)).toString()).pid;
  } catch {
    return undefined;
  }
  return Number.isSafeInteger(pid) && (pid as number) > 0
    ? (pid as number)
    : undefined;
}
function dead(pid: number) {
  try {
    process.kill(pid, 0);
    return false;
  } catch (error: any) {
    return error.code === "ESRCH";
  }
}
// Every value is protected credential material, ciphertext or opaque checkpoint metadata.
// The process-wide exclusive lock prevents simultaneous cache/draft writers.
export class FileStorage implements StoragePort {
  private constructor(
    private root: string,
    private token: string, // allow-secret: runtime-only lock owner id
  ) {}
  static async open(root: string, retried = false): Promise<FileStorage> {
    await noLinks(root);
    try {
      await fs.mkdir(root, { mode: 0o700 });
      await privateOutput(join(root, ".knovv-agent.json"), {
        schema: "knovv-agent-state.v1",
      });
    } catch (error: any) {
      if (error.code !== "EEXIST") throw error;
    }
    const stat = await fs.lstat(root);
    if (
      !stat.isDirectory() ||
      stat.uid !== process.getuid!() ||
      stat.mode & 0o077
    )
      throw new Error("private-state-directory-required");
    const marker = JSON.parse(
      (await privateRead(join(root, ".knovv-agent.json"), 1024)).toString(),
    );
    if (marker.schema !== "knovv-agent-state.v1")
      throw new Error("unowned-agent-state");
    const lock = join(root, ".lock"),
      token = randomUUID(); // allow-secret: runtime-only lock owner id
    try {
      await privateOutput(lock, { pid: process.pid, token });
    } catch (error: any) {
      if (error.code !== "EEXIST") throw error;
      const owner = await lockOwner(lock);
      if (owner === undefined || !dead(owner)) throw error;
      // Reclaiming is serialized by an exclusive guard so two openers cannot both
      // unlink the lock and each delete the other's freshly created one.
      const guard = join(root, ".lock.reclaim");
      try {
        await privateOutput(guard, { pid: process.pid });
      } catch (guardError: any) {
        // Clear an abandoned guard, then try once more; a live or fresh guard fails closed.
        if (
          guardError.code === "EEXIST" &&
          (await removeAbandonedGuard(guard)) &&
          !retried
        )
          return FileStorage.open(root, true);
        throw error;
      }
      try {
        // Re-validate under the guard: the lock must still be the dead owner's.
        if ((await lockOwner(lock)) !== owner) throw error;
        await fs.unlink(lock);
        await privateOutput(lock, { pid: process.pid, token });
      } finally {
        await fs.unlink(guard).catch(() => undefined);
      }
    }
    return new FileStorage(root, token);
  }
  async close() {
    const lock = join(this.root, ".lock");
    let held: unknown;
    try {
      held = JSON.parse((await privateRead(lock, 1024)).toString()).token;
    } catch (error: any) {
      if (error.code === "ENOENT") return;
      throw error;
    }
    // Only the owner that wrote this token may release the lock.
    if (held === this.token) await fs.unlink(lock);
  }
  private path(key: string) {
    return join(this.root, sha256(encoder.encode(key)) + ".entry");
  }
  async get<T>(key: string): Promise<T | undefined> {
    let raw: Buffer;
    try {
      raw = await privateRead(this.path(key), 96 * 1024 * 1024);
    } catch (error: any) {
      if (error.code === "ENOENT") return undefined;
      throw error;
    }
    const split = raw.indexOf(10);
    if (split < 0 || JSON.parse(raw.subarray(0, split).toString()) !== key)
      throw new Error("invalid-agent-state");
    return JSON.parse(raw.subarray(split + 1).toString(), (_key, value) =>
      value &&
      typeof value === "object" &&
      Object.keys(value).length === 1 &&
      typeof value.$knovv_bytes === "string"
        ? new Uint8Array(Buffer.from(value.$knovv_bytes, "base64"))
        : value,
    );
  }
  async set(key: string, value: unknown) {
    const path = this.path(key),
      temporary = join(this.root, "." + randomUUID());
    const raw =
      JSON.stringify(key) +
      "\n" +
      JSON.stringify(value, (_key, value) =>
        value instanceof Uint8Array
          ? { $knovv_bytes: Buffer.from(value).toString("base64") }
          : value,
      );
    const handle = await fs.open(temporary, "wx", 0o600);
    try {
      await handle.writeFile(raw);
      await handle.sync();
    } finally {
      await handle.close();
    }
    await fs.rename(temporary, path);
  }
  async remove(key: string) {
    await fs.unlink(this.path(key)).catch((error) => {
      if (error.code !== "ENOENT") throw error;
    });
  }
  async removeDraft(key: string, revisionId: string) {
    if ((await this.get<any>(key))?.envelope?.id === revisionId)
      await this.remove(key);
  }
  async keys(prefix: string) {
    const keys: string[] = [];
    for (const name of await fs.readdir(this.root)) {
      if (!/^[0-9a-f]{64}\.entry$/.test(name)) continue;
      await noLinks(join(this.root, name));
      const handle = await fs.open(join(this.root, name), "r");
      try {
        const { buffer, bytesRead } = await handle.read(
          Buffer.alloc(1024),
          0,
          1024,
          0,
        );
        const line = buffer.subarray(0, bytesRead).toString().split("\n")[0];
        const key = JSON.parse(line);
        if (typeof key === "string" && key.startsWith(prefix)) keys.push(key);
      } finally {
        await handle.close();
      }
    }
    return keys;
  }
  private writes = Promise.resolve();
  async putCipher(id: string, bytes: Uint8Array) {
    const action = this.writes
      .catch(() => undefined)
      .then(async () => {
        const limit = 32 * 1024 * 1024;
        if (bytes.length > limit) return;
        const order = (
          (await this.get<{ id: string; size: number }[]>("cache-order")) ?? []
        ).filter((e) => e.id !== id);
        order.push({ id, size: bytes.length });
        let total = order.reduce((n, e) => n + e.size, 0);
        while (total > limit) {
          const old = order.shift()!;
          total -= old.size;
          await this.remove("object:" + old.id);
        }
        await this.set("object:" + id, bytes);
        await this.set("cache-order", order);
      });
    this.writes = action;
    await action;
  }
}
