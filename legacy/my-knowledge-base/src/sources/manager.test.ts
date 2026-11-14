import { afterEach, describe, expect, it, vi } from 'vitest';
import { SourceManager } from './manager.js';
import { GoogleDocsSource } from './google-docs.js';
import { IncompleteCaptureError } from './capture-report.js';

vi.mock('./claude.js', () => ({ ClaudeSource: class { async exportAll() { return []; } } }));
vi.mock('./chatgpt.js', () => ({ ChatGPTSource: class { async exportAll() { return []; } } }));
vi.mock('./gemini.js', () => ({ GeminiSource: class { async exportAll() { return []; } } }));
vi.mock('./local.js', () => ({ LocalFileSource: class { async exportAll() { return []; } } }));
vi.mock('./bookmarks.js', () => ({ BookmarkSource: class { async exportAll() { return []; } } }));
vi.mock('./claude-export.js', () => ({ ClaudeExportSource: class { async exportAll() { return []; } } }));
vi.mock('./chatgpt-export.js', () => ({ ChatGPTExportSource: class { async exportAll() { return []; } } }));
afterEach(() => vi.restoreAllMocks());

describe('source manager partial capture', () => {
  it('retains successful partial items and failure receipts without logging bodies', async () => {
    const item = { id: 'ok', title: 'Document', content: 'private synthetic body', created: new Date(), modified: new Date(), format: 'txt' as const, metadata: {} };
    const report = { source: 'google-docs', status: 'partial' as const, receipts: [{ id: 'failed', stage: 'export' as const, status: 'failed' as const }] };
    vi.spyOn(GoogleDocsSource.prototype, 'exportAll').mockRejectedValue(new IncompleteCaptureError({ items: [item], report }));
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {});
    const manager = new SourceManager();
    expect(await manager.ingestAll()).toEqual([item]);
    expect((manager as any).lastCaptureReports).toContainEqual(report);
    expect(JSON.stringify(errors.mock.calls)).not.toContain('private synthetic body');
  });
});
