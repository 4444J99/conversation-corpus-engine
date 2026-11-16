// Measures the actual browser reader/search implementation in Node. This does
// not measure browser rendering, a physical phone, Access login or network RTT.
import { resolve, join } from "node:path";
import { performance } from "node:perf_hooks";
import { privateRead, privateOutput, noLinks } from "../agent/storage";
import { Vault, MemoryStorage } from "../src/vault";
import { terms } from "../src/search";
import { sha256, encoder } from "../src/crypto";

async function main() {
  process.umask(0o077);
  if (process.argv.length !== 3) throw new Error("private-config-required");
  const config = JSON.parse(
    (await privateRead(process.argv[2], 65536)).toString(),
  );
  for (const key of ["bundle", "package", "unlock", "grant", "output"])
    if (typeof config[key] !== "string") throw new Error("invalid-config");
  await noLinks(config.output);
  const pack = JSON.parse(
      (await privateRead(config.package, 1024 * 1024)).toString(),
    ),
    passphrase /* allow-secret: runtime-only */ = (
      await privateRead(config.unlock, 1024 * 1024)
    ).toString(),
    grant = JSON.parse((await privateRead(config.grant, 65536)).toString()),
    head = JSON.parse(
      (
        await privateRead(join(config.bundle, "HEAD.knovv"), 256 * 1024)
      ).toString(),
    ),
    storage = new MemoryStorage();
  let reads = 0,
    bytes = 0;
  const fetcher = async (path: string, init?: RequestInit) => {
    if (init?.method && init.method !== "GET")
      throw new Error("benchmark-is-read-only");
    if (path === "/api/publication") return Response.json(head);
    if (path === "/api/devices") return Response.json({ devices: [grant] });
    if (path.startsWith("/api/sync?"))
      return Response.json({ revisions: [], cursor: 0, has_more: false });
    if (/^\/api\/documents\/[0-9a-f]{64}$/.test(path))
      return Response.json({ head: null, revisions: [] });
    const match = path.match(/^\/api\/objects\/([0-9a-f]{64})$/);
    if (!match) throw new Error("unexpected-benchmark-request");
    const raw = await privateRead(
      resolve(config.bundle, "objects", match[1]),
      48 * 1024 * 1024,
    );
    reads++;
    bytes += raw.length;
    return new Response(new Uint8Array(raw).buffer);
  };
  const vault = new Vault(storage, fetcher),
    started = performance.now();
  try {
    await vault.unlock(pack, passphrase);
    const unlockMs = performance.now() - started,
      manifest = vault.manifest!,
      firstSource = manifest.navigation.length,
      ordinals = [
        ...new Set([0, firstSource, firstSource + 1, firstSource + 16]),
      ].filter((n) => n < manifest.count),
      cases = [];
    let lastId = "";
    for (const ordinal of ordinals) {
      const start = performance.now(),
        meta = await vault.meta(ordinal),
        opened = await vault.open(meta.id),
        document = opened.document,
        readMs = performance.now() - start;
      lastId = document.id;
      const tokens = terms(document.body).filter((t) => t.length >= 6);
      const query = (tokens.length ? tokens : terms(document.title))
        .slice(0, 4)
        .join(" ");
      if (!query) continue;
      const coldStart = performance.now(),
        cold = await vault.baseSearch(query),
        coldMs = performance.now() - coldStart,
        warmStart = performance.now(),
        warm = await vault.baseSearch(query),
        warmMs = performance.now() - warmStart;
      // These early ordinals must appear even when the index has >1,000 matches.
      if (ordinal < 1000 && !cold.ordinals.some((n) => n === ordinal))
        throw new Error("known-passage-missing");
      if (JSON.stringify(cold) !== JSON.stringify(warm))
        throw new Error("inconsistent-search-result");
      for (const source of document.sources)
        if (
          !Number.isInteger(source.start) ||
          !Number.isInteger(source.end) ||
          source.start < 0 ||
          source.end < source.start ||
          (document.kind === "source" &&
            [...document.body].length !== source.end - source.start)
        )
          throw new Error("source-coordinate-mismatch");
      cases.push({
        ordinal,
        query_sha256: sha256(encoder.encode(query)),
        read_ms: readMs,
        cold_ms: coldMs,
        warm_ms: warmMs,
        matches: cold.total,
        plan: cold.plan,
        expected_ordinal_returned: cold.ordinals.some((n) => n === ordinal),
        source_spans: document.sources.length,
      });
    }
    const decodedBytes = vault.decodedCacheBytes;
    vault.lock();
    // Reopen in a fresh online reader, so the final page's ciphertext (rather
    // than only a decoded body left in memory) is present before going offline.
    const reopened = new Vault(storage, fetcher);
    try {
      await reopened.unlock(pack, passphrase);
      if (lastId) await reopened.open(lastId);
    } finally {
      reopened.lock();
    }
    const offline = new Vault(storage, async () => {
      throw new TypeError("offline");
    });
    try {
      await offline.unlock(pack, passphrase);
      if (lastId) await offline.open(lastId);
      await privateOutput(config.output, {
        schema: "knovv-reader-benchmark.v1",
        runtime: "node-browser-reader",
        browser_rendering: "unmeasured",
        network: "local-files",
        physical_phone: "unmeasured",
        publication: pack.trust.head,
        documents: manifest.count,
        metadata_pages: manifest.metadata.length,
        navigation_pages: manifest.navigation.length,
        unlock_ms: unlockMs,
        cases,
        ciphertext_reads: reads,
        ciphertext_bytes_read: bytes,
        decoded_cache_bytes: decodedBytes,
        max_rss_kib: process.resourceUsage().maxRSS,
        offline_cached_reopen: true,
        elapsed_ms: performance.now() - started,
      });
      console.log(
        JSON.stringify({ benchmark_finished: true, cases: cases.length }),
      );
    } finally {
      offline.lock();
    }
  } finally {
    vault.lock();
  }
}
main().catch((error) => {
  const code =
    typeof error?.message === "string" &&
    /^[a-z0-9-]{1,100}$/.test(error.message)
      ? error.message
      : "publication-benchmark-failed";
  console.error(code);
  process.exitCode = 1;
});
