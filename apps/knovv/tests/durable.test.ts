import {
  beforeAll,
  afterAll,
  describe,
  it,
  expect,
  expectTypeOf,
} from "vitest";
import { Miniflare } from "miniflare";
import { build } from "esbuild";
import { mkdtemp, rm } from "node:fs/promises";
import { resolve, join } from "node:path";
import {
  generateDevice,
  sign,
  sha256,
  encoder,
  canonical,
} from "../src/crypto";
import type { Database } from "../server/database";

const owner = generateDevice("knovv", "durable-owner");
const otherOwner = generateDevice("knovv", "other-durable-owner");
const device = generateDevice("knovv", "durable-phone");
const doc = "a".repeat(64);
let mf: Miniflare;
let script: string;
let state: string;
let object: string;
let head: string;
let headSequence: number;
let publication: ReturnType<typeof sign>;

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

function start(publicKey = owner.signing_public, name?: string) {
  return new Miniflare({
    name: "knovv-durable-test",
    modules: true,
    script,
    compatibilityDate: "2026-08-01",
    durableObjects: {
      JOURNAL: { className: "KnovvJournal", useSQLite: true },
      PROBE: { className: "AdapterProbe", useSQLite: true },
    },
    durableObjectsPersist: join(state, "objects"),
    r2Buckets: ["BUCKET"],
    r2Persist: join(state, "r2"),
    bindings: {
      OWNER_PUBLIC_KEY: publicKey,
      OWNER_EMAIL: "owner@example.test",
      MAX_CIPHER_BYTES: "4096",
      ...(name ? { TEST_OBJECT_NAME: name } : {}),
    },
  });
}

async function restart(publicKey = owner.signing_public, name?: string) {
  await mf.dispose();
  mf = start(publicKey, name);
}

