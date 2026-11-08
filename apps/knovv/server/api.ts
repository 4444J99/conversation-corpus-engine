import { canonical, encoder, sha256, verify, MAX_OBJECT } from "../src/crypto";
import type { DeviceGrant, Revision, Publication } from "../src/contracts";

export interface Env {
  DB: D1Database;
  BUCKET: R2Bucket;
  ASSETS: Fetcher;
  OWNER_PUBLIC_KEY: string;
  OWNER_EMAIL: string;
  ACCESS_AUD: string;
  MAX_CIPHER_BYTES?: string;
}
export interface Principal {
  email: string;
}
const ID = /^[0-9a-f]{64}$/;
class Failure extends Error {
  constructor(
    public status: number,
    public code: string,
  ) {
    super(code);
  }
}
const fail = (status: number, code: string): never => {
  throw new Failure(status, code);
};
export function json(value: unknown, status = 200): Response {
  return Response.json(value, {
    status,
    headers: {
      "cache-control": "private, no-store",
      "x-content-type-options": "nosniff",
    },
  });
}
function fields(value: any, names: string[]) {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    Object.keys(value).sort().join(",") !== names.sort().join(",")
  )
    fail(400, "invalid-envelope");
}
function integer(
  value: unknown,
  min = 0,
  max = Number.MAX_SAFE_INTEGER,
): value is number {
  return (
    typeof value === "number" &&
    Number.isSafeInteger(value) &&
    value >= min &&
    value <= max
  );
}
async function boundedBody(
  request: Request,
  limit: number,
): Promise<Uint8Array> {
  if (Number(request.headers.get("content-length") ?? 0) > limit)
    fail(413, "request-too-large");
  if (!request.body) return new Uint8Array();
  const reader = request.body.getReader();
  let size = 0;
  const parts: Uint8Array[] = [];
  try {
    while (true) {
      const next = await reader.read();
      if (next.done) break;
      size += next.value.length;
      if (size > limit) {
        await reader.cancel();
        fail(413, "request-too-large");
      }
      parts.push(next.value);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const p of parts) {
    bytes.set(p, offset);
    offset += p.length;
  }
  return bytes;
}
async function body(request: Request): Promise<any> {
  try {
    return JSON.parse(
      new TextDecoder("utf-8", { fatal: true }).decode(
        await boundedBody(request, 256 * 1024),
      ),
    );
  } catch (error) {
    if (error instanceof Failure) throw error;
    return fail(400, "invalid-json");
  }
}
async function enrolled(env: Env, id: string): Promise<DeviceGrant> {
  if (!ID.test(id)) fail(403, "device-not-enrolled");
  const row = await env.DB.prepare(
    "SELECT envelope FROM devices WHERE id=? AND revoked=0",
  )
    .bind(id)
    .first<{ envelope: string }>();
  if (!row) fail(403, "device-not-enrolled");
  return JSON.parse(row!.envelope);
}
function offset(url: URL, key: string, max = Number.MAX_SAFE_INTEGER) {
  const s = url.searchParams.get(key) ?? "0";
  if (!/^\d+$/.test(s) || !integer(Number(s), 0, max))
    fail(400, "invalid-cursor");
  return Number(s);
}

export async function api(
  request: Request,
  env: Env,
  principal: Principal | null,
): Promise<Response> {
  try {
    if (!principal || principal.email !== env.OWNER_EMAIL)
      fail(403, "not-authorized");
    if (!ID.test(env.OWNER_PUBLIC_KEY)) fail(503, "trust-not-configured");
    const url = new URL(request.url),
      path = url.pathname.replace(/^\/api/, "");
    const method = request.method;
    if (
      !["GET", "HEAD"].includes(method) &&
      request.headers.get("origin") !== url.origin
    )
      fail(403, "origin-refused");
    if (path === "/status" && method === "GET") {
      const usage = await env.DB.prepare(
        "SELECT bytes FROM usage WHERE id=1",
      ).first<{ bytes: number }>();
      return json({
        schema: "knovv-status.v1",
        cipher_bytes: usage?.bytes ?? 0,
        budget: Number(env.MAX_CIPHER_BYTES ?? 536870912),
      });
    }
    if (path === "/devices" && method === "GET") {
      const rows = await env.DB.prepare(
        "SELECT envelope FROM devices ORDER BY id",
      ).all<{ envelope: string }>();
      return json({ devices: rows.results.map((r) => JSON.parse(r.envelope)) });
    }
    if (path === "/devices" && method === "POST") {
      const grant = (await body(request)) as DeviceGrant;
      fields(grant, [
        "schema",
        "id",
        "signing_public",
        "generation",
        "revoked",
        "signature",
      ]);
      if (
        grant.schema !== "knovv-device.v1" ||
        !ID.test(grant.id) ||
        !ID.test(grant.signing_public) ||
        !integer(grant.generation, 1) ||
        typeof grant.revoked !== "boolean"
      )
        fail(400, "invalid-grant");
      if (!verify(grant, env.OWNER_PUBLIC_KEY))
        fail(403, "invalid-owner-signature");
      if (sha256(encoder.encode(grant.signing_public)) !== grant.id)
        fail(400, "device-key-mismatch");
      const old = await env.DB.prepare(
        "SELECT envelope FROM devices WHERE id=?",
      )
        .bind(grant.id)
        .first<{ envelope: string }>();
      const raw = canonical(grant);
      if (old?.envelope === raw) return json({ saved: true }, 200);
      const result = await env.DB.prepare(
        "INSERT INTO devices(id,signing_public,generation,revoked,envelope) VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET generation=excluded.generation,revoked=excluded.revoked,envelope=excluded.envelope WHERE excluded.generation>devices.generation AND excluded.signing_public=devices.signing_public",
      )
        .bind(
          grant.id,
          grant.signing_public,
          grant.generation,
          Number(grant.revoked),
          raw,
        )
        .run();
      if (!result.meta.changes) fail(409, "stale-device-grant");
      return json({ saved: true }, 201);
    }
    const object = path.match(/^\/objects\/([0-9a-f]{64})$/)?.[1];
    if (object && method === "PUT") {
      const header = request.headers.get("x-knovv-authorization") ?? "";
      if (header.length > 8192) fail(400, "invalid-upload");
      let auth: any;
      try {
        auth = JSON.parse(atob(header));
      } catch {
        fail(403, "invalid-upload-signature");
      }
      fields(auth, ["schema", "object", "size", "device", "signature"]);
      if (
        auth.schema !== "knovv-upload.v1" ||
        auth.object !== object ||
        !integer(auth.size, 1, MAX_OBJECT)
      )
        fail(400, "invalid-upload");
      const grant = await enrolled(env, auth.device);
      if (!verify(auth, grant.signing_public))
        fail(403, "invalid-upload-signature");
      const bytes = await boundedBody(request, MAX_OBJECT);
      if (bytes.length !== auth.size || sha256(bytes) !== object)
        fail(400, "ciphertext-digest-mismatch");
      const old = await env.DB.prepare(
        "SELECT size,uploaded FROM objects WHERE id=?",
      )
        .bind(object)
        .first<{ size: number; uploaded: number }>();
      if (old?.uploaded) return json({ object, saved: true }, 200);
      const budget = Number(env.MAX_CIPHER_BYTES ?? 536870912);
      if (!integer(budget, 1, 8 * 1024 * 1024 * 1024))
        fail(503, "budget-not-configured");
      if (!old) {
        const reserve = await env.DB.prepare(
          "INSERT OR IGNORE INTO objects(id,size) SELECT ?,? WHERE (SELECT bytes FROM usage WHERE id=1)+?<=? AND EXISTS(SELECT 1 FROM devices WHERE id=? AND revoked=0)",
        )
          .bind(object, bytes.length, bytes.length, budget, auth.device)
          .run();
        if (!reserve.meta.changes) {
          const collision = await env.DB.prepare(
            "SELECT id FROM objects WHERE id=?",
          )
            .bind(object)
            .first();
          if (!collision) fail(507, "storage-budget-exceeded");
        }
      }
      // An interrupted upload leaves a bounded reservation. Retrying the same digest completes it.
      await env.BUCKET.put(object, bytes, {
        httpMetadata: { contentType: "application/octet-stream" },
      });
      await env.DB.prepare("UPDATE objects SET uploaded=1 WHERE id=?")
        .bind(object)
        .run();
      return json({ object, saved: true }, 201);
    }
    if (object && method === "GET") {
      const known = await env.DB.prepare(
        "SELECT id FROM objects WHERE id=? AND uploaded=1",
      )
        .bind(object)
        .first();
      if (!known) fail(404, "object-unavailable");
      const value = await env.BUCKET.get(object);
      if (!value) fail(503, "object-unavailable");
      return new Response(value!.body, {
        headers: {
          "content-type": "application/octet-stream",
          "cache-control": "private, no-store",
          "x-content-type-options": "nosniff",
        },
      });
    }
    if (path === "/revisions" && method === "POST") {
      const revision = (await body(request)) as Revision;
      fields(revision, [
        "schema",
        "id",
        "doc",
        "parent",
        "object",
        "device",
        "epoch",
        "signature",
      ]);
      if (
        revision.schema !== "knovv-revision.v1" ||
        ![revision.id, revision.doc, revision.object, revision.device].every(
          (v) => ID.test(v),
        ) ||
        !(revision.parent === null || ID.test(revision.parent)) ||
        !integer(revision.epoch, 1)
      )
        fail(400, "invalid-revision");
      const grant = await enrolled(env, revision.device);
      if (!verify(revision, grant.signing_public))
        fail(403, "invalid-device-signature");
      const raw = canonical(revision),
        existing = await env.DB.prepare(
          "SELECT envelope,seq FROM revisions WHERE id=?",
        )
          .bind(revision.id)
          .first<{ envelope: string; seq: number }>();
      if (existing) {
        if (existing.envelope !== raw) fail(409, "revision-id-collision");
        return json({ saved: true, seq: existing.seq }, 200);
      }
      const result = await env.DB.prepare(
        "INSERT OR IGNORE INTO revisions(id,doc,parent,object,device,envelope) SELECT ?,?,?,?,?,? WHERE COALESCE((SELECT head FROM documents WHERE id=?),'')=? AND EXISTS(SELECT 1 FROM objects WHERE id=? AND uploaded=1) AND EXISTS(SELECT 1 FROM devices WHERE id=? AND revoked=0) AND ?=COALESCE((SELECT epoch FROM publications WHERE id=1),1)",
      )
        .bind(
          revision.id,
          revision.doc,
          revision.parent,
          revision.object,
          revision.device,
          raw,
          revision.doc,
          revision.parent ?? "",
          revision.object,
          revision.device,
          revision.epoch,
        )
        .run();
      if (!result.meta.changes) fail(409, "revision-conflict");
      return json({ saved: true, seq: result.meta.last_row_id }, 201);
    }
    const doc = path.match(/^\/documents\/([0-9a-f]{64})$/)?.[1];
    if (doc && method === "GET") {
      const before = offset(url, "before"),
        rows = await env.DB.prepare(
          "SELECT seq,envelope FROM revisions WHERE doc=? AND (?=0 OR seq<?) ORDER BY seq DESC LIMIT 51",
        )
          .bind(doc, before, before)
          .all<{ seq: number; envelope: string }>();
      const found = rows.results.slice(0, 50);
      return json({
        head: found.length ? JSON.parse(found[0].envelope) : null,
        revisions: found.map((r) => ({
          seq: r.seq,
          envelope: JSON.parse(r.envelope),
        })),
        next_before: rows.results.length > 50 ? found.at(-1)!.seq : null,
      });
    }
    if (path === "/sync" && method === "GET") {
      const after = offset(url, "after");
      const rows = await env.DB.prepare(
        "SELECT seq,envelope FROM revisions WHERE seq>? ORDER BY seq LIMIT 101",
      )
        .bind(after)
        .all<{ seq: number; envelope: string }>();
      const found = rows.results.slice(0, 100);
      return json({
        revisions: found.map((r) => ({
          seq: r.seq,
          envelope: JSON.parse(r.envelope),
        })),
        cursor: found.at(-1)?.seq ?? after,
        has_more: rows.results.length > 100,
      });
    }
    if (path === "/publication" && method === "GET") {
      const row = await env.DB.prepare(
        "SELECT envelope FROM publications WHERE id=1",
      ).first<{ envelope: string }>();
      if (!row) fail(404, "publication-unavailable");
      return json(JSON.parse(row!.envelope));
    }
    if (path === "/publication" && method === "POST") {
      const pub = (await body(request)) as Publication;
      fields(pub, [
        "schema",
        "vault",
        "generation",
        "epoch",
        "parent",
        "manifest",
        "envelopes",
        "signature",
      ]);
      if (
        pub.schema !== "knovv-publication.v1" ||
        !ID.test(pub.vault) ||
        !ID.test(pub.manifest) ||
        !integer(pub.generation, 1) ||
        !integer(pub.epoch, 1) ||
        !(pub.parent === null || ID.test(pub.parent)) ||
        !Array.isArray(pub.envelopes) ||
        !pub.envelopes.length ||
        pub.envelopes.length > 64 ||
        pub.envelopes.some((s) => typeof s !== "string" || s.length > 20000)
      )
        fail(400, "invalid-publication");
      if (!verify(pub, env.OWNER_PUBLIC_KEY))
        fail(403, "invalid-owner-signature");
      const uploaded = await env.DB.prepare(
        "SELECT id FROM objects WHERE id=? AND uploaded=1",
      )
        .bind(pub.manifest)
        .first();
      if (!uploaded) fail(409, "manifest-unavailable");
      const raw = canonical(pub),
        digest = sha256(encoder.encode(raw)),
        old = await env.DB.prepare(
          "SELECT digest FROM publications WHERE id=1",
        ).first<{ digest: string }>();
      if (old?.digest === digest) return json({ saved: true }, 200);
      const result = await env.DB.prepare(
        "INSERT INTO publications(id,generation,epoch,vault,digest,envelope) SELECT 1,?,?,?,?,? WHERE ?=1 AND ? IS NULL OR EXISTS(SELECT 1 FROM publications WHERE id=1 AND generation+1=? AND digest=? AND vault=? AND (?=epoch OR ?=epoch+1)) ON CONFLICT(id) DO UPDATE SET generation=excluded.generation,epoch=excluded.epoch,digest=excluded.digest,envelope=excluded.envelope WHERE publications.generation+1=excluded.generation AND publications.digest=? AND publications.vault=excluded.vault AND (excluded.epoch=publications.epoch OR excluded.epoch=publications.epoch+1)",
      )
        .bind(
          pub.generation,
          pub.epoch,
          pub.vault,
          digest,
          raw,
          pub.generation,
          pub.parent,
          pub.generation,
          pub.parent,
          pub.vault,
          pub.epoch,
          pub.epoch,
          pub.parent,
        )
        .run();
      if (!result.meta.changes) fail(409, "publication-conflict");
      return json({ saved: true }, 201);
    }
    return json({ error: "not-found" }, 404);
  } catch (error) {
    if (error instanceof Failure)
      return json({ error: error.code }, error.status);
    // Never expose storage exceptions, input bodies or credential-bearing headers.
    return json({ error: "storage-unavailable" }, 503);
  }
}
