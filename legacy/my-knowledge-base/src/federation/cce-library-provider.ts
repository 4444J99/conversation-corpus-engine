/** Read a pinned CCE generation into the existing federation cache.
 *
 * CCE owns identities, source revisions and publication. This provider only
 * reads verified Markdown derivatives; it never ingests historical instructions
 * as commands, follows note links, rewrites sources, or deletes cached records.
 */
import { createHash } from 'node:crypto';
import { constants, closeSync, fstatSync, lstatSync, openSync, readSync } from 'node:fs';
import { dirname, isAbsolute, join, posix, resolve, sep } from 'node:path';
import { pathToFileURL } from 'node:url';
import { AppError } from '../logger.js';
import type { FederatedSourceRecord, LocalFilesystemDocument } from './types.js';

export const CCE_LIBRARY_FORMAT = 'cce.library-index.v1';
const UID = /^[a-z0-9][a-z0-9-]{0,199}$/;
const HASH = /^[a-f0-9]{64}$/;
const BLOCK_PATH = /^vault\/blocks\/([a-f0-9]{64})\.md$/;
const CHUNK_BYTES = 1024 * 1024;
const DEFAULT_LIMITS = {
  maxRecords: 100_000,
  maxBlocks: 1_000_000,
  maxFileSizeBytes: 128 * 1024 * 1024,
  maxMetadataBytes: 64 * 1024 * 1024,
  maxIndexBytes: 128 * 1024 * 1024,
  maxIndexRowBytes: 64 * 1024,
  maxTotalBytes: 8 * 1024 ** 3,
  maxSeconds: 300,
};

type JsonObject = Record<string, unknown>;
type Limits = typeof DEFAULT_LIMITS;
type Artifact = { sha256: string; bytes: number };

export interface CceLibraryScanOptions {
  isCancelled?: () => boolean;
}

export interface CceLibraryScan {
  /** Bodies are read one at a time while the indexer's transaction is open. */
  documents: Iterable<LocalFilesystemDocument>;
  count: number;
  metadata: Record<string, unknown>;
  verifyUnchanged: () => void;
}

function invalid(message: string): never {
  throw new AppError(message, 'CCE_LIBRARY_INVALID', 400);
}

function object(value: unknown, label: string): JsonObject {
  if (!value || typeof value !== 'object' || Array.isArray(value)) invalid(`Invalid CCE ${label}`);
  return value as JsonObject;
}

function sha256(value: string | Buffer): string {
  return createHash('sha256').update(value).digest('hex');
}

function relativePath(value: unknown): string {
  if (typeof value !== 'string' || !value || isAbsolute(value) || /^[a-z]:/i.test(value) ||
      /[\\\x00-\x1f\x7f]/.test(value) || value !== posix.normalize(value) ||
      value.split('/').some(part => part === '.' || part === '..' || !part)) {
    invalid('CCE artifact path must be a contained relative path');
  }
  return value;
}

function contained(root: string, relative: string): string {
  const path = resolve(root, relativePath(relative));
  if (!path.startsWith(root + sep)) invalid('CCE artifact path escapes the generation');
  return path;
}

function rejectSymlinks(path: string): void {
  for (let current = path; ; current = dirname(current)) {
    if (lstatSync(current).isSymbolicLink()) invalid(`CCE source contains a symlink: ${current}`);
    if (dirname(current) === current) return;
  }
}

function limitsFor(source: FederatedSourceRecord): Limits {
  const limits = { ...DEFAULT_LIMITS };
  for (const key of Object.keys(DEFAULT_LIMITS) as Array<keyof Limits>) {
    const value = source.metadata[key] ?? DEFAULT_LIMITS[key];
    if (typeof value !== 'number' || !Number.isSafeInteger(value) || value <= 0) {
      invalid(`CCE ${key} limit must be a positive safe integer`);
    }
    limits[key] = value;
  }
  return limits;
}

class BoundedReader {
  private readonly started = performance.now();
  private bytesRead = 0;

  constructor(readonly limits: Limits, private readonly options: CceLibraryScanOptions) {}

  check(additionalBytes = 0): void {
    if (this.options.isCancelled?.()) throw new AppError('Scan cancelled', 'SCAN_CANCELLED', 409);
    if (performance.now() - this.started > this.limits.maxSeconds * 1000) invalid('CCE scan deadline exceeded');
    if (this.bytesRead + additionalBytes > this.limits.maxTotalBytes) invalid('CCE total byte limit exceeded');
  }

