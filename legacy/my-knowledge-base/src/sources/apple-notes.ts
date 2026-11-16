import type { KnowledgeItem, ExportOptions } from '../types.js';
import type { KnowledgeSource } from './interface.js';
import { execFile } from 'child_process';
import { join } from 'path';
import { promisify } from 'util';
import { captureReport, CaptureReceipt, CaptureReport, CaptureResult, IncompleteCaptureError } from './capture-report.js';

const execFileAsync = promisify(execFile);

interface AppleNote {
  id: string;
  account: string;
  title: string;
  htmlBody: string;
  created: string | null;
  modified: string | null;
}

/** Parse on-demand JXA output without losing partial export receipts. */
export function parseAppleNotesExport(stdout: string): CaptureResult {
  const payload = JSON.parse(stdout);
  const legacy = Array.isArray(payload);
  const notes: AppleNote[] = legacy ? payload : payload.notes;
  if (!Array.isArray(notes) || (!legacy && (payload.schemaVersion !== 1 || !Array.isArray(payload.report?.receipts)))) throw new Error('Invalid Apple Notes export');
  const receipts: CaptureReceipt[] = legacy ? [] : [...payload.report.receipts];
  if (legacy) receipts.push({ id: 'apple-notes', stage: 'list', status: 'incomplete', reason: 'legacy-export-without-failure-accounting' });
  const items: KnowledgeItem[] = [];
  for (const note of notes) {
    if (typeof note.id !== 'string' || !note.id || typeof note.htmlBody !== 'string') {
      receipts.push({ id: typeof note.id === 'string' ? note.id : 'unknown-note', stage: 'export', status: 'failed', reason: 'invalid-note' });
      continue;
    }
    const now = new Date();
    items.push({
      id: note.id,
      title: note.title || 'Untitled Note',
      content: note.htmlBody,
      created: note.created ? new Date(note.created) : now,
      modified: note.modified ? new Date(note.modified) : now,
      url: `applenotes://${note.id}`,
      format: 'html',
      metadata: {
        sourceId: 'apple-notes', sourceName: 'Apple Notes', account: note.account,
        originalId: note.id, attachmentBytesCaptured: false,
        captureScope: 'note HTML body; excludes attachment bytes',
        timestampOrigin: { created: note.created ? 'source' : 'capture', modified: note.modified ? 'source' : 'capture' },
      },
    });
    if (legacy) receipts.push({ id: note.id, stage: 'export', status: 'captured' });
  }
  const report = { ...(!legacy ? payload.report : {}), ...captureReport('apple-notes', receipts) };
  return { items, report };
}

export class AppleNotesSource implements KnowledgeSource {
  id = 'apple-notes';
  name = 'Apple Notes';
  type: 'file' = 'file';
  lastCaptureReport: CaptureReport | null = null;

  async exportAll(options: ExportOptions = {}): Promise<KnowledgeItem[]> {
    const result = await this.exportAllWithReport(options);
    if (result.report.status !== 'complete') throw new IncompleteCaptureError(result);
    return result.items;
  }

  async exportAllWithReport(_options: ExportOptions = {}): Promise<CaptureResult> {
    let result: CaptureResult;
    try {
      if (process.platform !== 'darwin') throw new Error('macOS required');
      const scriptPath = join(process.cwd(), 'scripts', 'export-apple-notes.js');
      const { stdout } = await execFileAsync('/usr/bin/osascript', ['-l', 'JavaScript', scriptPath], { maxBuffer: 50 * 1024 * 1024, timeout: 120000 });
      result = parseAppleNotesExport(stdout);
    } catch {
      result = { items: [], report: captureReport(this.id, [{ id: this.id, stage: 'list', status: 'failed', reason: 'notes-export-unavailable' }]) };
    }
    this.lastCaptureReport = result.report;
    return result;
  }
}
