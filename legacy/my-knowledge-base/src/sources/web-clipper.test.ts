import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { WebClipperSource } from './web-clipper.js';

const browser = vi.hoisted(() => ({ launch: vi.fn(), newPage: vi.fn(), close: vi.fn(), goto: vi.fn(), content: vi.fn(), title: vi.fn(), url: vi.fn() }));
vi.mock('playwright', () => ({ chromium: { launch: browser.launch } }));
const article = (text: string) => '<html><head><title>Example</title><meta name="author" content="Named Writer"></head><body><article><h1>Example</h1><p>' + text.repeat(20) + '</p></article></body></html>';

beforeEach(() => {
  browser.launch.mockResolvedValue({ newPage: browser.newPage, close: browser.close });
  browser.newPage.mockResolvedValue({ goto: browser.goto, content: browser.content, title: browser.title, url: browser.url });
  browser.goto.mockResolvedValue({ ok: () => true, status: () => 200 });
  browser.content.mockResolvedValue(article('Captured text. ')); browser.title.mockResolvedValue('Example'); browser.url.mockReturnValue('https://example.test/article');
});
afterEach(() => vi.resetAllMocks());

describe('explicit web capture', () => {
  it('keeps stable URL identity across revisions and preserves exact extracted text/byline', async () => {
    const s = new WebClipperSource();
    const first = await s.clipUrl('https://EXAMPLE.test:443/article#section') as any;
    browser.content.mockResolvedValue(article('Changed text. '));
    const second = await s.clipUrl('https://example.test/article') as any;
    expect(first.id).toBe(second.id);
    expect(first.metadata.contentDigest).not.toBe(second.metadata.contentDigest);
    expect(first.metadata.capturedText).toContain('Captured text. Captured text.');
    expect(first.metadata.byline).toBe('Named Writer');
    expect(first.url).toBe('https://example.test/article');
  });
  it('captures only the supplied queue and reports captured, reference-only, and failed URLs', async () => {
    browser.goto.mockImplementation(async (url: string) => {
      browser.content.mockResolvedValue(url.endsWith('/link') ? '<html><body></body></html>' : article('Body text. '));
      return { ok: () => !url.endsWith('/failure'), status: () => url.endsWith('/failure') ? 403 : 200 };
    });
    const s = new WebClipperSource();
    const result = await (s as any).exportAllWithReport({ urls: ['https://example.test/body', 'https://example.test/link', 'https://example.test/failure', 'https://example.test/body#fragment'] });
    expect(result.items).toHaveLength(1);
    expect(result.report).toMatchObject({ status: 'partial', receipts: [expect.objectContaining({ status: 'captured' }), expect.objectContaining({ status: 'reference-only' }), expect.objectContaining({ status: 'failed', reason: 'http-403' })] });
    await expect(s.exportAll()).resolves.toEqual([]);
  });
  it('rejects excessive queues and unsupported URLs without visiting them', async () => {
    const s = new WebClipperSource();
    await expect((s as any).exportAllWithReport({ urls: Array.from({ length: 101 }, () => 'https://example.test') })).rejects.toThrow(/100/);
    const result = await (s as any).exportAllWithReport({ urls: ['file:///etc/passwd', 'https://user:secret@example.test/'] });
    expect(result.items).toEqual([]);
    expect(result.report.receipts.every((r: any) => r.status === 'failed' && r.reason === 'invalid-url')).toBe(true);
    expect(JSON.stringify(result)).not.toContain('secret');
  });
  it('rejects incomplete legacy exports and releases browser resources after navigation failure', async () => {
    browser.goto.mockRejectedValue(new Error('transport detail with synthetic secret'));
    const s = new WebClipperSource();
    await expect(s.exportAll({ urls: ['https://example.test/failure'] } as any)).rejects.toMatchObject({ report: { status: 'failed' } });
    expect(browser.close).toHaveBeenCalledOnce();
  });
});
