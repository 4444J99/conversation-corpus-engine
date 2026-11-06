import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import Database from 'better-sqlite3';
import { afterEach, describe, expect, it } from 'vitest';
import { coreMigrations } from '../migrations.js';
import { CCE_LIBRARY_FORMAT, CceLibraryProvider } from './cce-library-provider.js';
import { FederatedIndexer } from './indexer.js';
import { FederatedSourceRegistry } from './source-registry.js';
import type { FederatedSourceRecord } from './types.js';

const roots: string[] = [];
const digest = (value: string | Buffer) => createHash('sha256').update(value).digest('hex');

function temporary(): string {
  const root = realpathSync(mkdtempSync(join(tmpdir(), 'cce-consumer-')));
  roots.push(root);
  return root;
}

function record(uid: string, overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schema: 'cce.library-index.v1', uid, title: `Title ${uid}`, provider: 'chatgpt',
    kind: 'chat', wing: 'ai', lane: 'conversations', version: '1.0.0',
    digest: 'b'.repeat(64), content_digest: 'c'.repeat(64), body_status: 'full',
    message_count: 1, source_ref: '/original/source.json', created: '', updated: '',
    path: `vault/records/${uid}.md`, ...overrides,
  };
}

function generation(root: string, name: string, rows: Record<string, unknown>[], blocks = true): string {
  const location = join(root, 'library-all', 'generations', name);
  mkdirSync(location, { recursive: true });
  const artifacts: Record<string, { sha256: string; bytes: number }> = {};
  const put = (path: string, body: string) => {
    mkdirSync(dirname(join(location, path)), { recursive: true });
    writeFileSync(join(location, path), body);
    artifacts[path] = { sha256: digest(body), bytes: Buffer.byteLength(body) };
  };
  for (const row of rows) {
    // Malformed index paths never control where the fixture writes.
    const uid = String(row.uid).replace(/[^a-z0-9-]/g, '-');
    put(`vault/records/${uid}.md`, `# ${row.title}\n\nEvidence for ${uid}.\n`);
  }
  if (blocks) put(`vault/blocks/${'a'.repeat(64)}.md`, '# Shared passage\n\nOriginal repeated passage with source backlinks.\n');
  put('library-index.jsonl', rows.map(row => JSON.stringify(row)).join('\n') + (rows.length ? '\n' : ''));
  put('export-manifest.json', JSON.stringify({
    schema: 'cce.library-export.v1', records: rows.length, blocks: Number(blocks),
    files: [...Object.keys(artifacts), 'export-manifest.json'], notion_members: [],
  }));
  const report = JSON.stringify({ schema: 'cce.library-build.v1', complete: true, coverage_complete: false, artifacts });
  writeFileSync(join(location, 'report.json'), report);
  writeFileSync(join(root, 'library-all/current.json'), JSON.stringify({
    schema: 'cce.library-current.v1', generation: `generations/${name}`, report_sha256: digest(report),
  }));
  return location;
}

function source(root: string, metadata: Record<string, unknown> = {}): FederatedSourceRecord {
  return {
    id: 'cce-source', name: 'CCE knowledge library', kind: 'local-filesystem', status: 'active',
    rootPath: root, includePatterns: [], excludePatterns: [],
    metadata: { format: CCE_LIBRARY_FORMAT, ...metadata },
    createdAt: '2026-10-27T00:00:00Z', updatedAt: '2026-10-27T00:00:00Z',
  };
}

function database(): Database.Database {
  const db = new Database(':memory:');
  for (const migration of coreMigrations.filter(m => m.version === 7 || m.version === 8)) migration.up(db);
  return db;
}

afterEach(() => { for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true }); });

