import { describe, it, expect } from "vitest";
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import {
  generateDevice,
  protectDevice,
  unlockDevice,
  encrypt,
  decrypt,
  canonical,
  sign,
  verify,
  sha256,
  hex,
  unhex,
} from "../src/crypto";

describe("device custody and encrypted revision primitives", () => {
  it("encrypts multi-megabyte source material without exceeding the random generator request bound", () => {
    const data = new Uint8Array(1024 * 1024).fill(7),
      key = new Uint8Array(32).fill(8);
    expect(
      sha256(decrypt(key, encrypt(key, data, "large-source"), "large-source")),
    ).toBe(sha256(data));
  });
  it("decrypts Python CCE output and lets Python unlock browser-protected device credentials", async () => {
    const d = generateDevice("knovv", "interop"),
      credential = await protectDevice(
        d,
        "synthetic interoperability passphrase",
      );
    const input = {
      credential,
      passphrase: "synthetic interoperability passphrase",
      cipher: hex(
        encrypt(
          unhex(d.encryption_key),
          new TextEncoder().encode("from browser"),
          "interop",
        ),
      ),
    };
    const script = `import json,sys\nfrom conversation_corpus_engine.archive_sealed_credentials import unlock_identity\nfrom conversation_corpus_engine.archive_sealed_crypto import encrypt,decrypt,canonical\np=json.load(sys.stdin)\ni=unlock_identity(canonical(p['credential']),p['passphrase'].encode())\nassert decrypt(i.encryption_key,bytes.fromhex(p['cipher']),'interop')==b'from browser'\nprint(encrypt(i.encryption_key,b'from Python','interop').hex())`;
    const cipher = execFileSync(
      process.env.KNOVV_PYTHON ?? resolve("../../.venv/bin/python"),
      ["-c", script],
      { input: JSON.stringify(input), encoding: "utf8", timeout: 30000 },
    ).trim();
    expect(
      new TextDecoder().decode(
        decrypt(unhex(d.encryption_key), unhex(cipher), "interop"),
      ),
    ).toBe("from Python");
  }, 40000);
  it("unlocks only with the exact passphrase and authenticates ciphertext and context", async () => {
    const device = generateDevice("knovv", "fixture-device");
    const protectedKey = await protectDevice(
      device,
      "test-only correct horse battery",
    );
    const recovered = await unlockDevice(
      protectedKey,
      "test-only correct horse battery",
    );
    expect(recovered).toEqual(device);
    await expect(
      unlockDevice(protectedKey, "wrong passphrase"),
    ).rejects.toThrow();
    const message = new TextEncoder().encode("private test idea");
    const secret = unhex(device.encryption_key); // allow-secret: runtime-only
    const cipher = encrypt(secret, message, "revision:one");
    expect(hex(decrypt(secret, cipher, "revision:one"))).toBe(hex(message));
    expect(() => decrypt(secret, cipher, "revision:two")).toThrow();
    cipher[cipher.length - 1] ^= 1;
    expect(() => decrypt(secret, cipher, "revision:one")).toThrow();
  });
  it("binds device signatures to every revision field and uses Python-compatible canonical JSON", () => {
    const d = generateDevice("knovv", "fixture-device");
    const value = { z: ["é", "😀"], a: { c: 2, b: true } };
    expect(canonical({ "\u{10000}": 1, "\uE000": 2 })).toBe(
      '{"\uE000":2,"\u{10000}":1}',
    );
    expect(canonical(value)).toBe('{"a":{"b":true,"c":2},"z":["é","😀"]}');
    const signed = sign(value, d.signing_key);
    expect(verify(signed, d.signing_public)).toBe(true);
    expect(verify({ ...signed, z: ["changed"] }, d.signing_public)).toBe(false);
    expect(sha256(new Uint8Array())).toHaveLength(64);
  });
});
