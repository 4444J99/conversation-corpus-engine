import { ByteCache } from "./byte-cache";
import { sha256 } from "./crypto";
import { inflate, readStream } from "./vault";
import { searchIndex, type Postings } from "./search";
import type { Manifest, Publication } from "./contracts";
import { BrowserStorage } from "./storage";
import { PackedReader } from "./packed";
const storage = new BrowserStorage();
let reader: PackedReader | null = null;
async function loadCipher(oid: string) {
  let raw = await storage.get<Uint8Array>("object:" + oid);
  if (!raw) {
    const reply = await fetch("/api/objects/" + oid, {
      credentials: "same-origin",
    });
    if (!reply.ok) throw new Error("search-index-unavailable");
    raw = await readStream(reply.body, 48 * 1024 * 1024);
    if (sha256(raw) !== oid) throw new Error("search-index-digest");
    try {
      await storage.putCipher(oid, raw);
    } catch {
      /* Cache errors do not discard a verified read. */
    }
  }
  if (sha256(raw) !== oid) throw new Error("search-index-digest");
  return raw;
}
let secret: Uint8Array | null = null, // allow-secret: runtime-only
  publication: Publication | null = null,
  manifest: Manifest | null = null;
const buckets = new ByteCache<string, Postings>(8 * 1024 * 1024);
self.onmessage = async (event: MessageEvent) => {
  const { type, id, query } = event.data;
  if (type === "init") {
    reader?.close();
    secret?.fill(0);
    secret = event.data.secret; // allow-secret: runtime-only
    publication = event.data.publication;
    manifest = event.data.manifest;
    reader = new PackedReader(
      secret!,
      `knovv-object:${publication!.vault}:${publication!.epoch}`,
      manifest!.resolve,
      loadCipher,
      inflate,
    );
    buckets.clear();
    return;
  }
  if (type === "lock") {
    reader?.close();
    reader = null;
    secret?.fill(0);
    secret = null; // allow-secret: runtime-only
    publication = null;
    manifest = null;
    buckets.clear();
    return;
  }
  if (type !== "search" || !secret || !publication || !manifest) return;
  try {
    const result = await searchIndex(
      query,
      async (bucket) => {
        if (buckets.has(bucket)) return buckets.get(bucket)!;
        const oid = manifest!.search[bucket];
        if (!oid) return {};
        const index = await reader!.value<Postings>(oid);
        buckets.set(bucket, index);
        return index;
      },
      manifest!.search_prefix ?? 2,
    );
    self.postMessage({ id, result });
  } catch {
    self.postMessage({
      id,
      error: "Search is unavailable. Your sources have not changed.",
    });
  }
};
