import { chacha20poly1305 } from "@noble/ciphers/chacha.js";
import { ed25519, x25519 } from "@noble/curves/ed25519.js";
import { sha256 as hash } from "@noble/hashes/sha2.js";
import { hkdf } from "@noble/hashes/hkdf.js";
import { hmac } from "@noble/hashes/hmac.js";
import { scryptAsync } from "@noble/hashes/scrypt.js";

export const encoder = new TextEncoder(),
  decoder = new TextDecoder("utf-8", { fatal: true });
export const FORMAT = "cce-sealed.v1",
  KEY_FORMAT = "cce-protected-identity.v1";
export const MAX_OBJECT = 48 * 1024 * 1024;
export interface Identity {
  format: string;
  app_id: string;
  instance_id: string;
  encryption_key: string;
  signing_key: string;
}
export interface Device extends Identity {
  id: string;
  signing_public: string;
  encryption_public: string;
}
export type Signed<T = Record<string, unknown>> = T & { signature: string };
export function hex(bytes: Uint8Array): string {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}
export function unhex(value: string): Uint8Array {
  if (
    typeof value !== "string" ||
    value.length % 2 ||
    !/^[0-9a-f]*$/.test(value)
  )
    throw new Error("invalid-hex");
  return Uint8Array.from(value.match(/../g) ?? [], (b) => parseInt(b, 16));
}
export const sha256 = (data: Uint8Array) => hex(hash(data));
export function random(length = 32): Uint8Array {
  const value = new Uint8Array(length);
  for (let offset = 0; offset < length; offset += 65536)
    crypto.getRandomValues(
      value.subarray(offset, Math.min(length, offset + 65536)),
    );
  return value;
}
export function canonical(value: unknown): string {
  if (value === null || typeof value === "boolean" || typeof value === "string")
    return JSON.stringify(value);
  if (typeof value === "number" && Number.isSafeInteger(value))
    return JSON.stringify(value);
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  if (value && typeof value === "object") {
    return (
      "{" +
      Object.keys(value)
        .filter((k) => (value as any)[k] !== undefined)
        .sort((a, b) => {
          const left = Array.from(a),
            right = Array.from(b);
          for (let i = 0; i < Math.min(left.length, right.length); i++) {
            const difference =
              left[i].codePointAt(0)! - right[i].codePointAt(0)!;
            if (difference) return difference;
          }
          return left.length - right.length;
        })
        .map((k) => JSON.stringify(k) + ":" + canonical((value as any)[k]))
        .join(",") +
      "}"
    );
  }
  throw new Error("invalid-canonical-value");
}
export function derive(
  secret /* allow-secret: runtime-only */ : Uint8Array,
  purpose: string,
): Uint8Array {
  return hkdf(
    hash,
    secret,
    undefined,
    encoder.encode(FORMAT + ":" + purpose),
    32,
  );
}
export function address(
  secret /* allow-secret: runtime-only */ : Uint8Array,
  bytes: Uint8Array,
): string {
  return hex(hmac(hash, derive(secret, "address"), bytes));
}
export function encrypt(
  secret: Uint8Array, // allow-secret: runtime-only
  data: Uint8Array,
  context: string,
): Uint8Array {
  if (secret.length !== 32 || data.length > MAX_OBJECT)
    throw new Error("invalid-object");
  const length = Math.ceil((data.length + 8) / 4096) * 4096;
  const padded = random(length);
  new DataView(padded.buffer).setBigUint64(0, BigInt(data.length));
  padded.set(data, 8);
  const nonce = random(12),
    encrypted = chacha20poly1305(
      derive(secret, "aead"),
      nonce,
      encoder.encode(context),
    ).encrypt(padded);
  const result = new Uint8Array(nonce.length + encrypted.length);
  result.set(nonce);
  result.set(encrypted, nonce.length);
  padded.fill(0);
  return result;
}
export function decrypt(
  secret: Uint8Array, // allow-secret: runtime-only
  data: Uint8Array,
  context: string,
): Uint8Array {
  if (
    secret.length !== 32 ||
    data.length < 36 ||
    data.length > MAX_OBJECT + 8192
  )
    throw new Error("invalid-object");
  const plain = chacha20poly1305(
    derive(secret, "aead"),
    data.subarray(0, 12),
    encoder.encode(context),
  ).decrypt(data.subarray(12));
  if (plain.length < 8) throw new Error("invalid-object");
  const length = Number(
    new DataView(plain.buffer, plain.byteOffset, plain.length).getBigUint64(0),
  );
  if (!Number.isSafeInteger(length) || length > plain.length - 8)
    throw new Error("invalid-object");
  const result = plain.slice(8, 8 + length);
  plain.fill(0);
  return result;
}
export function deviceFromIdentity(value: Identity): Device {
  if (
    value.format !== FORMAT ||
    !value.app_id ||
    !value.instance_id ||
    unhex(value.encryption_key).length !== 32 ||
    unhex(value.signing_key).length !== 32
  )
    throw new Error("invalid-device");
  const signing_public = hex(ed25519.getPublicKey(unhex(value.signing_key))),
    encryption_public = hex(x25519.getPublicKey(unhex(value.encryption_key)));
  return {
    ...value,
    id: sha256(encoder.encode(signing_public)),
    signing_public,
    encryption_public,
  };
}
export function generateDevice(app_id: string, instance_id: string): Device {
  return deviceFromIdentity({
    format: FORMAT,
    app_id,
    instance_id,
    encryption_key: hex(random()),
    signing_key: hex(random()),
  });
}
export function sign<T extends Record<string, unknown>>(
  value: T,
  privateKey: string,
): Signed<T> {
  const { signature: _, ...unsigned } = value;
  return {
    ...unsigned,
    signature: hex(
      ed25519.sign(encoder.encode(canonical(unsigned)), unhex(privateKey)),
    ),
  } as Signed<T>;
}
export function verify(value: unknown, publicKey: string): boolean {
  try {
    const { signature, ...unsigned } = value as Record<string, unknown>;
    return (
      typeof signature === "string" &&
      ed25519.verify(
        unhex(signature),
        encoder.encode(canonical(unsigned)),
        unhex(publicKey),
      )
    );
  } catch {
    return false;
  }
}
const identity = (d: Identity): Identity => ({
  format: FORMAT,
  app_id: d.app_id,
  instance_id: d.instance_id,
  encryption_key: d.encryption_key,
  signing_key: d.signing_key,
});
async function passphraseKey(
  passphrase /* allow-secret: runtime-only */ : string,
  salt: Uint8Array,
) {
  const bytes = encoder.encode(passphrase);
  if (!bytes.length || bytes.length > 65536 || salt.length !== 16)
    throw new Error("credential-unlock-failed");
  try {
    return await scryptAsync(bytes, salt, {
      N: 32768,
      r: 8,
      p: 1,
      dkLen: 32,
      maxmem: 64 * 1024 * 1024,
    });
  } finally {
    bytes.fill(0);
  }
}
export async function protectDevice(
  device: Identity,
  passphrase: string, // allow-secret: runtime-only
): Promise<Record<string, string>> {
  if (encoder.encode(passphrase).length < 12)
    throw new Error("passphrase-too-short");
  const salt = random(16),
    key = await passphraseKey(passphrase, salt);
  try {
    return {
      format: KEY_FORMAT,
      salt: hex(salt),
      ciphertext: hex(
        encrypt(key, encoder.encode(canonical(identity(device))), KEY_FORMAT),
      ),
    };
  } finally {
    key.fill(0);
  }
}
export async function unlockDevice(
  value: unknown,
  passphrase: string, // allow-secret: runtime-only
): Promise<Device> {
  try {
    const record = value as Record<string, string>;
    if (record.format !== KEY_FORMAT || record.ciphertext.length > 131072)
      throw new Error("invalid-credential");
    const key = await passphraseKey(passphrase, unhex(record.salt));
    try {
      return deviceFromIdentity(
        JSON.parse(
          decoder.decode(decrypt(key, unhex(record.ciphertext), KEY_FORMAT)),
        ),
      );
    } finally {
      key.fill(0);
    }
  } catch {
    throw new Error("credential-unlock-failed");
  }
}
export const recipient = (d: Device) => ({
  app_id: d.app_id,
  instance_id: d.instance_id,
  encryption_public: d.encryption_public,
});
export function wrap(
  secret: Uint8Array, // allow-secret: runtime-only
  target: ReturnType<typeof recipient>,
  context: string,
): string {
  const ephemeral = random(),
    publicKey = x25519.getPublicKey(ephemeral),
    shared = x25519.getSharedSecret(ephemeral, unhex(target.encryption_public));
  const cipher = encrypt(
    shared,
    encoder.encode(canonical({ secret: hex(secret), recipient: target })), // allow-secret: runtime-only
    context,
  );
  const value = new Uint8Array(32 + cipher.length);
  value.set(publicKey);
  value.set(cipher, 32);
  ephemeral.fill(0);
  shared.fill(0);
  return hex(value);
}
export function unwrap(
  envelopes: string[],
  device: Device,
  context: string,
): Uint8Array {
  if (!Array.isArray(envelopes) || envelopes.length > 64)
    throw new Error("invalid-envelopes");
  for (const envelope of envelopes) {
    try {
      const bytes = unhex(envelope),
        shared = x25519.getSharedSecret(
          unhex(device.encryption_key),
          bytes.subarray(0, 32),
        );
      const plain = decrypt(shared, bytes.subarray(32), context);
      shared.fill(0);
      const payload = JSON.parse(decoder.decode(plain));
      plain.fill(0);
      if (canonical(payload.recipient) === canonical(recipient(device))) {
        const secret = unhex(payload.secret); // allow-secret: runtime-only
        if (secret.length === 32) return secret;
      }
    } catch {
      /* Try the next authorized recipient, with a content-free final error. */
    }
  }
  throw new Error("not-authorized");
}
