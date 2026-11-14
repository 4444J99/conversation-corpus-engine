import { describe, it, expect } from "vitest";
import { generateDevice, sign } from "../src/crypto";
import { revisionFixture } from "./revision-fixture";
import { MemoryStorage } from "../src/storage";
describe("accepted revision and enrollment high-water marks", () => {
  it("rejects a signed older head and an older revision assigned a forged later sequence", async () => {
    const f = await revisionFixture(),
      storage = new MemoryStorage(),
      app = await f.client(storage),
      doc = app.newDocument("wiki");
    doc.title = "A retained note";
    doc.body = "OLD";
    const old = await app.save(doc, null);
    const latest = await app.save({ ...doc, body: "NEW" }, old.revision.id);
    await app.sync();
    f.state.headOverride = old.revision;
    await expect(app.open(doc.id)).rejects.toThrow(/revision|rollback|replay/);
    f.state.headOverride = null;
    f.state.syncOverride = {
      revisions: [{ seq: 999, envelope: old.revision }],
      cursor: 999,
      has_more: false,
    };
    await expect(app.sync()).rejects.toThrow(/revision|rollback|replay/);
    f.state.syncOverride = null;
    expect((await app.open(doc.id)).document.body).toBe("NEW");
    expect(
      (await storage.get<any>("journal:" + f.publication.vault)).heads[0].id,
    ).toBe(latest.revision.id);
    app.lock();
  });
  it("rejects rollback of a previously observed owner-signed revocation", async () => {
    const f = await revisionFixture(),
      peer = generateDevice("knovv", "peer");
    const original = sign(
      {
        schema: "knovv-device.v1",
        id: peer.id,
        signing_public: peer.signing_public,
        generation: 1,
        revoked: false,
      },
      f.owner.signing_key,
    ) as any;
    f.state.devices.push(original);
    const app = await f.client();
    f.state.devices[1] = sign(
      { ...original, generation: 2, revoked: true },
      f.owner.signing_key,
    ) as any;
    await app.refreshGrants();
    f.state.devices[1] = original;
    await expect(app.refreshGrants()).rejects.toThrow(/enrollment|rollback/);
    app.lock();
  });
  it("retains discoverability after body eviction and keeps bootstrap material through cache pressure", async () => {
    const f = await revisionFixture(),
      storage = new MemoryStorage(),
      app = await f.client(storage),
      doc = app.newDocument("wiki");
    doc.title = "A newly synced thought";
    doc.body = "Retained evidence body";
    const saved = await app.save(doc, null);
    await app.sync();
    await storage.remove("object:" + saved.revision.object);
    app.lock();
    await app.unlock(f.pack, "synthetic revision passphrase");
    expect(app.recent.map((d) => d.title)).toContain(doc.title);
    expect(
      (await app.overlaySearch("Retained evidence")).map((d) => d.id),
    ).toContain(doc.id);
    await storage.putCipher("a".repeat(64), new Uint8Array(20 * 1024 * 1024));
    await storage.putCipher("b".repeat(64), new Uint8Array(20 * 1024 * 1024));
    app.lock();
    f.state.offline = true;
    await expect(
      app.unlock(f.pack, "synthetic revision passphrase"),
    ).resolves.toBeUndefined();
    expect(app.unlocked).toBe(true);
    app.lock();
  });
  it("offers every retained history page beyond fifty revisions", async () => {
    const f = await revisionFixture(),
      app = await f.client(),
      doc = app.newDocument("wiki");
    doc.title = "A long-lived note";
    let parent: string | null = null;
    for (let i = 0; i < 53; i++) {
      doc.body = "Revision " + i;
      parent = (await app.save({ ...doc }, parent)).revision.id;
    }
    const first = await app.historyPage(doc.id);
    expect(first.revisions).toHaveLength(50);
    expect(first.nextBefore).toBeTruthy();
    const second = await app.historyPage(doc.id, first.nextBefore!);
    expect(second.revisions).toHaveLength(3);
    expect(second.nextBefore).toBeNull();
    app.lock();
  }, 30000);
});