  *chunks(path: string, maximum: number, expected?: Artifact): Generator<Buffer> {
    this.check();
    rejectSymlinks(path);
    // A FIFO must not block in open before fstat can reject it and before the
    // cooperative deadline can run. O_NONBLOCK does not change regular reads.
    const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    try {
      const before = fstatSync(fd);
      if (!before.isFile()) invalid('CCE artifact must be a regular file');
      if (before.size > maximum) invalid('CCE file size limit exceeded');
      if (expected && before.size !== expected.bytes) invalid('CCE artifact size differs from its report');
      // Refuse a known over-budget file before allocating its first buffer.
      this.check(before.size);
      const hash = createHash('sha256');
      const buffer = Buffer.allocUnsafe(Math.min(CHUNK_BYTES, Math.max(1, before.size)));
      let position = 0;
      while (position < before.size) {
        const amount = Math.min(buffer.length, before.size - position);
        this.check(amount);
        const read = readSync(fd, buffer, 0, amount, position);
        if (read === 0) invalid('CCE artifact changed during read');
        position += read;
        this.bytesRead += read;
        const chunk = buffer.subarray(0, read);
        hash.update(chunk);
        yield chunk;
      }
      const after = fstatSync(fd);
      if (before.ino !== after.ino || before.size !== after.size || before.mtimeMs !== after.mtimeMs) {
        invalid('CCE artifact changed during read');
      }
      if (expected && hash.digest('hex') !== expected.sha256) invalid('CCE artifact hash differs from its report');
    } finally {
      closeSync(fd);
    }
  }

  bytes(path: string, maximum: number, expected?: Artifact): Buffer {
    // Copy each reusable read buffer; at most one bounded artifact is retained.
    const chunks: Buffer[] = [];
    for (const chunk of this.chunks(path, maximum, expected)) chunks.push(Buffer.from(chunk));
    return Buffer.concat(chunks);
  }

  json(path: string, expected?: Artifact): JsonObject {
    return object(JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(
      this.bytes(path, this.limits.maxMetadataBytes, expected),
    )), 'JSON object');
  }

  *lines(path: string, expected: Artifact): Generator<string> {
    const decoder = new TextDecoder('utf-8', { fatal: true });
    let pending = '';
    for (const chunk of this.chunks(path, this.limits.maxIndexBytes, expected)) {
      pending += decoder.decode(chunk, { stream: true });
      let boundary: number;
      while ((boundary = pending.indexOf('\n')) >= 0) {
        this.check();
        const line = pending.slice(0, boundary);
        pending = pending.slice(boundary + 1);
        if (Buffer.byteLength(line) > this.limits.maxIndexRowBytes) invalid('CCE index row byte limit exceeded');
        if (line.trim()) yield line;
      }
      if (Buffer.byteLength(pending) > this.limits.maxIndexRowBytes) invalid('CCE index row byte limit exceeded');
    }
    pending += decoder.decode();
    if (Buffer.byteLength(pending) > this.limits.maxIndexRowBytes) invalid('CCE index row byte limit exceeded');
    if (pending.trim()) yield pending;
  }
}

function artifactFor(artifacts: JsonObject, path: string): Artifact {
  const expected = object(artifacts[path], `artifact ${path}`);
  if (typeof expected.sha256 !== 'string' || !HASH.test(expected.sha256) ||
      typeof expected.bytes !== 'number' || !Number.isSafeInteger(expected.bytes) || expected.bytes < 0) {
    invalid(`Invalid CCE artifact hash or size: ${path}`);
  }
  return expected as Artifact;
}

function recordRow(value: unknown): JsonObject {
  const row = object(value, 'index record');
  if (row.schema !== CCE_LIBRARY_FORMAT || typeof row.uid !== 'string' || !UID.test(row.uid) ||
      row.path !== `vault/records/${row.uid}.md`) invalid('Invalid CCE record uid, schema or canonical path');
  for (const key of ['digest', 'content_digest']) {
    if (typeof row[key] !== 'string' || !HASH.test(row[key] as string)) invalid(`Invalid CCE record ${key} hash`);
  }
  if (typeof row.version !== 'string' || !/^\d+\.\d+\.\d+$/.test(row.version)) invalid('Invalid CCE record version');
  if (!['ai', 'authored'].includes(String(row.wing)) ||
      !['full', 'partial', 'metadata_only'].includes(String(row.body_status))) invalid('Invalid CCE record coverage or wing');
  for (const key of ['title', 'provider', 'kind', 'lane', 'created', 'updated', 'source_ref']) {
    if (typeof row[key] !== 'string') invalid(`Invalid CCE record ${key}`);
  }
  if (!Number.isSafeInteger(row.message_count) || (row.message_count as number) < 0) invalid('Invalid CCE message count');
  return row;
}

