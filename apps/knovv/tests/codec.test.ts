import { describe, it, expect } from "vitest";
import { compress, inflate, PLAIN_LIMIT } from "../src/vault";
describe("plaintext writer/reader bounds", () => {
  it("rejects one plaintext byte beyond the reader boundary before encryption", async () => {
    await expect(
      compress(new Uint8Array(PLAIN_LIMIT + 1).fill(65)),
    ).rejects.toThrow("plaintext-too-large");
  });
  it("round trips the full plaintext reader boundary", async () => {
    const bytes = new Uint8Array(PLAIN_LIMIT).fill(65);
    expect((await inflate(await compress(bytes))).length).toBe(PLAIN_LIMIT);
  });
});
