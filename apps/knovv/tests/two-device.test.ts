import { describe, it, expect } from "vitest";
import { Miniflare } from "miniflare";
import { build } from "esbuild";
import { readFile } from "node:fs/promises";
import {
  generateDevice,
  protectDevice,
  sign,
  wrap,
  recipient,
  random,
  hex,
  encrypt,
  encoder,
  canonical,
  sha256,
} from "../src/crypto";
import { executeAgent } from "../agent/client";
import { Vault, MemoryStorage, compress, ApiError } from "../src/vault";
import type { DevicePackage } from "../src/contracts";

describe("independently enrolled devices", () => {
  it("synchronizes encrypted edits without the source host, preserves conflicts and rejects revoked writes", async () => {
    const owner = generateDevice("knovv", "publisher"),
      a = generateDevice("knovv", "desktop"),
      b = generateDevice("knovv", "phone");
    const compiled = await build({
      stdin: {
        contents: `import {api} from './server/api';export default {fetch:(r,e)=>api(r,e,{email:'owner@example.test'})}`,
        resolveDir: process.cwd(),
      },
      bundle: true,
      write: false,
      format: "esm",
      platform: "browser",
    });
    const mf = new Miniflare({
      modules: true,
      script: compiled.outputFiles[0].text,
      compatibilityDate: "2026-08-01",
      d1Databases: { DB: "two-device" },
      r2Buckets: ["BUCKET"],
      bindings: {
        OWNER_PUBLIC_KEY: owner.signing_public,
        OWNER_EMAIL: "owner@example.test",
        MAX_CIPHER_BYTES: "1048576",
      },
    });
    try {
      const db = await mf.getD1Database("DB");
      await db.exec(
        (await readFile("server/schema.sql", "utf8")).replace(/\n/g, " "),
      );
      const requests: string[] = [];
      let offline = false;
      const fetcher = async (path: string, init: RequestInit = {}) => {
        if (offline) throw new TypeError("offline fixture");
        const headers = new Headers(init.headers);
        headers.set("origin", "http://knovv.test");
        if (typeof init.body === "string") requests.push(init.body);
        if (
          init.body != null &&
          typeof init.body !== "string" &&
          !(init.body instanceof ArrayBuffer)
        )
          throw new Error("unexpected-fixture-body");
        return mf.dispatchFetch("http://knovv.test" + path, {
          method: init.method,
          body: init.body ?? undefined,
          headers: Object.fromEntries(headers.entries()),
        }) as unknown as Promise<Response>;
      };
      for (const device of [a, b]) {
        const grant = sign(
          {
            schema: "knovv-device.v1",
            id: device.id,
            signing_public: device.signing_public,
            generation: 1,
            revoked: false,
          },
          owner.signing_key,
        );
        expect(
          (
            await fetcher("/api/devices", {
              method: "POST",
              body: canonical(grant),
            })
          ).status,
        ).toBe(201);
      }
      const vault = hex(random()),
        key = random(),
        catalog = {
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
      const cipher = encrypt(
          key,
          await compress(encoder.encode(canonical(catalog))),
          `knovv-object:${vault}:1`,
        ),
        object = sha256(cipher);
      const upload = sign(
        {
          schema: "knovv-upload.v1",
          object,
          size: cipher.length,
          device: a.id,
        },
        a.signing_key,
      );
      expect(
        (
          await fetcher("/api/objects/" + object, {
            method: "PUT",
            headers: { "x-knovv-authorization": btoa(canonical(upload)) },
            body: new Uint8Array(cipher).buffer,
          })
        ).status,
      ).toBe(201);
      const pub = sign(
        {
          schema: "knovv-publication.v1",
          vault,
          generation: 1,
          epoch: 1,
          parent: null,
          manifest: object,
          envelopes: [a, b].map((d) =>
            wrap(key, recipient(d), `knovv-publication.v1:${vault}:1`),
          ),
        },
        owner.signing_key,
      );
      expect(
        (
          await fetcher("/api/publication", {
            method: "POST",
            body: canonical(pub),
          })
        ).status,
      ).toBe(201);
      const trust = {
        owner: owner.signing_public,
        vault,
        generation: 1,
        head: sha256(encoder.encode(canonical(pub))),
      };
      const packA: DevicePackage = {
        schema: "knovv-device-package.v1",
        credential: await protectDevice(a, "synthetic desktop passphrase"),
        trust,
      };
      const packB: DevicePackage = {
        schema: "knovv-device-package.v1",
        credential: await protectDevice(b, "synthetic phone passphrase"),
        trust,
      };
      const storeA = new MemoryStorage(),
        storeB = new MemoryStorage(),
        desktop = new Vault(storeA, fetcher),
        phone = new Vault(storeB, fetcher);
      await desktop.unlock(packA, "synthetic desktop passphrase");
      await phone.unlock(packB, "synthetic phone passphrase");
      const agentCreated = await executeAgent(desktop, {
        action: "new",
        agent: "synthetic-codex",
        input: {
          title: "Agent authored",
          body: "PRIVATE AGENT CANARY",
          kind: "zettel",
          logical_id: "project-snapshot:synthetic-source",
          provenance: {
            source_authorship: "unknown",
            source_sha256: "f".repeat(64),
          },
        },
      });
      await phone.sync();
      const agentRead = await phone.open(agentCreated.document.id);
      expect(agentRead.document.author).toBe("agent");
      expect(agentRead.document.last_editor).toBe("agent:synthetic-codex");
      expect(agentRead.document.logical_id).toBe(
        "project-snapshot:synthetic-source",
      );
      expect(agentRead.document.provenance).toEqual({
        source_authorship: "unknown",
        source_sha256: "f".repeat(64),
      });
      await executeAgent(phone, {
        action: "save",
        agent: "synthetic-second",
        id: agentRead.document.id,
        parent: agentRead.revision!.id,
        input: { body: "Second agent change" },
      });
      await expect(
        executeAgent(desktop, {
          action: "save",
          agent: "synthetic-codex",
          id: agentRead.document.id,
          parent: agentRead.revision!.id,
          input: { body: "Retained agent conflict" },
        }),
      ).rejects.toMatchObject({ code: "revision-conflict" });
      expect(
        (await desktop.drafts()).some(
          (d) => d.document.body === "Retained agent conflict",
        ),
      ).toBe(true);
      const doc = desktop.newDocument("wiki");
      doc.title = "PRIVATE WORKFLOW CANARY";
      doc.body = "A durable private thought.";
      const file = encoder.encode("PRIVATE ATTACHMENT CANARY");
      Object.assign(
        doc,
        await desktop.attachFile(
          doc,
          file,
          "fixture.bin",
          "application/octet-stream",
        ),
      );
      const first = await desktop.save(doc, null);
      expect(first.state).toBe("remote");
      await phone.sync();
      const read = await phone.open(doc.id);
      expect(read.document.body).toBe(doc.body);
      expect(
        new TextDecoder().decode(
          await phone.attachment(read.document.attachments![0]),
        ),
      ).toBe("PRIVATE ATTACHMENT CANARY");
      const second = await phone.save(
        {
          ...read.document,
          body: "Changed on the second enrolled device.",
          links: ["d".repeat(64)],
        },
        read.revision!.id,
      );
      expect(second.state).toBe("remote");
      expect(phone.backlinks("d".repeat(64)).map((page) => page.id)).toContain(
        doc.id,
      );
      await expect(
        desktop.save(
          { ...doc, body: "A competing retained draft." },
          first.revision.id,
        ),
      ).rejects.toMatchObject({ code: "revision-conflict" });
      expect(
        (await desktop.drafts()).find((d) => d.document.id === doc.id)!.document
          .body,
      ).toBe("A competing retained draft.");
      expect((await desktop.open(doc.id)).document.body).toBe(
        "Changed on the second enrolled device.",
      );
      desktop.lock();
      await desktop.unlock(packA, "synthetic desktop passphrase");
      expect((await desktop.open(doc.id)).document.body).toBe(
        "Changed on the second enrolled device.",
      );
      phone.lock();
      offline = true;
      await phone.unlock(packB, "synthetic phone passphrase");
      expect((await phone.open(doc.id)).document.body).toBe(
        "Changed on the second enrolled device.",
      );
      offline = false;
      const rotatedKey = random(),
        rotatedCatalog = {
          ...catalog,
          epoch: 2,
          epoch_keys: { "1": hex(key) },
        };
      const rotatedCipher = encrypt(
          rotatedKey,
          await compress(encoder.encode(canonical(rotatedCatalog))),
          `knovv-object:${vault}:2`,
        ),
        rotatedId = sha256(rotatedCipher);
      const rotatedUpload = sign(
        {
          schema: "knovv-upload.v1",
          object: rotatedId,
          size: rotatedCipher.length,
          device: a.id,
        },
        a.signing_key,
      );
      expect(
        (
          await fetcher("/api/objects/" + rotatedId, {
            method: "PUT",
            headers: {
              "x-knovv-authorization": btoa(canonical(rotatedUpload)),
            },
            body: new Uint8Array(rotatedCipher).buffer,
          })
        ).status,
      ).toBe(201);
      const rotatedPublication = sign(
        {
          ...pub,
          generation: 2,
          epoch: 2,
          parent: sha256(encoder.encode(canonical(pub))),
          manifest: rotatedId,
          envelopes: [a, b].map((d) =>
            wrap(rotatedKey, recipient(d), `knovv-publication.v1:${vault}:2`),
          ),
        },
        owner.signing_key,
      );
      expect(
        (
          await fetcher("/api/publication", {
            method: "POST",
            body: canonical(rotatedPublication),
          })
        ).status,
      ).toBe(201);
      await phone.sync();
      expect(phone.publication?.epoch).toBe(2);
      expect((await phone.open(doc.id)).document.body).toContain(
        "Changed on the second",
      );
      expect(
        new TextDecoder().decode(await phone.attachment(doc.attachments![0])),
      ).toBe("PRIVATE ATTACHMENT CANARY");
      await desktop.sync();
      const revoke = sign(
        {
          schema: "knovv-device.v1",
          id: a.id,
          signing_public: a.signing_public,
          generation: 2,
          revoked: true,
        },
        owner.signing_key,
      );
      expect(
        (
          await fetcher("/api/devices", {
            method: "POST",
            body: canonical(revoke),
          })
        ).status,
      ).toBe(201);
      await expect(
        desktop.save({ ...doc, body: "Revoked attempt" }, second.revision.id),
      ).rejects.toMatchObject({ status: 403 });
      expect(requests.join("")).not.toContain("PRIVATE WORKFLOW CANARY");
      expect(requests.join("")).not.toContain("PRIVATE ATTACHMENT CANARY");
      expect(JSON.stringify([...storeA.values.values()])).not.toContain(
        "PRIVATE WORKFLOW CANARY",
      );
      desktop.lock();
      phone.lock();
    } finally {
      await mf.dispose();
    }
  }, 60000);
});
