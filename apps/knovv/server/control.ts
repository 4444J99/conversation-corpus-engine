import queries from "./control-queries.json";
import { canonical, encoder, sha256, verify } from "../src/crypto";
import type { Env } from "./api";
const ID = /^[0-9a-f]{64}$/;
const integer = (
  n: unknown,
  min = 0,
  max = Number.MAX_SAFE_INTEGER,
): n is number =>
  typeof n === "number" && Number.isSafeInteger(n) && n >= min && n <= max;
const reply = (value: unknown, status = 200) =>
  Response.json(value, {
    status,
    headers: {
      "cache-control": "private, no-store",
      "x-content-type-options": "nosniff",
    },
  });
export async function control(
  request: Request,
  env: Env,
  value: any,
  invoke: (path: string, value: unknown) => Promise<Response>,
): Promise<Response> {
  const origin = new URL(request.url).origin;
  if (
    !value ||
    Object.keys(value).sort().join(",") !==
      [
        "schema",
        "origin",
        "operation",
        "params",
        "issued",
        "nonce",
        "signature",
      ]
        .sort()
        .join(",") ||
    value.schema !== "knovv-control.v1" ||
    !integer(value.issued) ||
    !ID.test(value.nonce ?? "") ||
    !verify(value, env.OWNER_PUBLIC_KEY) ||
    value.origin !== origin ||
    Math.abs(Date.now() / 1000 - value.issued) > 300
  )
    return reply({ error: "invalid-owner-control" }, 403);
  const operation = value.operation;
  if (
    typeof operation !== "string" ||
    (!Object.hasOwn(queries, operation) && operation !== "initialize")
  )
    return reply({ error: "unsupported-control-operation" }, 400);
  if (
    !Array.isArray(value.params) ||
    value.params.some(
      (p: unknown) =>
        p !== null &&
        !(typeof p === "string" && p.length <= 256 * 1024) &&
        !integer(p),
    )
  )
    return reply({ error: "invalid-control-parameters" }, 400);
  const params = value.params as (string | number | null)[];
  const owner = await env.DB.prepare(
    "SELECT owner FROM knovv_identity WHERE id=1",
  ).first<{ owner: string }>();
  if (owner?.owner !== env.OWNER_PUBLIC_KEY)
    return reply({ error: "wrong-journal-owner" }, 503);
  if (operation === "initialize")
    return params.length
      ? reply({ error: "invalid-control-parameters" }, 400)
      : reply({ success: true, results: [] });
  const sql = queries[operation as keyof typeof queries];
  if (params.length !== (sql.match(/\?/g) ?? []).length)
    return reply({ error: "invalid-control-parameters" }, 400);
  if (operation === "owner-claim")
    return params[0] === env.OWNER_PUBLIC_KEY
      ? reply({ success: true, results: [] })
      : reply({ error: "wrong-journal-owner" }, 403);
  if (
    operation === "object-reserve" &&
    (!ID.test(String(params[0])) ||
      !integer(params[1], 0, 48 * 1024 * 1024) ||
      params[1] !== params[2] ||
      params[3] !== Number(env.MAX_CIPHER_BYTES ?? 536870912))
  )
    return reply({ error: "invalid-control-parameters" }, 400);
  if (
    ["object-state", "object-verified", "device-read"].includes(operation) &&
    !ID.test(String(params[0]))
  )
    return reply({ error: "invalid-control-parameters" }, 400);
  if (operation === "object-verified") {
    const stored = await env.DB.prepare("SELECT size FROM objects WHERE id=?")
      .bind(params[0])
      .first<{ size: number }>();
    const object = await env.BUCKET.head(String(params[0]));
    if (!stored || object?.size !== stored.size)
      return reply({ error: "ciphertext-unavailable" }, 409);
  }
  if (operation === "device-upsert") {
    let grant: any;
    try {
      grant = JSON.parse(String(params[4]));
    } catch {
      return reply({ error: "invalid-control-parameters" }, 400);
    }
    if (
      canonical([
        grant.id,
        grant.signing_public,
        grant.generation,
        Number(grant.revoked),
        canonical(grant),
      ]) !== canonical(params)
    )
      return reply({ error: "invalid-control-parameters" }, 400);
    const result = await invoke("devices", grant);
    return result.ok ? reply({ success: true, results: [] }) : result;
  }
  if (operation === "publication-first" || operation === "publication-cas") {
    let pub: any;
    const raw = String(params[operation === "publication-first" ? 4 : 3]);
    try {
      pub = JSON.parse(raw);
    } catch {
      return reply({ error: "invalid-control-parameters" }, 400);
    }
    const digest = sha256(encoder.encode(raw));
    if (canonical(pub) !== raw)
      return reply({ error: "invalid-control-parameters" }, 400);
    if (operation === "publication-first") {
      if (
        pub.parent !== null ||
        pub.generation !== 1 ||
        canonical(params) !==
          canonical([pub.generation, pub.epoch, pub.vault, digest, raw])
      )
        return reply({ error: "invalid-control-parameters" }, 400);
    } else if (
      (pub.parent !== params[4] && digest !== params[4]) ||
      canonical(params) !==
        canonical([
          pub.generation,
          pub.epoch,
          digest,
          raw,
          params[4],
          pub.vault,
        ])
    )
      return reply({ error: "invalid-control-parameters" }, 400);
    const result = await invoke("publication", pub);
    return result.ok ? reply({ success: true, results: [] }) : result;
  }
  if (
    operation === "object-page" &&
    (typeof params[0] !== "string" ||
      (params[0] !== "" && !ID.test(params[0])) ||
      !integer(params[1], 1, 1000))
  )
    return reply({ error: "invalid-control-parameters" }, 400);
  if (
    operation === "revision-page" &&
    (!integer(params[0]) ||
      !integer(params[1], Number(params[0])) ||
      !integer(params[2], 1, 1000))
  )
    return reply({ error: "invalid-control-parameters" }, 400);
  const result = await env.DB.prepare(sql)
    .bind(...params)
    .all();
  return reply({ success: true, results: result.results });
}
