import { expect, it } from "vitest";
import {
  mkdtemp,
  rm,
  symlink,
  mkdir,
  writeFile,
  readFile,
  unlink,
  utimes,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { FileStorage } from "../agent/storage";

it("reopens encrypted drafts and checkpoints without a competing state writer", async () => {
  const root = await mkdtemp(join(tmpdir(), "knovv-agent-test-"));
  try {
    const dir = join(root, "device");
    let store = await FileStorage.open(dir);
    await store.set("draft:opaque-vault:opaque-doc", {
      envelope: { id: "opaque-revision" },
      cipher: new Uint8Array([1, 2, 3]),
    });
    await expect(FileStorage.open(dir)).rejects.toMatchObject({
      code: "EEXIST",
    });
    await store.removeDraft("draft:opaque-vault:opaque-doc", "older-revision");
    await store.close();
    store = await FileStorage.open(dir);
    expect(
      (await store.get<any>("draft:opaque-vault:opaque-doc")).cipher,
    ).toEqual(new Uint8Array([1, 2, 3]));
    expect(await store.keys("draft:")).toEqual([
      "draft:opaque-vault:opaque-doc",
    ]);
    await store.putCipher("a".repeat(64), new Uint8Array(17 * 1024 * 1024));
    await store.putCipher("b".repeat(64), new Uint8Array(17 * 1024 * 1024));
    expect(await store.get("object:" + "a".repeat(64))).toBeUndefined();
    expect(
      (await store.get<Uint8Array>("object:" + "b".repeat(64)))!.length,
    ).toBe(17 * 1024 * 1024);
    expect(await store.get("draft:opaque-vault:opaque-doc")).toBeDefined();
    await store.removeDraft("draft:opaque-vault:opaque-doc", "opaque-revision");
    expect(await store.keys("draft:")).toEqual([]);
    await store.close();
    const unowned = join(root, "unowned");
    await mkdir(unowned, { mode: 0o700 });
    await expect(FileStorage.open(unowned)).rejects.toBeDefined();
    await symlink(dir, join(root, "alias"));
    await expect(FileStorage.open(join(root, "alias"))).rejects.toThrow(
      "symlink-refused",
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}, 30000);

it("reclaims a lock left by a dead process but never one held by a live process", async () => {
  const root = await mkdtemp(join(tmpdir(), "knovv-agent-lock-"));
  try {
    const dir = join(root, "device");
    await (await FileStorage.open(dir)).close();
    await writeFile(join(dir, ".lock"), JSON.stringify({ pid: 2 ** 22 + 1 }), {
      mode: 0o600,
    });
    const store = await FileStorage.open(dir);
    await expect(FileStorage.open(dir)).rejects.toMatchObject({
      code: "EEXIST",
    });
    await store.close();
    await writeFile(join(dir, ".lock"), "not json", { mode: 0o600 });
    await expect(FileStorage.open(dir)).rejects.toMatchObject({
      code: "EEXIST",
    });
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}, 30000);

it("close never removes a lock now held by another owner", async () => {
  const root = await mkdtemp(join(tmpdir(), "knovv-agent-close-"));
  try {
    const dir = join(root, "device");
    const store = await FileStorage.open(dir);
    await unlink(join(dir, ".lock"));
    const foreign = JSON.stringify({ pid: process.pid, token: "someone-else" }); // allow-secret: test lock owner id
    await writeFile(join(dir, ".lock"), foreign, { mode: 0o600 });
    await store.close();
    expect(await readFile(join(dir, ".lock"), "utf8")).toBe(foreign);
    await expect(FileStorage.open(dir)).rejects.toMatchObject({
      code: "EEXIST",
    });
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}, 30000);

it("lets exactly one of many racing openers reclaim a dead owner's lock", async () => {
  const root = await mkdtemp(join(tmpdir(), "knovv-agent-race-"));
  try {
    for (let round = 0; round < 25; round++) {
      const dir = join(root, "device" + round);
      await (await FileStorage.open(dir)).close();
      await writeFile(
        join(dir, ".lock"),
        JSON.stringify({ pid: 2 ** 22 + 1 }),
        { mode: 0o600 },
      );
      const results = await Promise.allSettled([
        FileStorage.open(dir),
        FileStorage.open(dir),
        FileStorage.open(dir),
      ]);
      const won = results.filter((r) => r.status === "fulfilled");
      expect(won.length).toBe(1);
      for (const r of results)
        if (r.status === "rejected")
          expect((r.reason as any).code).toBe("EEXIST");
      await (won[0] as PromiseFulfilledResult<FileStorage>).value.close();
    }
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}, 60000);

it("reclaims past a stale empty guard left by a crash but fails closed on a fresh one", async () => {
  const root = await mkdtemp(join(tmpdir(), "knovv-agent-guard-"));
  try {
    const dir = join(root, "device");
    await (await FileStorage.open(dir)).close();
    const deadLock = JSON.stringify({ pid: 2 ** 22 + 1 });
    await writeFile(join(dir, ".lock"), deadLock, { mode: 0o600 });
    // A guard another opener has only just created is empty for an instant: leave it alone.
    await writeFile(join(dir, ".lock.reclaim"), "", { mode: 0o600 });
    await expect(FileStorage.open(dir)).rejects.toMatchObject({
      code: "EEXIST",
    });
    expect(await readFile(join(dir, ".lock.reclaim"), "utf8")).toBe("");
    // A guard left empty by a crash long ago is stale.
    const old = new Date(Date.now() - 10 * 60 * 1000);
    await utimes(join(dir, ".lock.reclaim"), old, old);
    const store = await FileStorage.open(dir);
    await expect(readFile(join(dir, ".lock.reclaim"))).rejects.toMatchObject({
      code: "ENOENT",
    });
    await store.close();
    // An old guard that names a live process is never stolen.
    await writeFile(join(dir, ".lock"), deadLock, { mode: 0o600 });
    await writeFile(
      join(dir, ".lock.reclaim"),
      JSON.stringify({ pid: process.pid }),
      { mode: 0o600 },
    );
    await utimes(join(dir, ".lock.reclaim"), old, old);
    await expect(FileStorage.open(dir)).rejects.toMatchObject({
      code: "EEXIST",
    });
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}, 30000);