function request(path: string, method = "GET", body?: unknown) {
  return mf.dispatchFetch("http://localhost/api" + path, {
    method,
    headers: { "content-type": "application/json", origin: "http://localhost" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

async function upload(text: string) {
  const data = encoder.encode(text),
    id = sha256(data);
  const auth = sign(
    {
      schema: "knovv-upload.v1",
      object: id,
      size: data.length,
      device: device.id,
    },
    device.signing_key,
  );
  const response = await mf.dispatchFetch(
    "http://localhost/api/objects/" + id,
    {
      method: "PUT",
      body: data,
      headers: {
        origin: "http://localhost",
        "x-knovv-authorization": btoa(JSON.stringify(auth)),
      },
    },
  );
  return { object: id, response };
}

function revision(id: string, parent: string | null, epoch = 1) {
  return sign(
    {
      schema: "knovv-revision.v1",
      id,
      doc,
      parent,
      object,
      device: device.id,
      epoch,
    },
    device.signing_key,
  );
}

beforeAll(async () => {
  const compiled = await build({
    entryPoints: ["tests/durable-fixture.ts"],
    bundle: true,
    write: false,
    format: "esm",
    platform: "browser",
    target: "es2022",
    external: ["cloudflare:workers"],
    loader: { ".sql": "text" },
  });
  script = compiled.outputFiles[0].text;
  state = await mkdtemp(resolve(".knovv-durable-test-"));
  mf = start();
});

afterAll(async () => {
  await mf?.dispose();
  if (state) await rm(state, { recursive: true, force: true });
});

describe("SQLite Durable Object journal in the real Workers runtime", () => {
  it("keeps the native D1 structural port and SQLite metadata/transaction semantics", async () => {
    expectTypeOf<D1Database>().toMatchTypeOf<Database>();
    const response = await mf.dispatchFetch("http://localhost/probe");
    expect(response.status).toBe(200);
    const result = (await response.json()) as any;
    expect(result.inserted.meta).toMatchObject({ changes: 1, last_row_id: 7 });
    expect(result.insertedSecond.meta).toMatchObject({
      changes: 1,
      last_row_id: 8,
    });
    expect(result.skipped.meta).toMatchObject({ changes: 0, last_row_id: 8 });
    expect(result.selected.meta.changes).toBe(0);
    expect(result.selected.results).toEqual([
      { id: 7, value: "first" },
      { id: 8, value: "second" },
    ]);
    expect(result.one).toEqual({ id: 7, value: "first" });
    expect(result.missing).toBeNull();
    expect(result.rolledBack).toBe(true);
    expect(result.asyncRefused).toBe(true);
    expect(result.escapeRefused).toBe(true);
    expect(result.committed.result.meta).toMatchObject({
      changes: 1,
      last_row_id: 10,
    });
    expect(result.committed.row).toEqual({ id: 10, value: "committed" });
    expect(result.items.map((row: any) => row.id)).toEqual([7, 8, 10]);
    expect(result.audit).toEqual([
      { id: 1007, item: 7 },
      { id: 1008, item: 8 },
      { id: 1010, item: 10 },
    ]);
  });

  it("requires signed enrollment and preserves upload quota accounting on retries", async () => {
    expect((await upload("durable encrypted fixture")).response.status).toBe(
      403,
    );
    expect(
      (await request("/devices", "POST", { ...grant(), generation: 99 }))
        .status,
    ).toBe(403);
    expect((await request("/devices", "POST", grant())).status).toBe(201);
    expect((await request("/devices", "POST", grant())).status).toBe(200);
    const uploaded = await upload("durable encrypted fixture");
    expect(uploaded.response.status).toBe(201);
    object = uploaded.object;
    expect((await upload("durable encrypted fixture")).response.status).toBe(
      200,
    );
    const status = (await (await request("/status")).json()) as any;
    expect(status.cipher_bytes).toBe(
      encoder.encode("durable encrypted fixture").length,
    );
    expect((await upload("x".repeat(4097))).response.status).toBe(507);
    expect(await (await request("/status")).json()).toEqual(status);
    const competing = await Promise.all([
      upload("a".repeat(2500)),
      upload("b".repeat(2500)),
    ]);
    expect(competing.map(({ response }) => response.status).sort()).toEqual([
      201, 507,
    ]);
    const afterRace = (await (await request("/status")).json()) as any;
    expect(afterRace.cipher_bytes).toBe(status.cipher_bytes + 2500);
  });

  it("publishes once and enforces publication parent, owner signature, epoch, and generation", async () => {
    publication = sign(
      {
        schema: "knovv-publication.v1",
        vault: "b".repeat(64),
        generation: 1,
        epoch: 1,
        parent: null,
        manifest: object,
        envelopes: ["synthetic sealed envelope"],
      },
      owner.signing_key,
    );
    expect(
      (await request("/publication", "POST", { ...publication, epoch: 2 }))
        .status,
    ).toBe(403);
    expect((await request("/publication", "POST", publication)).status).toBe(
      201,
    );
    expect((await request("/publication", "POST", publication)).status).toBe(
      200,
    );
    const candidates = ["candidate-one", "candidate-two"].map((envelope) =>
      sign(
        {
          ...publication,
          generation: 2,
          parent: sha256(encoder.encode(canonical(publication))),
          envelopes: [envelope],
          signature: undefined,
        },
        owner.signing_key,
      ),
    );
    const responses = await Promise.all(
      candidates.map((value) => request("/publication", "POST", value)),
    );
    expect(responses.map((response) => response.status).sort()).toEqual([
      201, 409,
    ]);
    publication =
      candidates[responses.findIndex((response) => response.status === 201)];
    expect(await (await request("/publication")).json()).toEqual(publication);
    const wrongEpoch = sign(
      {
        ...publication,
        generation: 3,
        epoch: 3,
        parent: sha256(encoder.encode(canonical(publication))),
        signature: undefined,
      },
      owner.signing_key,
    );
    expect((await request("/publication", "POST", wrongEpoch)).status).toBe(
      409,
    );
  });

  it("returns revision row IDs, preserves retry identity, and atomically selects one competing head", async () => {
    const first = revision("c".repeat(64), null);
    const saved = await request("/revisions", "POST", first);
    expect(saved.status).toBe(201);
    expect(await saved.json()).toMatchObject({ seq: 1 });
    const repeat = await request("/revisions", "POST", first);
    expect(repeat.status).toBe(200);
    expect(await repeat.json()).toMatchObject({ seq: 1 });
    const second = revision("d".repeat(64), first.id);
    const secondSaved = await request("/revisions", "POST", second);
    expect(secondSaved.status).toBe(201);
    // documents remains rowid 1, while the actual journal sequence advances.
    expect(await secondSaved.json()).toMatchObject({ seq: 2 });
    const candidates = ["e".repeat(64), "f".repeat(64)].map((id) =>
      revision(id, second.id),
    );
    const responses = await Promise.all(
      candidates.map((value) => request("/revisions", "POST", value)),
    );
    expect(responses.map((response) => response.status).sort()).toEqual([
      201, 409,
    ]);
    const winner = responses.findIndex((response) => response.status === 201);
    head = candidates[winner].id;
    headSequence = ((await responses[winner].json()) as any).seq;
    expect(headSequence).toBeGreaterThan(2);
    const history = (await (await request("/documents/" + doc)).json()) as any;
    expect(history.head.id).toBe(head);
    expect(history.revisions.map((r: any) => r.seq)).toEqual([
      headSequence,
      2,
      1,
    ]);
    expect(
      (await request("/revisions", "POST", revision(head, null))).status,
    ).toBe(409);
    expect(
      (await request("/revisions", "POST", revision("1".repeat(64), head, 2)))
        .status,
    ).toBe(409);
  });

  it("recovers publication, journal, devices, quota and ciphertext after disposing the runtime", async () => {
    const status = await (await request("/status")).json();
    await restart();
    expect(await (await request("/status")).json()).toEqual(status);
    expect(await (await request("/devices")).json()).toEqual({
      devices: [grant()],
    });
    expect(await (await request("/publication")).json()).toEqual(publication);
    const history = (await (await request("/documents/" + doc)).json()) as any;
    expect(history.head.id).toBe(head);
    const sync = (await (await request("/sync?after=2")).json()) as any;
    expect(sync.revisions.map((entry: any) => entry.envelope.id)).toEqual([
      head,
    ]);
    expect(sync.cursor).toBe(headSequence);
    expect(await (await request("/objects/" + object)).text()).toBe(
      "durable encrypted fixture",
    );
    expect((await request("/revisions", "POST", history.head)).status).toBe(
      200,
    );
  });

  it("routes owner keys to independent workspaces and refuses reassignment of an existing owner marker", async () => {
    await restart(otherOwner.signing_public);
    expect(await (await request("/devices")).json()).toEqual({ devices: [] });
    expect((await request("/publication")).status).toBe(404);
    expect((await request("/objects/" + object)).status).toBe(404);
    expect((await request("/devices", "POST", grant())).status).toBe(403);
    const fresh = (await (await request("/status")).json()) as any;
    expect(fresh.cipher_bytes).toBe(0);
    await restart(otherOwner.signing_public, owner.signing_public);
    expect((await request("/status")).status).toBe(503);
    await restart();
    expect(await (await request("/devices")).json()).toEqual({
      devices: [grant()],
    });
  });

  it("retains revocation CAS and offers no Durable Object fetch handler", async () => {
    expect((await request("/devices", "POST", grant(2, true))).status).toBe(
      201,
    );
    expect((await request("/devices", "POST", grant(1, false))).status).toBe(
      409,
    );
    expect((await upload("revoked fixture")).response.status).toBe(403);
    const response = await mf.dispatchFetch(
      "http://localhost/fetch-is-not-exposed",
    );
    expect(response.status).toBe(500);
  });

  it("fails closed when the owner public key is not configured", async () => {
    await restart("invalid-owner-key");
    const response = await request("/status");
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: "trust-not-configured" });
  });
});
