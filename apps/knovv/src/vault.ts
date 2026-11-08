import {
  address,
  canonical,
  decrypt,
  encrypt,
  encoder,
  decoder,
  hex,
  random,
  sha256,
  sign,
  unhex,
  unlockDevice,
  unwrap,
  verify,
  type Device,
} from "./crypto";
import type {
  DeviceGrant,
  DevicePackage,
  Document,
  AttachmentRef,
  DocumentMeta,
  JournalEntry,
  Manifest,
  Publication,
  Revision,
  SourceSpan,
} from "./contracts";
import type { StoragePort } from "./storage";
import { PackedReader } from "./packed";
export { MemoryStorage } from "./storage";
export const PLAIN_LIMIT = 32 * 1024 * 1024;
export async function readStream(
  stream: ReadableStream<Uint8Array> | null,
  limit: number,
): Promise<Uint8Array> {
  if (!stream) throw new Error("empty-response");
  const reader = stream.getReader(),
    parts: Uint8Array[] = [];
  let length = 0;
  try {
    while (true) {
      const next = await reader.read();
      if (next.done) break;
      length += next.value.length;
      if (length > limit) {
        await reader.cancel();
        throw new Error("response-too-large");
      }
      parts.push(next.value);
    }
  } finally {
    reader.releaseLock();
  }
  const result = new Uint8Array(length);
  let offset = 0;
  for (const p of parts) {
    result.set(p, offset);
    offset += p.length;
  }
  return result;
}
export async function compress(bytes: Uint8Array): Promise<Uint8Array> {
  return readStream(
    new Blob([new Uint8Array(bytes).buffer])
      .stream()
      .pipeThrough(new CompressionStream("deflate")),
    PLAIN_LIMIT,
  );
}
export async function inflate(bytes: Uint8Array): Promise<Uint8Array> {
  return readStream(
    new Blob([new Uint8Array(bytes).buffer])
      .stream()
      .pipeThrough(new DecompressionStream("deflate")),
    PLAIN_LIMIT,
  );
}
export class ApiError extends Error {
  constructor(
    public code: string,
    public status: number,
  ) {
    super(code);
  }
}
type Fetcher = (path: string, init?: RequestInit) => Promise<Response>;
interface Pending {
  envelope: Revision;
  cipher: Uint8Array;
  intent: "draft" | "save";
}
export interface OpenDocument {
  document: Document;
  revision: Revision | null;
  local?: boolean;
}
export class Vault {
  private session = 0;
  private reader: PackedReader | null = null;
  private stageNumber = 0;
  private latestStage = new Map<string, number>();
  private draftWrites = new Map<string, Promise<void>>();
  private async wait<T>(operation: Promise<T>, session: number): Promise<T> {
    const value = await operation;
    if (session !== this.session) throw new Error("vault-locked");
    return value;
  }
  private device: Device | null = null;
  private secret: Uint8Array | null = null; // allow-secret: runtime-only
  private pack: DevicePackage | null = null;
  private metadata = new Map<number, DocumentMeta[]>();
  private locators = new Map<string, Record<string, number>>();
  private grants = new Map<string, DeviceGrant>();
  private overlays = new Map<string, Revision>();
  private overlayDocs = new Map<string, Document>();
  private cursor = 0;
  manifest: Manifest | null = null;
  publication: Publication | null = null;
  offline = false;
  constructor(
    public storage: StoragePort,
    private fetcher: Fetcher = (path, init) => fetch(path, init),
  ) {}
  get unlocked() {
    return (
      this.device !== null && this.secret !== null && this.manifest !== null
    );
  }
  get deviceId() {
    return this.device?.id ?? "";
  }
  get recent() {
    return [...this.overlayDocs.values()].sort((a, b) =>
      b.updated.localeCompare(a.updated),
    );
  }
  backlinks(id: string): DocumentMeta[] {
    this.require();
    const pages = new Map(
      (this.manifest!.navigation ?? []).map((page) => [page.id, page]),
    );
    for (const doc of this.recent)
      pages.set(doc.id, {
        ...doc,
        ordinal: -1,
        object: this.overlays.get(doc.id)!.object,
      });
    return [...pages.values()].filter((page) => page.links.includes(id));
  }
  private context() {
    return `knovv-object:${this.publication!.vault}:${this.publication!.epoch}`;
  }
  private revisionContext(epoch: number) {
    return `knovv-revision-object:${this.publication!.vault}:${epoch}`;
  }
  private require() {
    if (!this.unlocked) throw new Error("vault-locked");
  }
  private keyFor(epoch: number): Uint8Array {
    this.require();
    if (epoch === this.publication!.epoch) return this.secret!;
    const value = this.manifest!.epoch_keys?.[String(epoch)];
    if (
      !value ||
      !/^[0-9a-f]{64}$/.test(value) ||
      epoch >= this.publication!.epoch
    )
      throw new Error("historical-epoch-not-enrolled");
    return unhex(value);
  }
  async json(path: string, init?: RequestInit): Promise<any> {
    const ticket = this.session;
    const r = await this.wait(
      this.fetcher(path, { ...init, credentials: "same-origin" }),
      ticket,
    );
    const bytes = await this.wait(readStream(r.body, 1024 * 1024), ticket);
    let value: any;
    try {
      value = JSON.parse(decoder.decode(bytes));
    } catch {
      throw new ApiError("invalid-server-response", r.status);
    }
    if (!r.ok) throw new ApiError(value.error ?? "request-failed", r.status);
    return value;
  }
  async unlock(
    pack: DevicePackage,
    passphrase /* allow-secret: runtime-only */ : string,
  ) {
    this.lock();
    const ticket = this.session;
    if (
      pack.schema !== "knovv-device-package.v1" ||
      !/^[0-9a-f]{64}$/.test(pack.trust.owner) ||
      !/^[0-9a-f]{64}$/.test(pack.trust.vault)
    )
      throw new Error("invalid-device-package");
    this.device = await this.wait(
      unlockDevice(pack.credential, passphrase),
      ticket,
    );
    this.pack = pack;
    try {
      let pub: Publication;
      try {
        pub = await this.wait(this.json("/api/publication"), ticket);
        this.offline = false;
      } catch (error) {
        if (error instanceof ApiError) throw error;
        const cached = await this.wait(
          this.storage.get<Publication>("publication:" + pack.trust.vault),
          ticket,
        );
        if (!cached) throw error;
        pub = cached;
        this.offline = true;
      }
      const checkpoint = await this.wait(
        this.storage.get<DevicePackage["trust"]>(
          "checkpoint:" + pack.trust.vault,
        ),
        ticket,
      );
      const trust =
        checkpoint && checkpoint.generation > pack.trust.generation
          ? checkpoint
          : pack.trust;
      const head = sha256(encoder.encode(canonical(pub)));
      if (
        pub.schema !== "knovv-publication.v1" ||
        !verify(pub, trust.owner) ||
        pub.vault !== trust.vault ||
        pub.generation < trust.generation ||
        (pub.generation === trust.generation && head !== trust.head)
      )
        throw new Error("rollback-or-invalid-publication");
      this.publication = pub;
      this.secret /* allow-secret: runtime-only */ = unwrap(
        pub.envelopes,
        this.device,
        `knovv-publication.v1:${pub.vault}:${pub.epoch}`,
      );
      this.manifest = await this.wait(
        this.object<Manifest>(pub.manifest),
        ticket,
      );
      if (
        this.manifest.schema !== "knovv-catalog.v1" ||
        this.manifest.vault !== pub.vault ||
        this.manifest.epoch !== pub.epoch
      )
        throw new Error("invalid-catalog");
      this.reader = new PackedReader(
        this.secret,
        this.context(),
        this.manifest.resolve,
        (oid) => this.raw(oid),
        inflate,
      );
      await this.wait(
        this.storage.set("checkpoint:" + pub.vault, {
          ...trust,
          generation: pub.generation,
          head,
        }),
        ticket,
      );
      await this.wait(
        this.storage.set("publication:" + pub.vault, pub),
        ticket,
      );
      await this.wait(this.storage.set("device-package", pack), ticket);
      await this.wait(this.refreshGrants(), ticket);
      const retained = await this.wait(
        this.storage.get<{
          cursor: number;
          heads: Revision[];
        }>("journal:" + pub.vault),
        ticket,
      );
      if (retained) {
        this.cursor = retained.cursor;
        for (const revision of retained.heads) {
          const grant = this.grants.get(revision.device);
          if (!grant || !verify(revision, grant.signing_public))
            throw new Error("invalid-cached-revision");
          this.overlays.set(revision.doc, revision);
          if (
            await this.wait(
              this.storage.get("object:" + revision.object),
              ticket,
            )
          ) {
            const doc = await this.wait(this.revisionDoc(revision), ticket);
            this.overlayDocs.set(doc.id, doc);
          }
        }
      }
      await this.wait(this.sync(), ticket);
    } catch (error) {
      if (ticket === this.session) this.lock();
      throw error;
    }
  }
  lock() {
    this.session++;
    this.reader?.close();
    this.reader = null;
    this.secret?.fill(0);
    this.secret = null; // allow-secret: runtime-only
    this.device = null;
    this.pack = null;
    this.manifest = null;
    this.publication = null;
    this.metadata.clear();
    this.locators.clear();
    this.grants.clear();
    this.overlays.clear();
    this.overlayDocs.clear();
    this.cursor = 0;
  }
  async raw(oid: string): Promise<Uint8Array> {
    const ticket = this.session;
    if (!/^[0-9a-f]{64}$/.test(oid)) throw new Error("invalid-object");
    const key = "object:" + oid;
    let bytes = await this.wait(this.storage.get<Uint8Array>(key), ticket);
    if (!bytes) {
      const r = await this.wait(
        this.fetcher("/api/objects/" + oid, {
          credentials: "same-origin",
        }),
        ticket,
      );
      if (!r.ok) throw new ApiError("object-unavailable", r.status);
      bytes = await this.wait(readStream(r.body, 48 * 1024 * 1024), ticket);
      if (sha256(bytes) !== oid) throw new Error("ciphertext-digest-mismatch");
      try {
        await this.wait(this.storage.putCipher(oid, bytes), ticket);
      } catch {
        /* A cache failure cannot turn a verified remote read into missing content. */
      }
    }
    if (sha256(bytes) !== oid) throw new Error("ciphertext-digest-mismatch");
    return bytes;
  }
  async object<T>(
    oid: string,
    context = this.context(),
    key = this.secret,
  ): Promise<T> {
    const ticket = this.session;
    if (this.reader && context === this.context())
      return this.wait(this.reader.value<T>(oid), ticket);
    if (!this.secret || !key) throw new Error("vault-locked");
    const plain = await this.wait(
      inflate(decrypt(key, await this.wait(this.raw(oid), ticket), context)),
      ticket,
    );
    try {
      return JSON.parse(decoder.decode(plain));
    } finally {
      plain.fill(0);
    }
  }
  async meta(ordinal: number): Promise<DocumentMeta> {
    const ticket = this.session;
    this.require();
    const page = Math.floor(ordinal / 256);
    if (
      !Number.isSafeInteger(ordinal) ||
      ordinal < 0 ||
      ordinal >= this.manifest!.count
    )
      throw new Error("invalid-locator");
    if (!this.metadata.has(page))
      this.metadata.set(
        page,
        await this.wait(
          this.object<DocumentMeta[]>(this.manifest!.metadata[page]),
          ticket,
        ),
      );
    const result = this.metadata.get(page)!.find((m) => m.ordinal === ordinal);
    if (!result) throw new Error("missing-locator");
    return result;
  }
  async metaForId(id: string): Promise<DocumentMeta | null> {
    const ticket = this.session;
    const overlay = this.overlayDocs.get(id);
    if (overlay)
      return { ...overlay, ordinal: -1, object: this.overlays.get(id)!.object };
    const prefix = id.slice(0, 2),
      shard = this.manifest!.lookup[prefix];
    if (!shard) return null;
    if (!this.locators.has(prefix))
      this.locators.set(
        prefix,
        await this.wait(this.object<Record<string, number>>(shard), ticket),
      );
    const ordinal = this.locators.get(prefix)![id];
    return ordinal === undefined ? null : this.meta(ordinal);
  }
  private validate(doc: Document, id: string) {
    if (
      doc.id !== id ||
      typeof doc.title !== "string" ||
      typeof doc.body !== "string" ||
      !Array.isArray(doc.sources) ||
      !Array.isArray(doc.links) ||
      !["wiki", "project", "zettel", "source", "capture"].includes(doc.kind)
    )
      throw new Error("invalid-document");
    return doc;
  }
  private async revisionDoc(revision: Revision): Promise<Document> {
    const ticket = this.session;
    const grant = this.grants.get(revision.device);
    if (!grant || !verify(revision, grant.signing_public))
      throw new Error("invalid-revision-signature");
    const key = this.keyFor(revision.epoch);
    try {
      return this.validate(
        await this.wait(
          this.object<Document>(
            revision.object,
            this.revisionContext(revision.epoch),
            key,
          ),
          ticket,
        ),
        revision.doc,
      );
    } finally {
      if (key !== this.secret) key.fill(0);
    }
  }
  async open(id: string): Promise<OpenDocument> {
    const ticket = this.session;
    this.require();
    let revision = this.overlays.get(id) ?? null;
    try {
      const remote = await this.wait(this.json("/api/documents/" + id), ticket);
      if (remote.head) revision = remote.head;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      this.offline = true;
    }
    if (revision) {
      const document = await this.wait(this.revisionDoc(revision), ticket);
      this.overlays.set(id, revision);
      this.overlayDocs.set(id, document);
      await this.wait(this.persistHeads(), ticket);
      return { document, revision };
    }
    const meta = await this.wait(this.metaForId(id), ticket);
    if (!meta) throw new Error("document-unavailable");
    return {
      document: this.validate(
        await this.wait(this.object<Document>(meta.object), ticket),
        id,
      ),
      revision: null,
    };
  }
  async refreshGrants() {
    const ticket = this.session;
    try {
      const data = await this.wait(this.json("/api/devices"), ticket);
      await this.wait(
        this.storage.set("grants:" + this.publication!.vault, data.devices),
        ticket,
      );
      this.offline = false;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      this.offline = true;
    }
    const grants =
      (await this.wait(
        this.storage.get<DeviceGrant[]>("grants:" + this.publication!.vault),
        ticket,
      )) ?? [];
    this.grants.clear();
    for (const grant of grants) {
      if (!verify(grant, this.pack!.trust.owner))
        throw new Error("invalid-device-enrollment");
      this.grants.set(grant.id, grant);
    }
    const own = this.grants.get(this.device!.id);
    if (!own || own.revoked) throw new Error("device-not-enrolled");
  }
  private async refreshPublication(): Promise<void> {
    const ticket = this.session;
    this.require();
    const pub = (await this.wait(
      this.json("/api/publication"),
      ticket,
    )) as Publication;
    const current = this.publication!,
      checksum = sha256(encoder.encode(canonical(pub)));
    if (
      pub.schema !== "knovv-publication.v1" ||
      pub.vault !== current.vault ||
      !Number.isSafeInteger(pub.generation) ||
      pub.generation < current.generation ||
      !verify(pub, this.pack!.trust.owner)
    )
      throw new Error("rollback-or-invalid-publication");
    if (pub.generation === current.generation) {
      if (checksum !== sha256(encoder.encode(canonical(current))))
        throw new Error("rollback-or-invalid-publication");
      return;
    }
    const secret /* allow-secret: runtime-only */ = unwrap(
      pub.envelopes,
      this.device!,
      `knovv-publication.v1:${pub.vault}:${pub.epoch}`,
    );
    let adopted = false;
    try {
      const plain = await this.wait(
        inflate(
          decrypt(
            secret,
            await this.wait(this.raw(pub.manifest), ticket),
            `knovv-object:${pub.vault}:${pub.epoch}`,
          ),
        ),
        ticket,
      );
      let manifest: Manifest;
      try {
        manifest = JSON.parse(decoder.decode(plain));
      } finally {
        plain.fill(0);
      }
      if (
        manifest.schema !== "knovv-catalog.v1" ||
        manifest.vault !== pub.vault ||
        manifest.epoch !== pub.epoch ||
        manifest.id_key !== this.manifest!.id_key
      )
        throw new Error("invalid-catalog");
      this.reader?.close();
      this.secret?.fill(0);
      this.publication = pub;
      this.secret = secret; // allow-secret: runtime-only
      this.manifest = manifest;
      adopted = true;
      this.reader = new PackedReader(
        secret,
        this.context(),
        manifest.resolve,
        (id) => this.raw(id),
        inflate,
      );
      this.metadata.clear();
      this.locators.clear();
      await this.wait(
        this.storage.set("checkpoint:" + pub.vault, {
          ...this.pack!.trust,
          generation: pub.generation,
          head: checksum,
        }),
        ticket,
      );
      await this.wait(
        this.storage.set("publication:" + pub.vault, pub),
        ticket,
      );
    } finally {
      if (!adopted) secret.fill(0);
    }
  }
  async sync(): Promise<void> {
    const ticket = this.session;
    this.require();
    try {
      await this.wait(this.refreshGrants(), ticket);
      await this.wait(this.refreshPublication(), ticket);
      let more = true;
      while (more) {
        const reply = await this.wait(
          this.json("/api/sync?after=" + this.cursor),
          ticket,
        );
        if (!Array.isArray(reply.revisions) || reply.cursor < this.cursor)
          throw new Error("invalid-sync-cursor");
        for (const entry of reply.revisions as JournalEntry[]) {
          if (!Number.isSafeInteger(entry.seq) || entry.seq <= this.cursor)
            throw new Error("invalid-sync-sequence");
          const doc = await this.wait(this.revisionDoc(entry.envelope), ticket);
          this.overlays.set(doc.id, entry.envelope);
          this.overlayDocs.set(doc.id, doc);
        }
        if (reply.has_more && reply.cursor === this.cursor)
          throw new Error("stalled-sync-cursor");
        this.cursor = reply.cursor;
        more = reply.has_more;
      }
      this.offline = false;
      await this.wait(this.persistHeads(), ticket);
    } catch (error) {
      if (error instanceof ApiError || !(error instanceof TypeError))
        throw error;
      this.offline = true;
    }
  }
  private async persistHeads() {
    const ticket = this.session;
    await this.wait(
      this.storage.set("journal:" + this.publication!.vault, {
        cursor: this.cursor,
        heads: [...this.overlays.values()],
      }),
      ticket,
    );
  }
  newDocument(kind: Document["kind"] = "capture"): Document {
    this.require();
    const logical_id = "knovv:" + hex(random()),
      stamp = new Date().toISOString();
    return {
      id: address(unhex(this.manifest!.id_key), encoder.encode(logical_id)),
      logical_id,
      kind,
      title: "",
      body: "",
      summary: "",
      author: kind === "capture" ? "unknown" : "owner",
      authorship_basis: kind === "capture" ? "unverified" : "declared",
      status: "draft",
      created: stamp,
      updated: stamp,
      sources: [],
      links: [],
      tags: [],
    };
  }
  sourceId(source: SourceSpan): string {
    const start = Math.floor(source.start / 8000) * 8000,
      logical = `cce:${source.uid}@${source.version}#${source.sequence ?? "main"}:${source.message}:${start}`;
    return address(unhex(this.manifest!.id_key), encoder.encode(logical));
  }
  async stage(
    document: Document,
    parent: string | null,
    intent: "draft" | "save" = "draft",
  ): Promise<Pending> {
    const ticket = this.session;
    this.require();
    if (document.kind === "source") throw new Error("sources-are-immutable");
    this.validate(document, document.id);
    const order = ++this.stageNumber;
    this.latestStage.set(document.id, order);
    const compressed = await this.wait(
      compress(encoder.encode(canonical(document))),
      ticket,
    );
    const cipher = encrypt(
      this.secret!,
      compressed,
      this.revisionContext(this.publication!.epoch),
    );
    const envelope = sign(
      {
        schema: "knovv-revision.v1",
        id: hex(random()),
        doc: document.id,
        parent,
        object: sha256(cipher),
        device: this.device!.id,
        epoch: this.publication!.epoch,
      },
      this.device!.signing_key,
    ) as Revision;
    const pending = { envelope, cipher, intent };
    const key = `draft:${this.publication!.vault}:${document.id}`;
    const prior = this.draftWrites.get(document.id) ?? Promise.resolve();
    const write = prior
      .catch(() => undefined)
      .then(async () => {
        if (ticket !== this.session) throw new Error("vault-locked");
        if (this.latestStage.get(document.id) === order)
          await this.storage.set(key, pending);
      });
    this.draftWrites.set(document.id, write);
    try {
      await this.wait(write, ticket);
    } finally {
      if (this.draftWrites.get(document.id) === write)
        this.draftWrites.delete(document.id);
    }
    return pending;
  }
  async attachFile(
    document: Document,
    bytes: Uint8Array,
    name: string,
    media_type: string,
  ): Promise<Document> {
    const ticket = this.session;
    this.require();
    if (
      bytes.length > 4 * 1024 * 1024 ||
      (document.attachments?.length ?? 0) >= 8
    )
      throw new Error("attachment-bound");
    const compressed = await this.wait(compress(bytes), ticket);
    const epoch = this.publication!.epoch,
      cipher = encrypt(
        this.secret!,
        compressed,
        `knovv-attachment:${this.publication!.vault}:${epoch}`,
      ),
      object = sha256(cipher);
    await this.wait(
      this.storage.set(
        `attachment:${this.publication!.vault}:${object}`,
        cipher,
      ),
      ticket,
    );
    return {
      ...document,
      attachments: [
        ...(document.attachments ?? []),
        {
          object,
          name: name.slice(0, 240),
          media_type: media_type.slice(0, 100),
          size: bytes.length,
          epoch,
        },
      ],
    };
  }
  async attachment(ref: AttachmentRef): Promise<Uint8Array> {
    const ticket = this.session;
    this.require();
    const epochKey = this.keyFor(ref.epoch);
    const raw =
      (await this.wait(
        this.storage.get<Uint8Array>(
          `attachment:${this.publication!.vault}:${ref.object}`,
        ),
        ticket,
      )) ?? (await this.wait(this.raw(ref.object), ticket));
    if (sha256(raw) !== ref.object)
      throw new Error("ciphertext-digest-mismatch");
    const plain = await this.wait(
      inflate(
        decrypt(
          epochKey,
          raw,
          `knovv-attachment:${this.publication!.vault}:${ref.epoch}`,
        ),
      ),
      ticket,
    );
    if (plain.length !== ref.size || plain.length > 4 * 1024 * 1024)
      throw new Error("attachment-bound");
    if (epochKey !== this.secret) epochKey.fill(0);
    return plain;
  }
  async drafts(): Promise<OpenDocument[]> {
    const ticket = this.session;
    this.require();
    const result: OpenDocument[] = [];
    for (const key of await this.wait(
      this.storage.keys("draft:" + this.publication!.vault + ":"),
      ticket,
    )) {
      const pending = await this.wait(this.storage.get<Pending>(key), ticket);
      if (!pending) continue;
      const document = JSON.parse(
        decoder.decode(
          await this.wait(
            inflate(
              decrypt(
                this.keyFor(pending.envelope.epoch),
                pending.cipher,
                this.revisionContext(pending.envelope.epoch),
              ),
            ),
            ticket,
          ),
        ),
      ) as Document;
      this.validate(document, pending.envelope.doc);
      result.push({ document, revision: pending.envelope, local: true });
    }
    return result;
  }
  async send(pending: Pending): Promise<{
    state: "remote" | "local";
    revision: Revision;
  }> {
    const ticket = this.session;
    this.require();
    const revision = pending.envelope,
      auth = sign(
        {
          schema: "knovv-upload.v1",
          object: revision.object,
          size: pending.cipher.length,
          device: this.device!.id,
        },
        this.device!.signing_key,
      );
    try {
      const document = JSON.parse(
        decoder.decode(
          await this.wait(
            inflate(
              decrypt(
                this.secret!,
                pending.cipher,
                this.revisionContext(revision.epoch),
              ),
            ),
            ticket,
          ),
        ),
      ) as Document;
      for (const ref of document.attachments ?? []) {
        const bytes = await this.wait(
          this.storage.get<Uint8Array>(
            `attachment:${this.publication!.vault}:${ref.object}`,
          ),
          ticket,
        );
        if (!bytes) continue;
        const grant = sign(
          {
            schema: "knovv-upload.v1",
            object: ref.object,
            size: bytes.length,
            device: this.device!.id,
          },
          this.device!.signing_key,
        );
        const reply = await this.wait(
          this.fetcher("/api/objects/" + ref.object, {
            method: "PUT",
            credentials: "same-origin",
            headers: { "x-knovv-authorization": btoa(canonical(grant)) },
            body: new Uint8Array(bytes).buffer,
          }),
          ticket,
        );
        if (!reply.ok)
          throw new ApiError("attachment-upload-failed", reply.status);
        await this.wait(this.storage.putCipher(ref.object, bytes), ticket);
      }
      const uploaded = await this.wait(
        this.fetcher("/api/objects/" + revision.object, {
          method: "PUT",
          credentials: "same-origin",
          headers: { "x-knovv-authorization": btoa(canonical(auth)) },
          body: new Uint8Array(pending.cipher).buffer,
        }),
        ticket,
      );
      if (!uploaded.ok) {
        const value = (await this.wait(uploaded.json(), ticket)) as any;
        throw new ApiError(value.error ?? "upload-failed", uploaded.status);
      }
      await this.wait(
        this.json("/api/revisions", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: canonical(revision),
        }),
        ticket,
      );
      await this.wait(
        this.storage.putCipher(revision.object, pending.cipher),
        ticket,
      );
      await this.wait(
        this.storage.removeDraft(
          `draft:${this.publication!.vault}:${revision.doc}`,
          revision.id,
        ),
        ticket,
      );
      const doc = await this.wait(this.revisionDoc(revision), ticket);
      this.overlays.set(doc.id, revision);
      this.overlayDocs.set(doc.id, doc);
      for (const ref of document.attachments ?? [])
        await this.wait(
          this.storage.remove(
            `attachment:${this.publication!.vault}:${ref.object}`,
          ),
          ticket,
        );
      await this.wait(this.persistHeads(), ticket);
      this.offline = false;
      return { state: "remote", revision };
    } catch (error) {
      if (error instanceof ApiError) throw error;
      if (!(error instanceof TypeError)) throw error;
      this.offline = true;
      return { state: "local", revision };
    }
  }
  async save(document: Document, parent: string | null) {
    const ticket = this.session;
    return this.send(
      await this.wait(this.stage(document, parent, "save"), ticket),
    );
  }
  async retry(id: string) {
    const ticket = this.session;
    this.require();
    const pending = await this.wait(
      this.storage.get<Pending>(`draft:${this.publication!.vault}:${id}`),
      ticket,
    );
    if (!pending) throw new Error("draft-unavailable");
    return this.send(pending);
  }
  async history(id: string) {
    const ticket = this.session;
    this.require();
    const reply = await this.wait(this.json("/api/documents/" + id), ticket);
    return reply.revisions as JournalEntry[];
  }
  async historical(revision: Revision) {
    const ticket = this.session;
    this.require();
    return this.revisionDoc(revision);
  }
  async baseSearch(query: string) {
    const ticket = this.session;
    const { searchIndex } = await this.wait(import("./search"), ticket);
    return searchIndex(query, async (bucket) => {
      const oid = this.manifest!.search[bucket];
      return oid ? this.object<Record<string, number[]>>(oid) : {};
    });
  }
  async overlaySearch(query: string) {
    const ticket = this.session;
    const { terms } = await this.wait(import("./search"), ticket);
    const tokens = terms(query);
    return this.recent.filter((d) =>
      tokens.every((t) => (d.title + " " + d.body).toLowerCase().includes(t)),
    );
  }
  searchWorkerState() {
    this.require();
    return {
      secret: this.secret!.slice(), // allow-secret: runtime-only
      publication: this.publication!,
      manifest: this.manifest!,
    };
  }
}
