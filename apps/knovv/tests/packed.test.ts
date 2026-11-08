import { describe, it, expect } from "vitest";
import { execFileSync } from "node:child_process";
import { readFile, mkdir, mkdtemp, rm } from "node:fs/promises";
import { resolve } from "node:path";
import { Vault, MemoryStorage } from "../src/vault";

describe("packed canonical application publications", () => {
  it("opens Python-produced wiki pages and searches source passages through encrypted locator and data shards", async () => {
    await mkdir(".private", { recursive: true, mode: 0o700 });
    const root = await mkdtemp(resolve(".private/packed-"));
    try {
      execFileSync(
        process.env.KNOVV_PYTHON ?? resolve("../../.venv/bin/python"),
        ["tests/make_fixture.py", root],
        { encoding: "utf8", timeout: 30000 },
      );
      const pack = JSON.parse(await readFile(root + "/device.json", "utf8")),
        publication = JSON.parse(
          await readFile(root + "/bundle/HEAD.knovv", "utf8"),
        ),
        grant = JSON.parse(await readFile(root + "/grant.json", "utf8"));
      const vault = new Vault(new MemoryStorage(), async (path) => {
        if (path === "/api/publication") return Response.json(publication);
        if (path === "/api/devices") return Response.json({ devices: [grant] });
        if (path.startsWith("/api/sync"))
          return Response.json({ revisions: [], cursor: 0, has_more: false });
        if (path.startsWith("/api/documents/"))
          return Response.json({ head: null, revisions: [] });
        if (path.startsWith("/api/objects/")) {
          try {
            const bytes = await readFile(
              root + "/bundle/objects/" + path.split("/").at(-1),
            );
            return new Response(new Uint8Array(bytes).buffer);
          } catch {
            return Response.json({ error: "missing" }, { status: 404 });
          }
        }
        return Response.json({ error: "unexpected" }, { status: 500 });
      });
      await vault.unlock(pack, "fixture-only-passphrase");
      expect(vault.manifest?.resolve).toBeDefined();
      const page = vault.manifest!.navigation[0];
      expect((await vault.open(page.id)).document.body).toBeTruthy();
      const result = await vault.baseSearch("lattice sources");
      expect(result.total).toBe(2);
      const matches = await Promise.all(
        result.ordinals.map((n) => vault.meta(n)),
      );
      const source = await vault.open(
        matches.find((m) => m.kind === "source")!.id,
      );
      expect(source.document.body).toContain(
        "A lattice remembers its sources.",
      );
      vault.lock();
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  }, 40000);
});
