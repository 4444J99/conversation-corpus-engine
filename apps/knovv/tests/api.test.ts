import { beforeAll, afterAll, describe, it, expect } from "vitest";
import { Miniflare } from "miniflare";
import { build } from "esbuild";
import { readFile } from "node:fs/promises";
import { generateDevice, sign, sha256, hex } from "../src/crypto";

const owner = generateDevice("knovv", "owner");
const device = generateDevice("knovv", "phone");
let mf: Miniflare;
const grant = (generation = 1, revoked = false) =>
  sign(
    {
      schema: "knovv-device.v1",
      id: device.id,
      signing_public: device.signing_public,
      generation,
      revoked,
    },
    owner.signing_key,
  );
async function request(path: string, method = "GET", body?: unknown) {
  return mf.dispatchFetch("http://localhost/api" + path, {
    method,
    headers: { "content-type": "application/json", origin: "http://localhost" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}
async function upload(text: string) {
  const data = new TextEncoder().encode(text),
    object = sha256(data);
  const auth = sign(
    { schema: "knovv-upload.v1", object, size: data.length, device: device.id },
    device.signing_key,
  );
  const r = await mf.dispatchFetch("http://localhost/api/objects/" + object, {
    method: "PUT",
    headers: {
      origin: "http://localhost",
      "x-knovv-authorization": btoa(JSON.stringify(auth)),
    },
    body: data,
  });
  return { object, r };
}
beforeAll(async () => {
  const compiled = await build({
    stdin: {
      contents: `import { api } from './server/api'; export default {fetch:(r,e)=>api(r,e,{email:'owner@example.test'})}`,
      resolveDir: process.cwd(),
    },
    bundle: true,
    write: false,
    format: "esm",
    platform: "browser",
  });
  // Installed workerd supports August 2026. API uses no later runtime features; production
  // Access authentication is tested separately and never bypassed by this fixture wrapper.
  mf = new Miniflare({
    modules: true,
    script: compiled.outputFiles[0].text,
    compatibilityDate: "2026-08-01",
    d1Databases: { DB: "knovv-test" },
    r2Buckets: ["BUCKET"],
    bindings: {
      OWNER_PUBLIC_KEY: owner.signing_public,
      OWNER_EMAIL: "owner@example.test",
      MAX_CIPHER_BYTES: "4096",
    },
  });
  const db = await mf.getD1Database("DB");
  await db.exec(
    (await readFile("server/schema.sql", "utf8")).replace(/\n/g, " "),
  );
});
afterAll(async () => {
  await mf?.dispose();
});

describe("encrypted journal against real local D1 and R2", () => {
  it("requires owner-signed enrollment and authenticates object uploads", async () => {
    expect((await upload("cipher-fixture-one")).r.status).toBe(403);
    expect(
      (await request("/devices", "POST", { ...grant(), generation: 99 }))
        .status,
    ).toBe(403);
    expect((await request("/devices", "POST", grant())).status).toBe(201);
    expect((await upload("cipher-fixture-one")).r.status).toBe(201);
  });
  it("persists a signed revision, rejects stale-parent edits and makes retries idempotent", async () => {
    const { object } = await upload("cipher-fixture-one");
    const revision = sign(
      {
        schema: "knovv-revision.v1",
        id: hex(crypto.getRandomValues(new Uint8Array(32))),
        doc: "a".repeat(64),
        parent: null,
        object,
        device: device.id,
        epoch: 1,
      },
      device.signing_key,
    );
    const saved = await request("/revisions", "POST", revision);
    expect(saved.status).toBe(201);
    expect((await request("/revisions", "POST", revision)).status).toBe(200);
    const stale = sign(
      { ...revision, id: "b".repeat(64), signature: undefined },
      device.signing_key,
    );
    expect((await request("/revisions", "POST", stale)).status).toBe(409);
    const history = (await (
      await request("/documents/" + revision.doc)
    ).json()) as any;
    expect(history.head.id).toBe(revision.id);
    const sync = (await (await request("/sync?after=0")).json()) as any;
    expect(sync.revisions.map((r: any) => r.envelope.id)).toContain(
      revision.id,
    );
    const objectReply = await request("/objects/" + object);
    expect(await objectReply.text()).toBe("cipher-fixture-one");
  });
  it("keeps only one winner for competing saves and preserves the other as an explicit conflict", async () => {
    const doc = "c".repeat(64),
      { object } = await upload("another encrypted fixture");
    const revisions = [0, 1].map(() =>
      sign(
        {
          schema: "knovv-revision.v1",
          id: hex(crypto.getRandomValues(new Uint8Array(32))),
          doc,
          parent: null,
          object,
          device: device.id,
          epoch: 1,
        },
        device.signing_key,
      ),
    );
    const replies = await Promise.all(
      revisions.map((r) => request("/revisions", "POST", r)),
    );
    expect(replies.map((r) => r.status).sort()).toEqual([201, 409]);
  });
  it("rejects cross-origin mutation, unavailable objects, and payloads above the application budget", async () => {
    const cross = await mf.dispatchFetch("http://localhost/api/devices", {
      method: "POST",
      headers: {
        origin: "https://other.example",
        "content-type": "application/json",
      },
      body: JSON.stringify(grant(2)),
    });
    expect(cross.status).toBe(403);
    const { r } = await upload("x".repeat(4097));
    expect(r.status).toBe(507);
    const missing = sign(
      {
        schema: "knovv-revision.v1",
        id: "d".repeat(64),
        doc: "e".repeat(64),
        parent: null,
        object: "f".repeat(64),
        device: device.id,
        epoch: 1,
      },
      device.signing_key,
    );
    expect((await request("/revisions", "POST", missing)).status).toBe(409);
  });
  it("rejects writes from revoked devices and cannot replay an older enrollment", async () => {
    expect((await request("/devices", "POST", grant(2, true))).status).toBe(
      201,
    );
    expect((await upload("revoked fixture")).r.status).toBe(403);
    expect((await request("/devices", "POST", grant(1, false))).status).toBe(
      409,
    );
  });
});
