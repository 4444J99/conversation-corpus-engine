/** Bounded reader for the original signed cce-sealed.v1/v2 archive formats. */
import {
  address,
  canonical,
  decoder,
  decrypt,
  encoder,
  hex,
  sha256,
  unhex,
  unwrap,
  verify,
  type Device,
} from "./crypto";
import { inflate } from "./vault";
type Trust = { owner: string; vault: string; generation: number; head: string };
type Load = (path: string) => Promise<Uint8Array>;
const HEX = /^[0-9a-f]{64}$/;
const integer = (v: unknown, min: number, max: number): v is number =>
  Number.isSafeInteger(v) && (v as number) >= min && (v as number) <= max;
const PLAIN = 32 * 1024 * 1024;
function validId(value: unknown): asserts value is string {
  if (typeof value !== "string" || !HEX.test(value))
    throw new Error("invalid-sealed-address");
}
export class SealedReader {
  private secret: Uint8Array | null; // allow-secret: runtime-only
  private cached: { id: string; plain: Uint8Array } | null = null;
  private constructor(
    private head: any,
    private manifest: any,
    secret: Uint8Array, // allow-secret: runtime-only
    private trust: Trust,
    private load: Load,
  ) {
    this.secret = secret; // allow-secret: runtime-only
    this.validate();
  }
  static async open(head: any, device: Device, trust: Trust, load: Load) {
    validId(trust.owner);
    validId(trust.vault);
    if (
      !["cce-sealed.v1", "cce-sealed.v2"].includes(head?.format) ||
      head.vault !== trust.vault ||
      !integer(head.generation, 1, Number.MAX_SAFE_INTEGER) ||
      !verify(head, trust.owner) ||
      head.generation < trust.generation ||
      (head.generation === trust.generation &&
        sha256(encoder.encode(canonical(head))) !== trust.head)
    )
      throw new Error("rollback-or-invalid-sealed-publication");
    const context = `${head.format}:${head.vault}:${head.generation}`,
      secret = unwrap(head.envelopes, device, context); // allow-secret: runtime-only
    try {
      if (
        typeof head.manifest !== "string" ||
        head.manifest.length > 2 * (PLAIN + 8192)
      )
        throw new Error("sealed-manifest-bound");
      const plain = decrypt(
        secret,
        unhex(head.manifest),
        context + ":manifest",
      );
      try {
        return new SealedReader(
          head,
          JSON.parse(decoder.decode(plain)),
          secret,
          trust,
          load,
        );
      } finally {
        plain.fill(0);
      }
    } catch (error) {
      secret.fill(0);
      throw error;
    }
  }
  private validate() {
    const m = this.manifest;
    if (m?.format !== this.head.format || !m.objects || !m.files || !m.atoms)
      throw new Error("invalid-sealed-manifest");
    for (const [name, recipe] of Object.entries<any>(m.files)) {
      if (
        !name ||
        name.startsWith("/") ||
        name.includes("\\") ||
        name.split("/").some((x) => x === ".." || x === "." || !x) ||
        !Array.isArray(recipe.chunks) ||
        recipe.chunks.some(
          (id: unknown) =>
            typeof id !== "string" || !Object.hasOwn(m.objects, id),
        )
      )
        throw new Error("invalid-sealed-file");
      validId(recipe.sha256);
    }
    for (const id of Object.values(m.atoms))
      if (typeof id !== "string" || !Object.hasOwn(m.objects, id))
        throw new Error("invalid-sealed-atom");
    if (m.format === "cce-sealed.v2") {
      if (!m.shards) throw new Error("invalid-packed-manifest");
      const groups = new Map<string, any[]>();
      for (const [id, info] of Object.entries<any>(m.shards)) {
        validId(id);
        validId(info.cipher_sha256);
        if (
          info.codec !== "zlib" ||
          !integer(info.plain_bytes, 9, PLAIN) ||
          !integer(info.compressed_bytes, 1, 48 * 1024 * 1024)
        )
          throw new Error("invalid-packed-manifest");
        groups.set(id, []);
      }
      for (const [id, info] of Object.entries<any>(m.objects)) {
        validId(id);
        if (
          !groups.has(info.shard) ||
          !integer(info.offset, 8, PLAIN) ||
          !integer(info.length, 1, PLAIN)
        )
          throw new Error("invalid-packed-offset");
        groups.get(info.shard)!.push(info);
      }
      for (const [id, entries] of groups) {
        let end = 0;
        for (const info of entries.sort((a, b) => a.offset - b.offset)) {
          if (info.offset - 8 !== end) throw new Error("invalid-packed-offset");
          end = info.offset + info.length;
        }
        if (!entries.length || end !== m.shards[id].plain_bytes)
          throw new Error("invalid-packed-offset");
      }
    } else
      for (const [id, info] of Object.entries<any>(m.objects)) {
        validId(id);
        validId(info.cipher_sha256);
      }
  }
  lock() {
    this.secret?.fill(0);
    this.secret = null; // allow-secret: runtime-only
    this.cached?.plain.fill(0);
    this.cached = null;
  }
  private async cipher(path: string, id: string, digest: string) {
    const bytes = await this.load(path);
    if (!this.secret) throw new Error("vault-locked");
    if (
      bytes.length < 100 ||
      bytes.length > 48 * 1024 * 1024 ||
      sha256(bytes) !== digest ||
      !verify(
        {
          format: "cce-sealed.v1",
          purpose: "object",
          vault: this.trust.vault,
          address: id,
          cipher_sha256: sha256(bytes.subarray(64)),
          signature: hex(bytes.subarray(0, 64)),
        },
        this.trust.owner,
      )
    )
      throw new Error("invalid-sealed-ciphertext");
    return bytes.subarray(64);
  }
  async object(id: string): Promise<Uint8Array> {
    if (!this.secret) throw new Error("vault-locked");
    validId(id);
    const info = this.manifest.objects[id];
    if (!info) throw new Error("sealed-object-unavailable");
    let plain: Uint8Array;
    if (this.manifest.format === "cce-sealed.v1")
      plain = decrypt(
        this.secret,
        await this.cipher(
          `objects/${id.slice(0, 2)}/${id}`,
          id,
          info.cipher_sha256,
        ),
        `cce-sealed.v1:object:${id}`,
      );
    else {
      const sid = info.shard,
        meta = this.manifest.shards[sid];
      if (this.cached?.id !== sid) {
        this.cached?.plain.fill(0);
        this.cached = null;
        const cipher = await this.cipher(
          `shards/${sid.slice(0, 2)}/${sid}`,
          sid,
          meta.cipher_sha256,
        );
        if (!this.secret) throw new Error("vault-locked");
        const compressed = decrypt(
          this.secret,
          cipher,
          "cce-sealed.v2:shard:" + sid,
        );
        if (compressed.length !== meta.compressed_bytes)
          throw new Error("invalid-packed-shard");
        const slab = await inflate(compressed);
        compressed.fill(0);
        if (!this.secret) {
          slab.fill(0);
          throw new Error("vault-locked");
        }
        if (
          slab.length !== meta.plain_bytes ||
          address(this.secret, slab) !== sid
        )
          throw new Error("invalid-packed-shard");
        this.cached = { id: sid, plain: slab };
      }
      const slab = this.cached!.plain;
      if (
        new DataView(
          slab.buffer,
          slab.byteOffset + info.offset - 8,
          8,
        ).getBigUint64(0) !== BigInt(info.length)
      )
        throw new Error("invalid-packed-frame");
      plain = slab.slice(info.offset, info.offset + info.length);
    }
    if (!this.secret || address(this.secret, plain) !== id) {
      plain.fill(0);
      throw new Error("invalid-sealed-address");
    }
    return plain;
  }
  async file(name: string): Promise<Uint8Array> {
    if (!this.secret) throw new Error("vault-locked");
    const recipe = this.manifest.files[name];
    if (!recipe) throw new Error("sealed-file-unavailable");
    const chunks: Uint8Array[] = [];
    let size = 0;
    for (const id of recipe.chunks) {
      const bytes = await this.object(id);
      size += bytes.length;
      if (size > PLAIN) throw new Error("browser-file-bound");
      chunks.push(bytes);
    }
    const result = new Uint8Array(size);
    let offset = 0;
    for (const bytes of chunks) {
      result.set(bytes, offset);
      offset += bytes.length;
      bytes.fill(0);
    }
    if (sha256(result) !== recipe.sha256) {
      result.fill(0);
      throw new Error("sealed-file-digest");
    }
    return result;
  }
}