export class CceLibraryProvider {
  scan(source: FederatedSourceRecord, options: CceLibraryScanOptions = {}): CceLibraryScan {
    if (source.metadata.format !== CCE_LIBRARY_FORMAT) invalid('Source is not a CCE library');
    const limits = limitsFor(source);
    const reader = new BoundedReader(limits, options);
    const root = resolve(source.rootPath);
    reader.check();
    rejectSymlinks(root);
    const pointerPath = join(root, 'library-all', 'current.json');
    const pointerBytes = reader.bytes(pointerPath, limits.maxMetadataBytes);
    const pointer = object(JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(pointerBytes)), 'pointer');
    if (pointer.schema !== 'cce.library-current.v1' || typeof pointer.generation !== 'string' ||
        !/^generations\/generation-[a-zA-Z0-9-]+$/.test(pointer.generation) ||
        typeof pointer.report_sha256 !== 'string' || !HASH.test(pointer.report_sha256)) {
      invalid('Invalid CCE generation pointer or report hash');
    }
    const generation = contained(join(root, 'library-all'), pointer.generation);
    const reportBytes = reader.bytes(join(generation, 'report.json'), limits.maxMetadataBytes);
    if (sha256(reportBytes) !== pointer.report_sha256) invalid('CCE report hash differs from its publication pointer');
    const report = object(JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(reportBytes)), 'report');
    if (report.schema !== 'cce.library-build.v1' || report.complete !== true) invalid('CCE generation is not verified complete');
    const artifacts = object(report.artifacts, 'artifact inventory');
    const manifest = reader.json(join(generation, 'export-manifest.json'), artifactFor(artifacts, 'export-manifest.json'));
    if (manifest.schema !== 'cce.library-export.v1' || !Array.isArray(manifest.files) ||
        !Number.isSafeInteger(manifest.records) || !Number.isSafeInteger(manifest.blocks) ||
        (manifest.records as number) < 0 || (manifest.blocks as number) < 0) invalid('Invalid CCE export manifest');
    if ((manifest.records as number) > limits.maxRecords || (manifest.blocks as number) > limits.maxBlocks) {
      invalid('CCE record or block limit exceeded');
    }
    const files = new Set(manifest.files.map(relativePath));
    if (files.size !== manifest.files.length) invalid('Duplicate CCE export inventory paths');
    const rows: JsonObject[] = [];
    const uids = new Set<string>();
    const indexArtifact = artifactFor(artifacts, 'library-index.jsonl');
    for (const line of reader.lines(join(generation, 'library-index.jsonl'), indexArtifact)) {
      const row = recordRow(JSON.parse(line));
      const uid = row.uid as string;
      if (uids.has(uid)) invalid('Duplicate CCE record uid');
      if (!files.has(row.path as string)) invalid('CCE record path absent from export inventory');
      artifactFor(artifacts, row.path as string);
      uids.add(uid);
      rows.push(row);
      if (rows.length > limits.maxRecords) invalid('CCE record limit exceeded');
    }
    const recordPaths = [...files].filter(path => path.startsWith('vault/records/'));
    if (rows.length !== manifest.records || recordPaths.length !== rows.length ||
        recordPaths.some(path => !uids.has(path.slice('vault/records/'.length, -3)))) {
      invalid('CCE record inventory differs from index');
    }
    const blockPaths = [...files].filter(path => path.startsWith('vault/blocks/')).sort();
    if (blockPaths.length !== manifest.blocks || blockPaths.some(path => !BLOCK_PATH.test(path))) {
      invalid('Invalid CCE block inventory');
    }
    for (const path of blockPaths) artifactFor(artifacts, path);
    const metadata = {
      cceStoreRoot: root, cceGeneration: pointer.generation, cceReportSha256: pointer.report_sha256,
      cceIndexSha256: indexArtifact.sha256, cceCoverageComplete: report.coverage_complete === true,
      cceRecords: rows.length, cceBlocks: blockPaths.length,
    };
    const makeDocument = (path: string, externalId: string, title: string, extra: JsonObject, modifiedAt: string): LocalFilesystemDocument => {
      const expected = artifactFor(artifacts, path);
      const absolute = contained(generation, path);
      const body = reader.bytes(absolute, limits.maxFileSizeBytes, expected);
      const content = new TextDecoder('utf-8', { fatal: true }).decode(body);
      return {
        externalId, path, title, content,
        // Generation binding refreshes canonical links when publication advances.
        hash: sha256(JSON.stringify([pointer.generation, expected.sha256, extra.cceDigest ?? externalId])),
        sizeBytes: body.byteLength, mimeType: 'text/markdown', modifiedAt,
        metadata: {
          ...metadata, ...extra, cceArtifactSha256: expected.sha256,
          absolutePath: absolute, canonicalVaultPath: absolute,
          canonicalVaultUri: pathToFileURL(absolute).href,
          evidenceOnly: true,
        },
      };
    };
    const documents = (function* (): Generator<LocalFilesystemDocument> {
      for (const row of rows) {
        reader.check();
        yield makeDocument(row.path as string, row.uid as string, row.title as string, {
          cceUid: row.uid, cceVersion: row.version, cceDigest: row.digest,
          cceContentDigest: row.content_digest, cceProvider: row.provider,
          cceKind: row.kind, cceWing: row.wing, cceLane: row.lane,
          cceBodyStatus: row.body_status, cceMessageCount: row.message_count,
          cceSourceRef: row.source_ref,
        }, (row.updated || row.created || '') as string);
      }
      for (const path of blockPaths) {
        reader.check();
        const digest = BLOCK_PATH.exec(path)![1];
        yield makeDocument(path, `block:${digest}`, `Shared passage ${digest.slice(0, 12)}`, {
          cceKind: 'block', cceBlockDigest: digest,
        }, '');
      }
    })();
    return {
      documents, count: rows.length + blockPaths.length, metadata,
      verifyUnchanged: () => {
        if (!reader.bytes(pointerPath, limits.maxMetadataBytes).equals(pointerBytes)) {
          invalid('CCE current generation changed during scan');
        }
      },
    };
  }
}
