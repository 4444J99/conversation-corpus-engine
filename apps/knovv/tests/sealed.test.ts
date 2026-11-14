import { describe, it, expect } from "vitest";
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { deviceFromIdentity, unhex, decoder } from "../src/crypto";
import { SealedReader } from "../src/sealed";

describe("original CCE archive format compatibility", () => {
  it.each(["v1", "v2"])(
    "reads exact Python %s source files and rejects untrusted or corrupted ciphertext",
    async (format) => {
      const script = `import tempfile,json,os\nfrom pathlib import Path\nfrom conversation_corpus_engine import archive_sealed as s\nfrom conversation_corpus_engine.archive_sealed_crypto import Identity\nwith tempfile.TemporaryDirectory() as tmp:\n p=Path(tmp);src=p/'source';src.mkdir();(src/'note').write_bytes(b'Exact synthetic source.\\x00\\xff');i=Identity('knovv','synthetic',os.urandom(32),os.urandom(32));t=s.create_trust(p/'trust',i);r=p/'sealed';s.publish(src,['note'],r,i,t,[i.recipient()],storage_format='${format}');print(json.dumps({'head':json.loads((r/'HEAD.cce').read_bytes()),'trust':json.loads(t.read_bytes()),'identity':{'format':'cce-sealed.v1','app_id':i.app_id,'instance_id':i.instance_id,'encryption_key':i.encryption_key.hex(),'signing_key':i.signing_key.hex()},'objects':{x.relative_to(r).as_posix():x.read_bytes().hex() for x in r.rglob('*') if x.is_file() and x.name!='HEAD.cce'}}))`;
      const fixture = JSON.parse(
        execFileSync(
          process.env.KNOVV_PYTHON ?? resolve("../../.venv/bin/python"),
          ["-c", script],
          {
            encoding: "utf8",
            timeout: 30000,
          },
        ),
      );
      const device = deviceFromIdentity(fixture.identity);
      const load = async (path: string) => unhex(fixture.objects[path]);
      const reader = await SealedReader.open(
        fixture.head,
        device,
        fixture.trust,
        load,
      );
      expect([...(await reader.file("note"))]).toEqual([
        ...new TextEncoder().encode("Exact synthetic source."),
        0,
        255,
      ]);
      await expect(
        SealedReader.open(
          fixture.head,
          device,
          { ...fixture.trust, generation: fixture.trust.generation + 1 },
          load,
        ),
      ).rejects.toThrow();
      const corrupt = await SealedReader.open(
        fixture.head,
        device,
        fixture.trust,
        async (path) => {
          const bytes = await load(path);
          bytes[70] ^= 1;
          return bytes;
        },
      );
      await expect(corrupt.file("note")).rejects.toThrow();
      reader.lock();
      await expect(reader.file("note")).rejects.toThrow();
    },
  );
});
