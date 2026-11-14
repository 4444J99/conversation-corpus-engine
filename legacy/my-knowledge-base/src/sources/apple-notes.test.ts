import { readFileSync } from 'fs';
import vm from 'vm';
import { describe, expect, it } from 'vitest';
import * as notesSource from './apple-notes.js';

function runExport(accounts: any[]) {
  const sandbox = { Application: () => ({ accounts: () => accounts }) };
  vm.createContext(sandbox);
  vm.runInContext(readFileSync('scripts/export-apple-notes.js', 'utf8').replace(/^#!.*\n/, ''), sandbox);
  return JSON.parse(vm.runInContext('run([])', sandbox));
}
const note = (id: string, body: () => string) => ({ id: () => id, name: () => 'Note', body, creationDate: () => new Date('2020-01-01'), modificationDate: () => new Date('2021-01-01') });

describe('observable Apple Notes HTML export', () => {
  it('preserves exact note IDs/bodies and accounts for a failed note', () => {
    const result = runExport([{ name: () => 'Synthetic', notes: () => [note('note-1', () => '<div>Exact body &amp; text</div>'), note('note-2', () => { throw new Error('synthetic private error'); })] }]);
    expect(result.notes).toHaveLength(1);
    expect(result.notes[0]).toMatchObject({ id: 'note-1', htmlBody: '<div>Exact body &amp; text</div>' });
    expect(result.report).toMatchObject({ status: 'partial', discoveredNotes: 2, exportedNotes: 1, failedNotes: 1, receipts: expect.arrayContaining([expect.objectContaining({ id: 'note-2', stage: 'export', status: 'failed' })]) });
    expect(JSON.stringify(result)).not.toContain('synthetic private error');
  });
  it('reports inaccessible accounts rather than a successful empty export', () => {
    const result = runExport([{ name: () => 'Synthetic', notes: () => { throw new Error('permission denied'); } }]);
    expect(result.report).toMatchObject({ status: 'failed', failedAccounts: 1 });
  });
  it('converts a report without discarding partial status or claiming attachment bytes', () => {
    const exported = runExport([{ name: () => 'Synthetic', notes: () => [note('note-1', () => '<div>body</div>'), note('note-2', () => { throw new Error('locked'); })] }]);
    const result = (notesSource as any).parseAppleNotesExport(JSON.stringify(exported));
    expect(result.items[0]).toMatchObject({ id: 'note-1', content: '<div>body</div>', created: new Date('2020-01-01'), metadata: { attachmentBytesCaptured: false } });
    expect(result.report.status).toBe('partial');
  });
});
