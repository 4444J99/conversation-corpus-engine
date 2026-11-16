import type { KnowledgeDocument, KnowledgeItem, ExportOptions } from '../types.js';
import type { KnowledgeSource } from './interface.js';
import { chromium } from 'playwright';
import { Readability } from '@mozilla/readability';
import { JSDOM } from 'jsdom';
import { createHash } from 'crypto';
import { captureReport, CaptureReceipt, CaptureReport, CaptureResult, IncompleteCaptureError } from './capture-report.js';

export interface WebClipperExportOptions extends ExportOptions {
  /** Explicit opt-in URL inventory; never sourced from browsing history. */
  urls?: string[];
}

const digest = (value: string) => createHash('sha256').update(value).digest('hex');

function normalizeUrl(input: string): string {
  const url = new URL(input);
  if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) throw new Error('invalid-url');
  url.hash = '';
  return url.toString();
}

export class WebClipperSource implements KnowledgeSource {
  id = 'web-clipper';
  name = 'Web Clipper';
  type: 'file' = 'file';
  lastCaptureReport: CaptureReport | null = null;

  async exportAll(options: WebClipperExportOptions = {}): Promise<KnowledgeItem[]> {
    const result = await this.exportAllWithReport(options);
    if (result.report.status !== 'complete') throw new IncompleteCaptureError(result);
    return result.items;
  }

  async exportAllWithReport(options: WebClipperExportOptions = {}): Promise<CaptureResult> {
    const urls = options.urls || [];
    if (!Array.isArray(urls) || urls.length > 100) throw new Error('Explicit URL queue must contain at most 100 URLs');
    const items: KnowledgeItem[] = [];
    const receipts: CaptureReceipt[] = [];
    const seen = new Set<string>();
    for (const input of urls) {
      let url: string;
      try {
        url = normalizeUrl(input);
      } catch {
        receipts.push({ id: `web-${digest(String(input))}`, stage: 'export', status: 'failed', reason: 'invalid-url' });
        continue;
      }
      if (seen.has(url)) continue;
      seen.add(url);
      const result = await this.captureUrl(url);
      if (result.item) items.push(result.item);
      receipts.push(result.receipt);
    }
    const report = captureReport(this.id, receipts);
    this.lastCaptureReport = report;
    return { items, report };
  }

  async exportItem(url: string): Promise<KnowledgeItem> {
    const result = await this.exportAllWithReport({ urls: [url] });
    if (result.report.status !== 'complete' || !result.items[0]) throw new IncompleteCaptureError(result);
    return result.items[0];
  }

  /** Preserve the manual single-URL API; details are available in lastCaptureReport. */
  async clipUrl(url: string): Promise<KnowledgeItem | null> {
    const result = await this.exportAllWithReport({ urls: [url] });
    return result.items[0] || null;
  }

  private async captureUrl(url: string): Promise<{ item?: KnowledgeDocument; receipt: CaptureReceipt }> {
    const id = `web-${digest(url)}`;
    const receipt: CaptureReceipt = { id, url, stage: 'export', status: 'failed', reason: 'capture-failed' };
    let browser;
    let item: KnowledgeDocument | undefined;
    let dom: JSDOM | undefined;
    try {
      browser = await chromium.launch();
      const page = await browser.newPage();
      const response = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
      if (!response || !response.ok()) {
        receipt.reason = response ? `http-${response.status()}` : 'no-http-response';
      } else {
        const html = await page.content();
        dom = new JSDOM(html, { url: page.url() });
        const article = new Readability(dom.window.document).parse();
        if (!article?.content || !article.textContent?.trim()) {
          receipt.status = 'reference-only';
          receipt.reason = 'no-extractable-body';
        } else {
          const now = new Date();
          item = {
            id,
            title: article.title || await page.title() || 'Untitled Web Page',
            content: article.content,
            created: now,
            modified: now,
            url,
            format: 'html',
            metadata: {
              sourceId: this.id,
              sourceName: this.name,
              sourceUrl: url,
              finalUrl: page.url(),
              capturedAt: now.toISOString(),
              timestampOrigin: 'capture',
              contentDigest: digest(article.content),
              capturedText: article.textContent,
              excerpt: article.excerpt,
              byline: article.byline,
              siteName: article.siteName,
              lang: article.lang,
              publishedTime: article.publishedTime,
              captureScope: 'readability article HTML and exact extracted text; excludes linked pages and attachment bytes',
            },
          };
          receipt.status = 'captured';
          delete receipt.reason;
        }
      }
    } catch {
      // Do not expose transport errors, browser state or credential-bearing URLs.
      receipt.status = 'failed';
      receipt.reason = 'capture-failed';
    } finally {
      dom?.window.close();
      if (browser) {
        try { await browser.close(); } catch {
          receipt.status = item ? 'incomplete' : 'failed';
          receipt.reason = 'browser-close-failed';
        }
      }
    }
    return { item, receipt };
  }
}
