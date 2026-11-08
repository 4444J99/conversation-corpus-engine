import { address, decoder, decrypt } from "./crypto";
import { ByteCache } from "./byte-cache";
const ID = /^[0-9a-f]{64}$/;
type Ref = [string, number, number];
/** On-device decoded cache: an 8 MiB locator budget and one bounded data slab. */
export class PackedReader {
  private maps = new ByteCache<string, Record<string, Ref>>(8 * 1024 * 1024);
  private slab: { id: string; bytes: Uint8Array } | null = null;
  private closed = false;
  constructor(
    private secret: Uint8Array, // allow-secret: runtime-only
    private context: string,
    private resolve: Record<string, string> | undefined,
    private load: (id: string) => Promise<Uint8Array>,
    private inflate: (bytes: Uint8Array) => Promise<Uint8Array>,
  ) {}
  close() {
    this.closed = true;
    this.slab?.bytes.fill(0);
    this.slab = null;
    this.maps.clear();
  }
  private check() {
    if (this.closed) throw new Error("vault-locked");
  }
  private async direct(id: string) {
    this.check();
    if (!ID.test(id)) throw new Error("invalid-object-locator");
    const raw = await this.load(id);
    this.check();
    const compressed = decrypt(this.secret, raw, this.context);
    try {
      const plain = await this.inflate(compressed);
      if (this.closed) {
        plain.fill(0);
        throw new Error("vault-locked");
      }
      return plain;
    } finally {
      compressed.fill(0);
    }
  }
  async raw(key: string): Promise<Uint8Array> {
    this.check();
    if (!ID.test(key)) throw new Error("invalid-object-locator");
    if (!this.resolve) return this.direct(key);
    const prefix = key.slice(0, 2);
    let mapping = this.maps.get(prefix);
    if (!mapping) {
      const oid = this.resolve[prefix];
      if (!oid) throw new Error("missing-object-locator");
      const plain = await this.direct(oid);
      try {
        mapping = JSON.parse(decoder.decode(plain));
      } finally {
        plain.fill(0);
      }
      if (!mapping || typeof mapping !== "object" || Array.isArray(mapping))
        throw new Error("invalid-object-locator");
      this.maps.set(prefix, mapping);
    }
    const ref = Object.hasOwn(mapping, key) ? mapping[key] : null;
    if (
      !Array.isArray(ref) ||
      ref.length !== 3 ||
      !ID.test(ref[0]) ||
      !Number.isSafeInteger(ref[1]) ||
      !Number.isSafeInteger(ref[2]) ||
      ref[1] < 8 ||
      ref[2] < 1 ||
      ref[1] + ref[2] > 32 * 1024 * 1024
    )
      throw new Error("invalid-packed-offset");
    const [id, offset, length] = ref;
    let slab = this.slab?.id === id ? this.slab.bytes : null;
    if (!slab) {
      const bytes = await this.direct(id);
      this.check();
      this.slab?.bytes.fill(0);
      this.slab = { id, bytes };
      slab = bytes;
    }
    if (
      offset + length > slab.length ||
      new DataView(slab.buffer, slab.byteOffset + offset - 8, 8).getBigUint64(
        0,
      ) !== BigInt(length)
    )
      throw new Error("invalid-packed-frame");
    const plain = slab.slice(offset, offset + length);
    if (address(this.secret, plain) !== key) {
      plain.fill(0);
      throw new Error("invalid-packed-address");
    }
    return plain;
  }
  async value<T>(key: string): Promise<T> {
    const raw = await this.raw(key);
    this.check();
    try {
      return JSON.parse(decoder.decode(raw));
    } finally {
      raw.fill(0);
    }
  }
}
