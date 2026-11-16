import {
  generateDevice,
  random,
  hex,
  encrypt,
  encoder,
  canonical,
  sha256,
  sign,
  wrap,
  recipient,
  protectDevice,
} from "../src/crypto";
import { Vault, MemoryStorage, compress } from "../src/vault";
import type {
  DeviceGrant,
  DevicePackage,
  JournalEntry,
  Revision,
} from "../src/contracts";
export async function revisionFixture() {
  const owner = generateDevice("knovv", "fixture-owner"),
    secret /* allow-secret: runtime-only */ = random(),
    vault = hex(random()),
    objects = new Map<string, Uint8Array>();
  const catalog = {
    schema: "knovv-catalog.v1",
    vault,
    epoch: 1,
    id_key: hex(random()),
    count: 0,
    metadata: [],
    lookup: {},
    search: {},
    navigation: [],
    receipts: [],
  };
  const raw = encrypt(
      secret,
      await compress(encoder.encode(canonical(catalog))),
      `knovv-object:${vault}:1`,
    ),
    oid = sha256(raw);
  objects.set(oid, raw);
  const publication = sign(
    {
      schema: "knovv-publication.v1",
      vault,
      epoch: 1,
      generation: 1,
      parent: null,
      manifest: oid,
      envelopes: [
        wrap(secret, recipient(owner), `knovv-publication.v1:${vault}:1`),
      ],
    },
    owner.signing_key,
  );
  const grant = sign(
    {
      schema: "knovv-device.v1",
      id: owner.id,
      signing_public: owner.signing_public,
      generation: 1,
      revoked: false,
    },
    owner.signing_key,
  ) as DeviceGrant;
  const pack: DevicePackage = {
    schema: "knovv-device-package.v1",
    credential: await protectDevice(owner, "synthetic revision passphrase"),
    trust: {
      owner: owner.signing_public,
      vault,
      generation: 1,
      head: sha256(encoder.encode(canonical(publication))),
    },
  };
  const state = {
    offline: false,
    devices: [grant],
    entries: [] as JournalEntry[],
    headOverride: null as Revision | null,
    syncOverride: null as any,
  };
  const fetcher = async (path: string, init: RequestInit = {}) => {
    if (state.offline) throw new TypeError("offline fixture");
    if (path === "/api/publication") return Response.json(publication);
    if (path === "/api/devices")
      return Response.json({ devices: state.devices });
    if (path.startsWith("/api/objects/")) {
      const id = path.split("/").at(-1)!;
      if (init.method === "PUT") {
        objects.set(id, new Uint8Array(init.body as ArrayBuffer));
        return Response.json({ saved: true });
      }
      const data = objects.get(id);
      return data
        ? new Response(new Uint8Array(data).buffer)
        : Response.json({ error: "missing" }, { status: 404 });
    }
    if (path === "/api/revisions") {
      const envelope = JSON.parse(init.body as string) as Revision,
        current = [...state.entries]
          .reverse()
          .find((e) => e.envelope.doc === envelope.doc)?.envelope;
      if ((current?.id ?? null) !== envelope.parent)
        return Response.json({ error: "revision-conflict" }, { status: 409 });
      state.entries.push({ seq: state.entries.length + 1, envelope });
      return Response.json({ saved: true, seq: state.entries.length });
    }
    if (path.startsWith("/api/documents/")) {
      const url = new URL(path, "https://fixture.invalid"),
        id = url.pathname.split("/").at(-1)!,
        before = Number(url.searchParams.get("before") ?? 0);
      const all = [...state.entries]
          .reverse()
          .filter((e) => e.envelope.doc === id && (!before || e.seq < before)),
        page = all.slice(0, 50);
      return Response.json({
        head: state.headOverride ?? page[0]?.envelope ?? null,
        revisions: page,
        next_before: all.length > 50 ? page.at(-1)!.seq : null,
      });
    }
    if (path.startsWith("/api/sync")) {
      if (state.syncOverride) return Response.json(state.syncOverride);
      const after = Number(
          new URL(path, "https://fixture.invalid").searchParams.get("after") ??
            0,
        ),
        all = state.entries.filter((e) => e.seq > after),
        page = all.slice(0, 100);
      return Response.json({
        revisions: page,
        cursor: page.at(-1)?.seq ?? after,
        has_more: all.length > 100,
      });
    }
    return Response.json({ error: "unexpected" }, { status: 500 });
  };
  async function client(storage = new MemoryStorage()) {
    const app = new Vault(storage, fetcher);
    await app.unlock(pack, "synthetic revision passphrase");
    return app;
  }
  return { owner, secret, pack, publication, state, objects, fetcher, client };
}
