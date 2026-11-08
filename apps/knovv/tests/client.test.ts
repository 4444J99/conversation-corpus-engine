import { describe, it, expect } from "vitest";
import {
  generateDevice,
  protectDevice,
  random,
  hex,
  sign,
  wrap,
  recipient,
  encrypt,
  encoder,
  canonical,
  sha256,
} from "../src/crypto";
import { Vault, MemoryStorage, compress } from "../src/vault";
import { searchIndex } from "../src/search";
import type { DevicePackage, Document } from "../src/contracts";

describe("browser knowledge workflow", () => {
  it("keeps private content out of persistent storage and transport plaintext, and retains an encrypted failed save", async () => {
    const device = generateDevice("knovv", "fixture"),
      vaultId = hex(random()),
      secret = random(); // allow-secret: runtime-only
    const storage = new MemoryStorage(),
      objects = new Map<string, Uint8Array>();
    async function object(value: unknown) {
      const raw = encrypt(
          secret,
          await compress(encoder.encode(canonical(value))),
          `knovv-object:${vaultId}:1`,
        ),
        id = sha256(raw);
      objects.set(id, raw);
      return id;
    }
    const doc: Document = {
      id: "a".repeat(64),
      title: "SYNTHETIC PRIVATE LATTICE",
      body: "A private retained thought.",
      kind: "wiki",
      summary: "A private summary",
      author: "owner",
      authorship_basis: "declared",
      status: "draft",
      created: "",
      updated: "",
      sources: [],
      links: [],
      tags: [],
    };
    const cipher = await object(doc),
      meta = { ...doc, body: undefined, object: cipher, ordinal: 0 };
    const metadata = await object([meta]);
    const lookup = await object({ [doc.id]: 0 });
    const manifestId = await object({
      schema: "knovv-catalog.v1",
      vault: vaultId,
      epoch: 1,
      id_key: hex(random()),
      count: 1,
      metadata: [metadata],
      lookup: { aa: lookup },
      navigation: [meta],
      search: {},
      receipts: [],
    });
    const publication = sign(
      {
        schema: "knovv-publication.v1",
        vault: vaultId,
        generation: 1,
        epoch: 1,
        parent: null,
        manifest: manifestId,
        envelopes: [
          wrap(secret, recipient(device), `knovv-publication.v1:${vaultId}:1`),
        ],
      },
      device.signing_key,
    );
    const grant = sign(
      {
        schema: "knovv-device.v1",
        id: device.id,
        signing_public: device.signing_public,
        generation: 1,
        revoked: false,
      },
      device.signing_key,
    );
    let offline = false;
    const sent: string[] = [];
    const fetcher = async (path: string, init?: RequestInit) => {
      if (offline) throw new TypeError("network unavailable");
      if (init?.body)
        sent.push(
          typeof init.body === "string" ? init.body : "ciphertext bytes",
        );
      if (path === "/api/publication") return Response.json(publication);
      if (path === "/api/devices") return Response.json({ devices: [grant] });
      if (path.startsWith("/api/sync"))
        return Response.json({ revisions: [], cursor: 0, has_more: false });
      if (path.startsWith("/api/documents/"))
        return Response.json({ head: null, revisions: [] });
      if (path.startsWith("/api/objects/")) {
        const id = path.split("/").at(-1)!;
        if (init?.method === "PUT") return Response.json({ saved: true });
        const bytes = objects.get(id);
        return new Response(bytes ? new Uint8Array(bytes).buffer : null);
      }
      return Response.json({ error: "unexpected" }, { status: 500 });
    };
    const pack: DevicePackage = {
      schema: "knovv-device-package.v1",
      credential: await protectDevice(device, "test-only browser passphrase"),
      trust: {
        owner: device.signing_public,
        vault: vaultId,
        generation: 1,
        head: sha256(encoder.encode(canonical(publication))),
      },
    };
    const app = new Vault(storage, fetcher);
    await app.unlock(pack, "test-only browser passphrase");
    expect((await app.open(doc.id)).document.body).toBe(doc.body);
    offline = true;
    const pending = await app.save(
      { ...doc, body: "SYNTHETIC PRIVATE EDIT" },
      null,
    );
    expect(pending.state).toBe("local");
    expect(JSON.stringify([...storage.values.values()])).not.toContain(
      "SYNTHETIC PRIVATE",
    );
    expect(sent.join("")).not.toContain("SYNTHETIC PRIVATE");
    app.lock();
    expect(app.unlocked).toBe(false);
    await app.unlock(pack, "test-only browser passphrase");
    expect((await app.drafts())[0].document.body).toBe(
      "SYNTHETIC PRIVATE EDIT",
    );
    offline = false;
    let release!: () => void;
    const gate = new Promise<void>((r) => {
      release = r;
    });
    const delayed = new Vault(new MemoryStorage(), async (path, init) => {
      if (path === "/api/publication") await gate;
      return fetcher(path, init);
    });
    const unlocking = delayed.unlock(pack, "test-only browser passphrase");
    // A lock during credential derivation/network loading must win.
    delayed.lock();
    release();
    await expect(unlocking).rejects.toThrow("vault-locked");
    expect(delayed.unlocked).toBe(false);
    let releaseWrite!: () => void,
      entered!: () => void,
      secondEntered!: () => void;
    const writeGate = new Promise<void>((r) => {
        releaseWrite = r;
      }),
      firstEntered = new Promise<void>((r) => {
        entered = r;
      }),
      secondWrite = new Promise<void>((r) => {
        secondEntered = r;
      });
    class DelayedDraftStorage extends MemoryStorage {
      writes = 0;
      async set(key: string, value: unknown) {
        if (key.startsWith("draft:")) {
          this.writes++;
          if (this.writes === 1) {
            entered();
            await writeGate;
          } else secondEntered();
        }
        return super.set(key, value);
      }
    }
    const racing = new Vault(new DelayedDraftStorage(), fetcher);
    await racing.unlock(pack, "test-only browser passphrase");
    const older = racing.stage({ ...doc, body: "older draft" }, null);
    await firstEntered;
    const newer = racing.stage({ ...doc, body: "newer draft" }, null);
    await Promise.race([
      secondWrite,
      new Promise<void>((r) => setTimeout(r, 200)),
    ]);
    releaseWrite();
    await Promise.all([older, newer]);
    expect((await racing.drafts())[0].document.body).toBe("newer draft");
    racing.lock();
  }, 30000);
  it("searches every query term across encrypted posting buckets and reports exact candidate counts", async () => {
    const bucket = { lattice: [1, 3, 5], source: [3, 5, 8] };
    const result = await searchIndex("lattice source", async () => bucket);
    expect(result.ordinals).toEqual([3, 5]);
    expect(result.total).toBe(2);
    expect(result.plan).toBe("and");
    const fallback = await searchIndex("lattice missing", async () => bucket);
    expect(fallback.plan).toBe("or");
    expect(fallback.ordinals).toEqual([1, 3, 5]);
    expect((await searchIndex("constructor", async () => ({}))).total).toBe(0);
  });
});
