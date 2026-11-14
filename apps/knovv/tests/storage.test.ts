import { describe, it, expect } from "vitest";
import { MemoryStorage } from "../src/storage";
describe("bounded encrypted device cache", () => {
  it("removes only the draft revision that was actually synchronized", async () => {
    const cache = new MemoryStorage();
    const key = "draft:fixture";
    await cache.set(key, { envelope: { id: "newer" }, cipher: "encrypted" });
    await cache.removeDraft(key, "older");
    expect(await cache.get(key)).toBeDefined();
    await cache.removeDraft(key, "newer");
    expect(await cache.get(key)).toBeUndefined();
  });
  it("evicts older ciphertext without evicting protected credentials or pending drafts", async () => {
    const cache = new MemoryStorage();
    await cache.set("device-package", { credential: "protected fixture" });
    await cache.set("draft:fixture", { cipher: "encrypted fixture" });
    await cache.putCipher("a".repeat(64), new Uint8Array(20 * 1024 * 1024));
    await cache.putCipher("b".repeat(64), new Uint8Array(20 * 1024 * 1024));
    expect(await cache.get("object:" + "a".repeat(64))).toBeUndefined();
    expect(
      (await cache.get<Uint8Array>("object:" + "b".repeat(64)))?.length,
    ).toBe(20 * 1024 * 1024);
    expect(await cache.get("device-package")).toBeDefined();
    expect(await cache.get("draft:fixture")).toBeDefined();
  });
});