describe('CCE library consumer', () => {
  it('preserves exact identities, both wings, hashes and canonical links in existing federation records', () => {
    const root = temporary();
    const location = generation(root, 'generation-a', [record('chatgpt-one'), record('documents-two', { provider: 'documents', wing: 'authored', lane: 'research', body_status: 'metadata_only', message_count: 0 })]);
    const scan = new CceLibraryProvider().scan(source(root));
    expect(Array.isArray(scan.documents)).toBe(false);
    const documents = [...scan.documents];
    expect(documents.map(doc => doc.externalId)).toEqual(['chatgpt-one', 'documents-two', `block:${'a'.repeat(64)}`]);
    expect(documents[0].metadata).toMatchObject({ cceUid: 'chatgpt-one', cceVersion: '1.0.0', cceDigest: 'b'.repeat(64), cceContentDigest: 'c'.repeat(64), cceSourceRef: '/original/source.json', cceWing: 'ai' });
    expect(documents[0].metadata.canonicalVaultPath).toBe(join(location, 'vault/records/chatgpt-one.md'));
    expect(documents[0].metadata.cceArtifactSha256).toBe(digest(readFileSync(join(location, documents[0].path))));
    expect(documents[1].metadata).toMatchObject({ cceWing: 'authored', cceBodyStatus: 'metadata_only' });
    expect(documents[2].content).toContain('Original repeated passage');
    expect(scan.count).toBe(3);
    scan.verifyUnchanged();
  });

  it('names --vault-profile when a generation was built without the legacy notes', () => {
    const root = temporary();
    const location = generation(root, 'generation-a', [record('chatgpt-one')]);
    const manifest = JSON.stringify({ schema: 'cce.library-export.v2', profile: 'none', records: 1, blocks: 0, files: ['export-manifest.json'] });
    writeFileSync(join(location, 'export-manifest.json'), manifest);
    const report = JSON.parse(readFileSync(join(location, 'report.json'), 'utf8'));
    report.artifacts['export-manifest.json'] = { sha256: digest(manifest), bytes: Buffer.byteLength(manifest) };
    const pinned = JSON.stringify(report);
    writeFileSync(join(location, 'report.json'), pinned);
    writeFileSync(join(root, 'library-all/current.json'), JSON.stringify({
      schema: 'cce.library-current.v1', generation: 'generations/generation-a', report_sha256: digest(pinned),
    }));
    expect(() => new CceLibraryProvider().scan(source(root))).toThrow(/--vault-profile none/);
  });

  it('loads bodies lazily and rejects body tampering against the pinned report', () => {
    const root = temporary();
    const location = generation(root, 'generation-a', [record('chatgpt-one')]);
    const scan = new CceLibraryProvider().scan(source(root));
    writeFileSync(join(location, 'vault/records/chatgpt-one.md'), 'tampered');
    expect(() => [...scan.documents]).toThrow(/hash|size|artifact/i);
  });

  it.each([
    record('../outside'),
    record('chatgpt-one', { path: '../../outside' }),
    record('chatgpt-one', { digest: 'not-a-hash' }),
    record('chatgpt-one', { schema: 'unowned-schema' }),
  ])('rejects malformed record identities and provenance paths', row => {
    const root = temporary();
    generation(root, 'generation-a', [row], false);
    expect(() => new CceLibraryProvider().scan(source(root))).toThrow();
  });

  it('refuses source symlinks and a pointer escaping its generation root', () => {
    const root = temporary();
    const location = generation(root, 'generation-a', [record('chatgpt-one')]);
    const file = join(location, 'vault/records/chatgpt-one.md');
    const outside = join(root, 'outside');
    writeFileSync(outside, readFileSync(file));
    rmSync(file);
    symlinkSync(outside, file);
    const scan = new CceLibraryProvider().scan(source(root));
    expect(() => [...scan.documents]).toThrow(/symlink/i);
    writeFileSync(join(root, 'library-all/current.json'), JSON.stringify({ schema: 'cce.library-current.v1', generation: '../outside', report_sha256: 'a'.repeat(64) }));
    expect(() => new CceLibraryProvider().scan(source(root))).toThrow(/generation|path/i);
  });

  it('rejects an invalid report hash and notices publication changes', () => {
    const root = temporary();
    generation(root, 'generation-a', [record('chatgpt-one')]);
    const scan = new CceLibraryProvider().scan(source(root));
    generation(root, 'generation-b', [record('chatgpt-one')]);
    expect(() => scan.verifyUnchanged()).toThrow(/changed/i);
    const pointerPath = join(root, 'library-all/current.json');
    const pointer = JSON.parse(readFileSync(pointerPath, 'utf8'));
    pointer.report_sha256 = '0'.repeat(64);
    writeFileSync(pointerPath, JSON.stringify(pointer));
    expect(() => new CceLibraryProvider().scan(source(root))).toThrow(/hash/i);
  });

  it.runIf(process.platform !== 'win32').each(['pointer', 'report', 'index', 'body'])(
    'rejects a %s FIFO without blocking before its deadline', stage => {
      const root = temporary();
      const location = generation(root, 'generation-a', [record('chatgpt-one')], false);
      const paths: Record<string, string> = {
        pointer: join(root, 'library-all/current.json'),
        report: join(location, 'report.json'),
        index: join(location, 'library-index.jsonl'),
        body: join(location, 'vault/records/chatgpt-one.md'),
      };
      rmSync(paths[stage]);
      const fifo = spawnSync('mkfifo', [paths[stage]], { timeout: 1000, encoding: 'utf8' });
      expect(fifo.status, fifo.stderr).toBe(0);
      // Isolate the synchronous open: a regression must time out this child,
      // never wedge the test worker or require an external pipe writer.
      const script = `
        import { CceLibraryProvider } from ${JSON.stringify(new URL('./cce-library-provider.ts', import.meta.url).href)};
        try {
          [...new CceLibraryProvider().scan(${JSON.stringify(source(root, { maxSeconds: 1 }))}).documents];
          process.exitCode = 1;
        } catch (error) {
          if (error.code !== 'CCE_LIBRARY_INVALID' || !/regular file/i.test(error.message)) throw error;
          console.log('rejected-nonregular-file');
        }
      `;
      const child = spawnSync(process.execPath, ['--import', import.meta.resolve('tsx'), '--input-type=module', '--eval', script], {
        timeout: 3000, killSignal: 'SIGKILL', encoding: 'utf8',
      });
      expect(child.error, child.stderr).toBeUndefined();
      expect(child.status, child.stderr).toBe(0);
      expect(child.stdout).toContain('rejected-nonregular-file');
    },
  );

  it('enforces declared record, file, total byte and cancellation bounds without truncating coverage', () => {
    const root = temporary();
    generation(root, 'generation-a', [record('chatgpt-one'), record('chatgpt-two')]);
    expect(() => new CceLibraryProvider().scan(source(root, { maxRecords: 1 }))).toThrow(/record|limit/i);
    expect(() => [...new CceLibraryProvider().scan(source(root, { maxFileSizeBytes: 10 })).documents]).toThrow(/size|byte|limit/i);
    expect(() => new CceLibraryProvider().scan(source(root, { maxTotalBytes: 1 }))).toThrow(/byte|limit/i);
    expect(() => new CceLibraryProvider().scan(source(root, { maxRecords: 0 }))).toThrow(/limit|positive/i);
    expect(() => new CceLibraryProvider().scan(source(root), { isCancelled: () => true })).toThrow(/cancel/i);
  });

  it('keeps CCE identities and missing cached records on full scan, with idempotent replay', async () => {
    const root = temporary();
    generation(root, 'generation-a', [record('chatgpt-one'), record('chatgpt-two')]);
    const db = database();
    try {
      const registry = new FederatedSourceRegistry(db);
      const registered = registry.createSource(source(root));
      const indexer = new FederatedIndexer(db, registry);
      const first = await indexer.scanSource(registered.id);
      expect(first.indexedCount).toBe(3);
      const id = (db.prepare('SELECT id FROM federated_documents WHERE external_id=?').get('chatgpt-one') as { id: string }).id;
      expect((await indexer.scanSource(registered.id)).skippedCount).toBe(3);
      generation(root, 'generation-b', [record('chatgpt-one', { title: 'Changed title', version: '1.0.1' })], false);
      const next = await indexer.scanSource(registered.id, { mode: 'full' });
      expect(next.summary).toMatchObject({ deletedCount: 0, retainedCount: 2 });
      expect(db.prepare('SELECT id,title FROM federated_documents WHERE external_id=?').get('chatgpt-one')).toEqual({ id, title: 'Changed title' });
      expect((db.prepare('SELECT COUNT(*) AS n FROM federated_documents').get() as { n: number }).n).toBe(3);
    } finally { db.close(); }
  });

  it('rolls back every CCE cache update if a later artifact fails verification', async () => {
    const root = temporary();
    generation(root, 'generation-a', [record('chatgpt-one')], false);
    const db = database();
    try {
      const registry = new FederatedSourceRegistry(db);
      const registered = registry.createSource(source(root));
      const indexer = new FederatedIndexer(db, registry);
      await indexer.scanSource(registered.id);
      const before = db.prepare('SELECT * FROM federated_documents').all();
      const location = generation(root, 'generation-b', [record('chatgpt-one', { title: 'Changed' }), record('chatgpt-two')], false);
      writeFileSync(join(location, 'vault/records/chatgpt-two.md'), 'bad');
      await expect(indexer.scanSource(registered.id)).rejects.toThrow();
      expect(db.prepare('SELECT * FROM federated_documents').all()).toEqual(before);
      expect(registry.getSourceById(registered.id)?.lastScanStatus).toBe('failed');
    } finally { db.close(); }
  });

  it('retains ordinary filesystem full-scan behavior', async () => {
    const root = temporary();
    writeFileSync(join(root, 'one.md'), 'ordinary document');
    const db = database();
    try {
      const registry = new FederatedSourceRegistry(db);
      const registered = registry.createSource({ name: 'ordinary', rootPath: root });
      const indexer = new FederatedIndexer(db, registry);
      await indexer.scanSource(registered.id);
      rmSync(join(root, 'one.md'));
      const run = await indexer.scanSource(registered.id, { mode: 'full' });
      expect(run.summary).toMatchObject({ deletedCount: 1 });
      expect((db.prepare('SELECT COUNT(*) AS n FROM federated_documents').get() as { n: number }).n).toBe(0);
    } finally { db.close(); }
  });
});
