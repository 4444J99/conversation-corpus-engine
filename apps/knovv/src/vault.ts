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
import { ByteCache } from "./byte-cache";
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
  if (bytes.length > PLAIN_LIMIT) throw new Error("plaintext-too-large");
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
  private metadata = new ByteCache<number, DocumentMeta[]>(8 * 1024 * 1024);
  private locators = new ByteCache<string, Record<string, number>>(
    8 * 1024 * 1024,
  );
  private grants = new Map<string, DeviceGrant>();
  private overlays = new Map<string, Revision>();
  private overlayDocs = new ByteCache<string, DocumentMeta>(8 * 1024 * 1024);
  private bodies = new ByteCache<
    string,
    { revision: string; document: Document }
  >(16 * 1024 * 1024);
  private known = new Map<string, JournalEntry>();
  private sequences = new Map<number, string>();
  private adoptions = new Map<string, Promise<unknown>>();
  private checkpoints: Promise<void> = Promise.resolve();
  private enrollmentUpdates: Promise<void> = Promise.resolve();
  private publicationUpdates: Promise<void> = Promise.resolve();
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
  get changedIds() {
    return new Set(this.overlays.keys());
  }
  get decodedCacheBytes() {
    return (
      this.bodies.bytes +
      this.overlayDocs.bytes +
      this.metadata.bytes +
      this.locators.bytes
    );
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
          known?: JournalEntry[];
        }>("journal:" + pub.vault),
        ticket,
      );
      if (retained) {
        if (!Number.isSafeInteger(retained.cursor) || retained.cursor < 0)
          throw new Error("invalid-cached-cursor");
        this.cursor = retained.cursor;
        for (const entry of retained.known ?? []) this.record(entry);
        for (const revision of retained.heads) {
          const grant = this.grants.get(revision.device);
          if (!grant || !verify(revision, grant.signing_public))
            throw new Error("invalid-cached-revision");
          this.overlays.set(revision.doc, revision);
          if (!this.known.has(revision.id))
            this.record({ seq: 0, envelope: revision });
          if (
            !(await this.wait(this.retainedMeta(revision), ticket)) &&
            !this.offline
          )
            await this.wait(this.adopt(revision), ticket);
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
    this.bodies.clear();
    this.known.clear();
    this.sequences.clear();
    this.adoptions.clear();
    this.checkpoints = Promise.resolve();
    this.enrollmentUpdates = Promise.resolve();
    this.publicationUpdates = Promise.resolve();
    this.cursor = 0;
  }
  async raw(oid: string): Promise<Uint8Array> {
    const ticket = this.session;
    if (!/^[0-9a-f]{64}$/.test(oid)) throw new Error("invalid-object");
    const key = "object:" + oid;
    let bytes = await this.wait(this.storage.get<Uint8Array>(key), ticket);
    if (!bytes && this.publication?.manifest === oid)
      bytes = await this.wait(
        this.storage.get<Uint8Array>(
          `bootstrap:${this.publication.vault}:${oid}`,
        ),
        ticket,
      );
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
    if (this.publication?.manifest === oid)
      await this.wait(this.pinBootstrap(oid, bytes), ticket);
    return bytes;
  }
  private async pinBootstrap(oid: string, bytes: Uint8Array) {
    const ticket = this.session,
      vault = this.publication!.vault;
    await this.wait(
      this.storage.set(`bootstrap:${vault}:${oid}`, bytes),
      ticket,
    );
    const order =
      (await this.wait(
        this.storage.get<string[]>("bootstrap-order:" + vault),
        ticket,
      )) ?? [];
    const next = [...order.filter((id) => id !== oid), oid];
    while (next.length > 2)
      await this.wait(
        this.storage.remove(`bootstrap:${vault}:${next.shift()!}`),
        ticket,
      );
    await this.wait(this.storage.set("bootstrap-order:" + vault, next), ticket);
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
    let entries = this.metadata.get(page);
    if (!entries) {
      entries = await this.wait(
        this.object<DocumentMeta[]>(this.manifest!.metadata[page]),
        ticket,
      );
      this.metadata.set(page, entries);
    }
    const result = entries.find((m) => m.ordinal === ordinal);
    if (!result) throw new Error("missing-locator");
    return result;
  }
  async metaForId(id: string): Promise<DocumentMeta | null> {
    const ticket = this.session;
    let overlay = this.overlayDocs.get(id);
    const head = this.overlays.get(id);
    if (!overlay && head)
      overlay = (await this.wait(this.retainedMeta(head), ticket)) ?? undefined;
    if (overlay)
      return { ...overlay, ordinal: -1, object: this.overlays.get(id)!.object };
    const prefix = id.slice(0, 2),
      shard = this.manifest!.lookup[prefix];
    if (!shard) return null;
    let mapping = this.locators.get(prefix);
    if (!mapping) {
      mapping = await this.wait(
        this.object<Record<string, number>>(shard),
        ticket,
      );
      this.locators.set(prefix, mapping);
    }
    const ordinal = mapping[id];
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
    const cached = this.bodies.get(revision.doc);
    if (cached?.revision === revision.id) return cached.document;
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
  private checkRevision(revision: Revision, id = revision.doc) {
    const grant = this.grants.get(revision.device);
    if (
      revision.schema !== "knovv-revision.v1" ||
      revision.doc !== id ||
      ![revision.id, revision.doc, revision.object, revision.device].every(
        (v) => /^[0-9a-f]{64}$/.test(v),
      ) ||
      !(revision.parent === null || /^[0-9a-f]{64}$/.test(revision.parent)) ||
      !grant ||
      !verify(revision, grant.signing_public)
    )
      throw new Error("invalid-revision-binding");
  }
  private record(entry: JournalEntry) {
    this.checkRevision(entry.envelope);
    if (!Number.isSafeInteger(entry.seq) || entry.seq < 0)
      throw new Error("invalid-revision-sequence");
    const prior = this.known.get(entry.envelope.id);
    if (
      prior &&
      (canonical(prior.envelope) !== canonical(entry.envelope) ||
        (prior.seq > 0 && entry.seq > 0 && prior.seq !== entry.seq))
    )
      throw new Error("revision-sequence-replay");
    if (
      entry.seq > 0 &&
      this.sequences.has(entry.seq) &&
      this.sequences.get(entry.seq) !== entry.envelope.id
    )
      throw new Error("revision-sequence-replay");
    if (!prior || entry.seq > 0) this.known.set(entry.envelope.id, entry);
    if (entry.seq > 0) this.sequences.set(entry.seq, entry.envelope.id);
  }
  private ancestor(ancestor: string, head: Revision) {
    const seen = new Set<string>();
    let current: Revision | undefined = head;
    while (current && !seen.has(current.id)) {
      if (current.id === ancestor) return true;
      seen.add(current.id);
      if (current.parent === ancestor) return true;
      current = current.parent
        ? this.known.get(current.parent)?.envelope
        : undefined;
    }
    return false;
  }
  async historyPage(
    id: string,
    before = 0,
  ): Promise<{ revisions: JournalEntry[]; nextBefore: number | null }> {
    const ticket = this.session;
    this.require();
    const reply = await this.wait(
      this.json("/api/documents/" + id + (before ? "?before=" + before : "")),
      ticket,
    );
    if (!Array.isArray(reply.revisions) || reply.revisions.length > 50)
      throw new Error("invalid-revision-history");
    let previous = before || Number.MAX_SAFE_INTEGER;
    for (const entry of reply.revisions as JournalEntry[]) {
      this.checkRevision(entry.envelope, id);
      if (
        !Number.isSafeInteger(entry.seq) ||
        entry.seq < 1 ||
        entry.seq >= previous
      )
        throw new Error("invalid-revision-history");
      this.record(entry);
      previous = entry.seq;
    }
    const next = reply.next_before ?? null;
    if (
      next !== null &&
      (!Number.isSafeInteger(next) || next !== previous || next < 1)
    )
      throw new Error("invalid-revision-history");
    return { revisions: reply.revisions, nextBefore: next };
  }
  private async ensureAdvance(revision: Revision, id: string) {
    this.checkRevision(revision, id);
    const current = this.overlays.get(id);
    if (!current || current.id === revision.id) return;
    const requested = new Set<number>();
    for (;;) {
      let node = revision;
      const seen = new Set<string>();
      for (;;) {
        if (node.id === current.id) return;
        if (seen.has(node.id) || node.parent === null)
          throw new Error("revision-rollback-or-fork");
        seen.add(node.id);
        if (node.parent === current.id) return;
        const parent = this.known.get(node.parent);
        if (parent) {
          node = parent.envelope;
          continue;
        }
        const before = this.known.get(node.id)?.seq ?? 0;
        if (!before || requested.has(before))
          throw new Error("revision-history-gap");
        requested.add(before);
        const page = await this.historyPage(id, before);
        if (!page.revisions.length) throw new Error("revision-history-gap");
        break;
      }
    }
  }
  private async remember(document: Document, revision: Revision) {
    const ticket = this.session,
      { body: _, ...fields } = document;
    const meta: DocumentMeta = {
      ...fields,
      ordinal: -1,
      object: revision.object,
    };
    const compressed = await this.wait(
        compress(encoder.encode(canonical(meta))),
        ticket,
      ),
      epoch = this.publication!.epoch;
    const cipher = encrypt(
      this.secret!,
      compressed,
      `knovv-head-meta:${this.publication!.vault}:${document.id}:${revision.id}:${epoch}`,
    );
    await this.wait(
      this.storage.set(`headmeta:${this.publication!.vault}:${document.id}`, {
        epoch,
        revision: revision.id,
        cipher,
      }),
      ticket,
    );
    this.overlayDocs.set(document.id, meta);
    this.bodies.set(document.id, { revision: revision.id, document });
  }
  private async retainedMeta(revision: Revision) {
    const ticket = this.session,
      item = await this.wait(
        this.storage.get<{
          epoch: number;
          revision: string;
          cipher: Uint8Array;
        }>(`headmeta:${this.publication!.vault}:${revision.doc}`),
        ticket,
      );
    if (!item || item.revision !== revision.id) return null;
    const key = this.keyFor(item.epoch);
    try {
      const raw = await this.wait(
        inflate(
          decrypt(
            key,
            item.cipher,
            `knovv-head-meta:${this.publication!.vault}:${revision.doc}:${revision.id}:${item.epoch}`,
          ),
        ),
        ticket,
      );
      try {
        const meta = JSON.parse(decoder.decode(raw)) as DocumentMeta;
        if (meta.id !== revision.doc || meta.object !== revision.object)
          throw new Error("invalid-head-metadata");
        this.overlayDocs.set(meta.id, meta);
        return meta;
      } finally {
        raw.fill(0);
      }
    } finally {
      if (key !== this.secret) key.fill(0);
    }
  }
  private async adopt(
    revision: Revision,
    id = revision.doc,
  ): Promise<Document> {
    const ticket = this.session,
      previous = this.adoptions.get(id) ?? Promise.resolve();
    const action = previous
      .catch(() => undefined)
      .then(async () => {
        if (ticket !== this.session) throw new Error("vault-locked");
        await this.wait(this.ensureAdvance(revision, id), ticket);
        const document = await this.wait(this.revisionDoc(revision), ticket);
        await this.wait(this.remember(document, revision), ticket);
        this.overlays.delete(id);
        this.overlays.set(id, revision);
        await this.wait(this.persistHeads(), ticket);
        return document;
      });
    this.adoptions.set(id, action);
    try {
      return await this.wait(action, ticket);
    } finally {
      if (this.adoptions.get(id) === action) this.adoptions.delete(id);
    }
  }
  async open(id: string): Promise<OpenDocument> {
    const ticket = this.session;
    this.require();
    let revision = this.overlays.get(id) ?? null;
    try {
      const remote = await this.wait(this.json("/api/documents/" + id), ticket);
      if (remote.head) {
        this.checkRevision(remote.head, id);
        for (const entry of remote.revisions ?? []) {
          this.checkRevision(entry.envelope, id);
          this.record(entry);
        }
        if (
          !(remote.revisions ?? []).some(
            (e: JournalEntry) => e.envelope.id === remote.head.id,
          )
        )
          throw new Error("revision-head-mismatch");
        revision = remote.head;
      } else if (revision) throw new Error("revision-head-rollback");
    } catch (error) {
      if (!(error instanceof TypeError)) throw error;
      this.offline = true;
    }
    if (revision) {
      const document = await this.wait(this.adopt(revision, id), ticket);
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
    const action = this.enrollmentUpdates
      .catch(() => undefined)
      .then(async () => {
        if (ticket !== this.session) throw new Error("vault-locked");
        const key = "grants:" + this.publication!.vault;
        const cached =
          (await this.wait(this.storage.get<DeviceGrant[]>(key), ticket)) ?? [];
        let incoming: DeviceGrant[];
        try {
          incoming = (await this.wait(this.json("/api/devices"), ticket))
            .devices;
          this.offline = false;
        } catch (error) {
          if (!(error instanceof TypeError)) throw error;
          incoming = cached;
          this.offline = true;
        }
        if (!Array.isArray(incoming))
          throw new Error("invalid-device-enrollment");
        const merged = new Map<string, DeviceGrant>();
        for (const grant of [...cached, ...this.grants.values()]) {
          if (!verify(grant, this.pack!.trust.owner))
            throw new Error("invalid-device-enrollment");
          const prior = merged.get(grant.id);
          if (!prior || prior.generation < grant.generation)
            merged.set(grant.id, grant);
        }
        const seen = new Set<string>();
        for (const grant of incoming) {
          if (
            !verify(grant, this.pack!.trust.owner) ||
            grant.schema !== "knovv-device.v1" ||
            !Number.isSafeInteger(grant.generation) ||
            grant.generation < 1 ||
            typeof grant.revoked !== "boolean" ||
            sha256(encoder.encode(grant.signing_public)) !== grant.id ||
            seen.has(grant.id)
          )
            throw new Error("invalid-device-enrollment");
          seen.add(grant.id);
          const prior = merged.get(grant.id);
          if (
            prior &&
            (prior.generation > grant.generation ||
              (prior.generation === grant.generation &&
                canonical(prior) !== canonical(grant)))
          )
            throw new Error("enrollment-rollback");
          merged.set(grant.id, grant);
        }
        await this.wait(this.storage.set(key, [...merged.values()]), ticket);
        this.grants = merged;
        const own = this.grants.get(this.device!.id);
        if (!own || own.revoked) throw new Error("device-not-enrolled");
      });
    this.enrollmentUpdates = action;
    await this.wait(action, ticket);
  }
  private async refreshPublication() {
    const ticket = this.session;
    const action = this.publicationUpdates
      .catch(() => undefined)
      .then(async () => {
        if (ticket !== this.session) throw new Error("vault-locked");
        await this.readPublication();
      });
    this.publicationUpdates = action;
    await this.wait(action, ticket);
  }

  private async readPublication(): Promise<void> {
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
      await this.wait(this.raw(pub.manifest), ticket);
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
        if (
          !Array.isArray(reply.revisions) ||
          !Number.isSafeInteger(reply.cursor) ||
          reply.cursor < this.cursor
        )
          throw new Error("invalid-sync-cursor");
        let previous = this.cursor;
        for (const entry of reply.revisions as JournalEntry[]) {
          if (!Number.isSafeInteger(entry.seq) || entry.seq <= previous)
            throw new Error("invalid-sync-sequence");
          this.record(entry);
          const current = this.overlays.get(entry.envelope.doc);
          if (!current || !this.ancestor(entry.envelope.id, current))
            await this.wait(this.adopt(entry.envelope), ticket);
          previous = entry.seq;
        }
        if (
          reply.cursor !== previous ||
          (reply.has_more && previous === this.cursor)
        )
          throw new Error("invalid-sync-cursor");
        this.cursor = reply.cursor;
        more = reply.has_more;
        await this.wait(this.persistHeads(), ticket);
      }
      this.offline = false;
    } catch (error) {
      if (!(error instanceof TypeError)) throw error;
      this.offline = true;
    }
  }
  private async persistHeads() {
    const ticket = this.session;
    const action = this.checkpoints
      .catch(() => undefined)
      .then(async () => {
        if (ticket !== this.session) throw new Error("vault-locked");
        const heads = new Set([...this.overlays.values()].map((r) => r.id));
        const ordered = [...this.known.values()].sort((a, b) => a.seq - b.seq),
          recent = new Set(ordered.slice(-10000).map((e) => e.envelope.id));
        const kept = ordered.filter(
          (e) =>
            e.seq > this.cursor ||
            heads.has(e.envelope.id) ||
            recent.has(e.envelope.id),
        );
        await this.wait(
          this.storage.set("journal:" + this.publication!.vault, {
            cursor: this.cursor,
            heads: [...this.overlays.values()],
            known: kept,
          }),
          ticket,
        );
        for (const entry of ordered)
          if (
            !heads.has(entry.envelope.id) &&
            !recent.has(entry.envelope.id) &&
            entry.seq <= this.cursor
          ) {
            this.known.delete(entry.envelope.id);
            if (entry.seq) this.sequences.delete(entry.seq);
          }
      });
    this.checkpoints = action;
    await this.wait(action, ticket);
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
      const acknowledged = await this.wait(
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
      this.record({
        seq: Number.isSafeInteger(acknowledged.seq) ? acknowledged.seq : 0,
        envelope: revision,
      });
      await this.wait(this.adopt(revision), ticket);
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
    return searchIndex(
      query,
      async (bucket) => {
        const oid = this.manifest!.search[bucket];
        return oid ? this.object<Record<string, number[]>>(oid) : {};
      },
      this.manifest!.search_prefix ?? 2,
    );
  }

  async overlaySearch(query: string) {
    const ticket = this.session,
      { terms } = await this.wait(import("./search"), ticket),
      tokens = terms(query),
      result: DocumentMeta[] = [];
    for (const revision of this.overlays.values()) {
      let meta =
        this.overlayDocs.get(revision.doc) ??
        (await this.wait(this.retainedMeta(revision), ticket));
      if (
        meta &&
        tokens.every((t) =>
          (meta!.title + " " + meta!.summary).toLowerCase().includes(t),
        )
      ) {
        result.push(meta);
        continue;
      }
      try {
        const doc = await this.wait(this.revisionDoc(revision), ticket);
        this.bodies.set(doc.id, { revision: revision.id, document: doc });
        if (!meta) {
          const { body: _, ...fields } = doc;
          meta = { ...fields, ordinal: -1, object: revision.object };
          this.overlayDocs.set(doc.id, meta);
        }
        if (
          tokens.every((t) =>
            (doc.title + " " + doc.body).toLowerCase().includes(t),
          )
        )
          result.push(meta);
      } catch (error) {
        if (!(this.offline && error instanceof TypeError)) throw error;
      }
    }
    return result;
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
