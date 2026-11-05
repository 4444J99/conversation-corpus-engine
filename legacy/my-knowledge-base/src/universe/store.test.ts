import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { mkdirSync, rmSync } from 'fs';
import Database from 'better-sqlite3';
import { join } from 'path';
import { KnowledgeDatabase } from '../database.js';
import { CHAT_THREAD_CONTENT_HASH_VERSION, createChatThreadContentHash } from './content-hash.js';
import { UniverseStore } from './store.js';

describe('UniverseStore', () => {
  let tempDir: string;
  let dbPath: string;
  let db: KnowledgeDatabase;
  let store: UniverseStore;
  let raw: Database.Database;

  beforeEach(() => {
    tempDir = join(process.cwd(), '.test-tmp', 'universe-store');
    dbPath = join(tempDir, 'test.db');
    mkdirSync(tempDir, { recursive: true });

    db = new KnowledgeDatabase(dbPath);
    raw = db.getRawHandle();
    store = new UniverseStore(raw);
  });

  afterEach(() => {
    try {
      db.close();
    } catch {
      // ignore cleanup errors
    }
    rmSync(tempDir, { recursive: true, force: true });
  });

  it('ingests normalized threads and exposes provider/chat metadata', () => {
    const alpha = store.ingestNormalizedThread({
      provider: 'chatgpt',
      providerDisplayName: 'ChatGPT',
      externalAccountId: 'acct-chatgpt',
      accountDisplayName: 'ChatGPT User',
      accountEmail: '[email redacted]',
      externalThreadId: 'chatgpt-thread-alpha',
      title: 'Alpha Thread',
      sourcePath: 'intake/chatgpt/alpha.json',
      createdAt: '2026-01-01T00:00:00.000Z',
      updatedAt: '2026-01-01T00:00:00.000Z',
      turns: [
        { turnIndex: 0, role: 'user', content: 'Alpha asks about nebula synthesis.' },
        { turnIndex: 1, role: 'assistant', content: 'Assistant replies about nebula pipelines.' },
      ],
    });

    const beta = store.ingestNormalizedThread({
      provider: 'claude',
      providerDisplayName: 'Claude',
      externalThreadId: 'claude-thread-beta',
      title: 'Beta Thread',
      sourcePath: 'intake/claude/beta.json',
      turns: [
        { turnIndex: 0, role: 'user', content: 'Beta evaluates nebula resilience.' },
        { turnIndex: 1, role: 'assistant', content: 'Comparative view and evidence checks.' },
      ],
    });

    const summary = store.getUniverseSummary();
    expect(summary.providers).toBe(2);
    expect(summary.accounts).toBe(1);
    expect(summary.chats).toBe(2);
    expect(summary.turns).toBe(4);
    expect(summary.terms).toBeGreaterThan(0);
    expect(summary.occurrences).toBeGreaterThan(0);

    const providers = store.listProviders(20, 0);
    expect(providers.total).toBe(2);
    expect(providers.items.map((provider) => provider.providerId)).toEqual([
      'chatgpt',
      'claude',
    ]);

    const alphaChats = store.listProviderChats('chatgpt', 20, 0);
    expect(alphaChats.total).toBe(1);
    expect(alphaChats.items[0].id).toBe(alpha.thread.id);
    expect(alphaChats.items[0].providerId).toBe('chatgpt');
    expect(alphaChats.items[0].providerName).toBe('ChatGPT');
    expect(alphaChats.items[0].turnCount).toBe(2);

    const alphaTurns = store.listChatTurns(alpha.thread.id, 10, 0);
    expect(alphaTurns.total).toBe(2);
    expect(alphaTurns.items[0]).toMatchObject({
      turnIndex: 0,
      role: 'user',
      content: 'Alpha asks about nebula synthesis.',
    });

    const alphaChat = store.getChat(alpha.thread.id);
    expect(alphaChat).toMatchObject({
      id: alpha.thread.id,
      title: alpha.thread.title,
      providerId: 'chatgpt',
      providerName: 'ChatGPT',
      turnCount: 2,
    });

    const conversation = raw
      .prepare('SELECT * FROM conversations WHERE id = ?')
      .get(alpha.thread.id) as
      | {
          id: string;
          provider_id: string;
          provider_account_id: string;
          source_path: string;
          source_type: string;
        }
      | undefined;

    expect(conversation?.id).toBe(alpha.thread.id);
    expect(conversation?.provider_id).toBe(alpha.provider.id);
    expect(conversation?.provider_account_id).toBe(alpha.account?.id);
    expect(conversation?.source_path).toBe('intake/chatgpt/alpha.json');
    expect(conversation?.source_type).toBe('chat');

    const networkOnAlpha = store.getChatNetwork(alpha.thread.id, 10);
    const hasSharedEdge = networkOnAlpha.some((edge) => edge.targetThreadId === beta.thread.id);
    expect(hasSharedEdge).toBe(true);
  });

  it('finds term occurrences and parallel network edges', () => {
    const alpha = store.ingestNormalizedThread({
      provider: 'chatgpt',
      title: 'Shared Nebula Thread',
      sourcePath: 'intake/chatgpt/shared-nebula.json',
      turns: [
        { turnIndex: 0, role: 'user', content: 'Shared nebula signal appears here.' },
        { turnIndex: 1, role: 'assistant', content: 'Add an unrelated follow-up note.' },
      ],
    });

    store.ingestNormalizedThread({
      provider: 'claude',
      title: 'Secondary Nebula Thread',
      sourcePath: 'intake/claude/shared-nebula.json',
      turns: [
        { turnIndex: 0, role: 'user', content: 'Nebula drift appears in this thread too.' },
        { turnIndex: 1, role: 'assistant', content: 'Consolidate the shared evidence carefully.' },
      ],
    });

    const occurrences = store.findTermOccurrences('nebula', 20, 0);
    expect(occurrences.total).toBeGreaterThan(0);
    expect(occurrences.items.length).toBeGreaterThan(0);
    const providers = new Set(occurrences.items.map((row) => row.providerId));
    expect(providers).toEqual(new Set(['chatgpt', 'claude']));

    const parallelNetworks = store.listParallelNetworks(20, 1);
    expect(parallelNetworks.length).toBeGreaterThan(0);

    const sharedNetwork = store.getChatNetwork(alpha.thread.id, 10);
    expect(sharedNetwork.length).toBeGreaterThan(0);
    expect(sharedNetwork.some((edge) => edge.targetThreadId !== alpha.thread.id)).toBe(true);
  });

  it('updates existing chat threads instead of creating duplicates', () => {
    const provider = store.upsertProvider('grok', 'Grok');

    const created = store.upsertChatThread({
      providerRefId: provider.id,
      title: 'Original Thread',
      sourcePath: 'intake/grok/original.json',
    });

    const sameSource = store.upsertChatThread({
      providerRefId: provider.id,
      title: 'Updated Thread',
      sourcePath: 'intake/grok/original.json',
      externalThreadId: 'ext-thread-1',
    });

    expect(sameSource.id).toBe(created.id);
    expect(sameSource.title).toBe('Updated Thread');
    expect(sameSource.externalThreadId).toBe('ext-thread-1');
  });

  it('tracks content hash migration and existence checks', () => {
    const turns = [
      { turnIndex: 0, role: 'user' as const, content: 'Hash-stable first turn.' },
      { turnIndex: 1, role: 'assistant' as const, content: 'Hash-stable assistant response.' },
    ];

    const { thread } = store.ingestNormalizedThread({
      provider: 'copilot',
      title: 'Hash Migration Thread',
      sourcePath: 'intake/copilot/hash-thread.json',
      turns,
    });

    const staleMetadata = {
      contentHash: 'legacy-hash',
      contentHashVersion: 1,
    };
    raw.prepare('UPDATE chat_threads SET metadata = ? WHERE id = ?').run(JSON.stringify(staleMetadata), thread.id);

    const updatedCount = store.backfillChatThreadContentHashes();
    expect(updatedCount).toBe(1);

    const refreshedMetadata = JSON.parse(
      (raw.prepare('SELECT metadata FROM chat_threads WHERE id = ?').get(thread.id) as { metadata: string }).metadata,
    ) as { contentHash: string; contentHashVersion: number };
    const expectedHash = createChatThreadContentHash({ turns });

    expect(refreshedMetadata.contentHash).toBe(expectedHash);
    expect(refreshedMetadata.contentHashVersion).toBe(CHAT_THREAD_CONTENT_HASH_VERSION);
    expect(store.threadExistsByContentHash(expectedHash)).toBe(true);
    expect(store.threadExistsByContentHash('non-existent-hash')).toBe(false);
  });

  it('rebuilds derived indexes during reindexUniverse', () => {
    store.ingestNormalizedThread({
      provider: 'gemini',
      title: 'Reindex Thread One',
      sourcePath: 'intake/gemini/reindex-one.json',
      turns: [
        { turnIndex: 0, role: 'user', content: 'A common term for reindexing is omega.' },
        { turnIndex: 1, role: 'assistant', content: 'Omega appears for indexing validation.' },
      ],
    });
    store.ingestNormalizedThread({
      provider: 'gemini',
      externalAccountId: 'x-account',
      accountDisplayName: 'Gemini Analyst',
      title: 'Reindex Thread Two',
      sourcePath: 'intake/gemini/reindex-two.json',
      turns: [
        { turnIndex: 0, role: 'user', content: 'A second thread for reindexing with omega.' },
        { turnIndex: 1, role: 'assistant', content: 'Index rebuild should recover shared edges.' },
      ],
    });

    const before = raw.prepare('SELECT COUNT(*) as count FROM thematic_edges').get() as { count: number };
    expect(before.count).toBeGreaterThan(0);

    raw.exec('DELETE FROM term_occurrences; DELETE FROM thematic_edges; DELETE FROM term_lexicon;');
    const cleared = raw.prepare('SELECT COUNT(*) as count FROM term_occurrences').get() as { count: number };
    expect(cleared.count).toBe(0);

    const result = store.reindexUniverse();
    expect(result.threadsIndexed).toBe(2);
    expect(result.termsIndexed).toBeGreaterThan(0);
    expect(result.occurrencesIndexed).toBeGreaterThan(0);
    expect(result.networkEdgesIndexed).toBeGreaterThan(0);

    const occ = store.findTermOccurrences('omega', 20, 0);
    expect(occ.total).toBeGreaterThan(0);
  });

  it('persists ingest run metadata and final state', () => {
    const runId = store.createIngestRun('/tmp/universe/source', { source: 'unit-test' });
    const started = store.getIngestRun(runId);

    expect(started?.id).toBe(runId);
    expect(started?.sourceRoot).toBe('/tmp/universe/source');
    expect(started?.status).toBe('running');
    expect(started?.filesScanned).toBe(0);

    store.completeIngestRun(
      runId,
      'completed',
      {
        filesScanned: 5,
        filesIngested: 2,
        filesQuarantined: 1,
        chatsIngested: 1,
        turnsIngested: 8,
      },
      {
        metadata: {
          mode: 'unit-test',
          policyReport: true,
        },
      },
    );

    const finished = store.getIngestRun(runId);
    expect(finished?.status).toBe('completed');
    expect(finished?.filesScanned).toBe(5);
    expect(finished?.filesIngested).toBe(2);
    expect(finished?.filesQuarantined).toBe(1);
    expect(finished?.chatsIngested).toBe(1);
    expect(finished?.turnsIngested).toBe(8);
    expect(finished?.metadata).toMatchObject({
      mode: 'unit-test',
      policyReport: true,
    });
  });
});
