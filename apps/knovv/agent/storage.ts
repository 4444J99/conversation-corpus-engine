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
// Every value is protected credential material, ciphertext or opaque checkpoint metadata.
// The process-wide exclusive lock prevents simultaneous cache/draft writers.
export class FileStorage implements StoragePort {
  private constructor(private root: string) {}
  static async open(root: string) {
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
    await privateOutput(join(root, ".lock"), { pid: process.pid });
    return new FileStorage(root);
  }
  async close() {
    await fs.unlink(join(this.root, ".lock"));
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
