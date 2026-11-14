import { expect, it } from "vitest";
import { mkdtemp, rm, symlink, mkdir } from "node:fs/promises";
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
