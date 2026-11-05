import { mkdtempSync, readFileSync, writeFileSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { describe, expect, it, vi } from 'vitest';
import { WebClipperSource } from './sources/web-clipper.js';

describe('explicit clip queue command', () => {
  it('writes bodies and receipts and returns a partial exit code', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'clip-queue-test-'));
    const queue = join(dir, 'queue.txt'); const output = join(dir, 'capture.json');
    writeFileSync(queue, '# Explicit URL list\nhttps://example.test/body\nhttps://example.test/link\n');
    vi.spyOn(WebClipperSource.prototype, 'exportAllWithReport').mockImplementation(async options => ({ items: [], report: { source: 'web-clipper', status: 'partial', receipts: options!.urls!.map(url => ({ id: url, url, stage: 'export', status: 'reference-only' })) } }));
    const { runClipQueue } = await import('./clip-queue-cli.js');
    expect(await runClipQueue([queue, output])).toBe(2);
    expect(JSON.parse(readFileSync(output, 'utf8')).report.receipts).toHaveLength(2);
    vi.restoreAllMocks();
  });
});
