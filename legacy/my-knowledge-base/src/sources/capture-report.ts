import type { KnowledgeItem } from '../types.js';

export interface CaptureReceipt {
  id: string;
  stage: 'list' | 'export';
  status: 'captured' | 'failed' | 'incomplete' | 'reference-only';
  reason?: string;
  url?: string;
  limitations?: string[];
}

export interface CaptureReport {
  source: string;
  status: 'complete' | 'partial' | 'failed';
  receipts: CaptureReceipt[];
}

export interface CaptureResult {
  items: KnowledgeItem[];
  report: CaptureReport;
}

export function captureReport(source: string, receipts: CaptureReceipt[]): CaptureReport {
  const problems = receipts.some(receipt => receipt.status !== 'captured');
  const captured = receipts.some(receipt => receipt.stage === 'export' && (receipt.status === 'captured' || receipt.status === 'incomplete'));
  return { source, status: problems ? (captured ? 'partial' : 'failed') : 'complete', receipts };
}

/** Legacy array APIs must not silently turn incomplete capture into success. */
export class IncompleteCaptureError extends Error {
  readonly items: KnowledgeItem[];
  readonly report: CaptureReport;

  constructor(result: CaptureResult) {
    super(`${result.report.source} capture ${result.report.status}; inspect report and partial items`);
    this.name = 'IncompleteCaptureError';
    this.items = result.items;
    this.report = result.report;
  }
}
