// Synthetic localhost-only QA harness. The production entrypoint never imports this module.
import { Miniflare } from "miniflare";
import { build } from "esbuild";
import { execFileSync, spawn } from "node:child_process";
import { readFile, mkdir, mkdtemp, readdir } from "node:fs/promises";
import { resolve } from "node:path";
await mkdir(".private", { recursive: true, mode: 0o700 });
const root = await mkdtemp(resolve(".private/fixture-"));
const fixture = JSON.parse(
  execFileSync(
    resolve("../../.venv/bin/python"),
    ["tests/make_fixture.py", root],
    { encoding: "utf8" },
  ),
);
const compiled = await build({
  stdin: {
    contents: `import {api} from './server/api'; export default {fetch:(r,e)=>api(r,e,{email:'fixture@example.test'})}`,
    resolveDir: process.cwd(),
  },
  bundle: true,
  write: false,
  format: "esm",
  platform: "browser",
});
const mf = new Miniflare({
  host: "127.0.0.1",
  port: 8789,
  modules: true,
  script: compiled.outputFiles[0].text,
  compatibilityDate: "2026-08-01",
  d1Databases: { DB: "preview" },
  r2Buckets: ["BUCKET"],
  bindings: {
    OWNER_PUBLIC_KEY: fixture.owner,
    OWNER_EMAIL: "fixture@example.test",
    MAX_CIPHER_BYTES: "536870912",
  },
});
const db = await mf.getD1Database("DB"),
  bucket = await mf.getR2Bucket("BUCKET");
await db.exec(
  (await readFile("server/schema.sql", "utf8")).replace(/\n/g, " "),
);
for (const id of await readdir(root + "/bundle/objects")) {
  const bytes = await readFile(root + "/bundle/objects/" + id);
  await bucket.put(id, bytes);
  await db
    .prepare("INSERT INTO objects(id,size,uploaded) VALUES (?,?,1)")
    .bind(id, bytes.length)
    .run();
}
const base = await mf.ready;
for (const [path, file] of [
  ["devices", "grant.json"],
  ["publication", "bundle/HEAD.knovv"],
]) {
  const response = await mf.dispatchFetch(new URL("/api/" + path, base), {
    method: "POST",
    headers: { origin: base.origin, "content-type": "application/json" },
    body: await readFile(root + "/" + file, "utf8"),
  });
  if (!response.ok)
    throw new Error(
      "Fixture initialization failed: " + (await response.text()),
    );
}
const vite = spawn(
  process.execPath,
  [
    "node_modules/vite/bin/vite.js",
    "--host",
    "127.0.0.1",
    "--port",
    "5173",
    "--strictPort",
  ],
  { stdio: "inherit", env: { ...process.env, KNOVV_DEV_API: base.origin } },
);
process.stdout.write(
  JSON.stringify({
    preview: "http://127.0.0.1:5173",
    devicePackage: root + "/device.json",
    syntheticPassphrase: "fixture-only-passphrase",
  }) + "\n",
);
async function stop() {
  vite.kill("SIGTERM");
  await mf.dispose();
  process.exit(0);
}
process.on("SIGINT", () => void stop());
process.on("SIGTERM", () => void stop());
