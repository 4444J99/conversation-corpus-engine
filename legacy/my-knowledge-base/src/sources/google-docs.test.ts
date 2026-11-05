import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { GoogleDocsSource } from './google-docs.js';
import { GoogleAuthHelper } from './google-auth.js';

const folder = (id: string) => ({ id, name: id, mimeType: 'application/vnd.google-apps.folder', trashed: false, capabilities: { canListChildren: true } });
const doc = (id: string) => ({ id, name: id, mimeType: 'application/vnd.google-apps.document', createdTime: '2020-01-01T00:00:00Z', modifiedTime: '2021-01-01T00:00:00Z', version: '42', owners: [{ displayName: 'Owner' }], lastModifyingUser: { displayName: 'Editor' } });
const body = (text: string) => ({ content: [{ paragraph: { elements: [{ textRun: { content: text } }] } }] });
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
function mockApi(list: (url: URL) => Response) {
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    const url = new URL(input);
    if (url.hostname === 'docs.googleapis.com') return response({ title: 'Document', revisionId: 'revision-7', tabs: [{ documentTab: { body: body('first tab') }, childTabs: [{ documentTab: { body: body('child tab') } }] }] });
    if (url.pathname.startsWith('/drive/v3/files/')) return response(folder(decodeURIComponent(url.pathname.split('/').at(-1)!)));
    return list(url);
  }));
}
async function source() { const s = new GoogleDocsSource(); await s.init(); return s; }

beforeEach(() => {
  vi.stubEnv('GOOGLE_DOCS_FOLDER_ID', 'root'); vi.stubEnv('GOOGLE_DOCS_FOLDER_IDS', undefined);
  vi.stubEnv('GOOGLE_CLIENT_ID', 'synthetic-client'); vi.stubEnv('GOOGLE_CLIENT_SECRET', 'synthetic-secret');
  vi.spyOn(GoogleAuthHelper.prototype, 'loadTokens').mockReturnValue({ access_token: 'synthetic-token', token_type: 'Bearer' });
  vi.spyOn(GoogleAuthHelper.prototype, 'areTokensValid').mockReturnValue(true);
  vi.spyOn(GoogleAuthHelper.prototype, 'getValidAccessToken').mockResolvedValue('synthetic-token');
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.unstubAllEnvs(); });

describe('Google Docs complete capture', () => {
  it('paginates beyond 100, recurses folders, and deduplicates overlapping roots and cycles', async () => {
    mockApi(url => {
      const q = url.searchParams.get('q')!;
      if (q.includes("'root'")) return response(url.searchParams.has('pageToken') ? { files: [doc('100'), folder('nested')] } : { files: Array.from({ length: 100 }, (_, i) => doc(String(i))), nextPageToken: 'next' });
      return response({ files: [doc('101'), doc('0'), folder('root')] });
    });
    const s = await source();
    const items = await s.exportAll();
    expect(items).toHaveLength(102);
    expect(new Set(items.map(x => x.id)).size).toBe(102);
    expect(items[0]).toMatchObject({ created: new Date('2020-01-01'), modified: new Date('2021-01-01'), metadata: { revisionId: 'revision-7', driveVersion: '42' } });
    expect((items[0] as any).content).toContain('child tab');
    expect((items[0] as any).metadata.author).toBeUndefined();
  });
  it('stops repeated page tokens and exposes partial items instead of false success', async () => {
    mockApi(() => response({ files: [doc('one')], nextPageToken: 'repeat' }));
    const s = await source();
    await expect(s.exportAll()).rejects.toMatchObject({ items: [{ id: 'one' }], report: { status: 'partial', receipts: expect.arrayContaining([expect.objectContaining({ reason: 'repeated-page-token' })]) } });
  });
  it('retains earlier pages after a mid-list failure and accounts for failed exports', async () => {
    mockApi(url => url.searchParams.has('pageToken') ? response({}, 503) : response({ files: [doc('ok'), doc('bad')], nextPageToken: 'second' }));
    const fetcher = global.fetch;
    vi.stubGlobal('fetch', vi.fn((input: string, init?: RequestInit) => input.includes('/documents/bad') ? Promise.resolve(response({}, 403)) : fetcher(input, init)));
    const s = await source();
    await expect(s.exportAll()).rejects.toMatchObject({ items: [{ id: 'ok' }], report: { status: 'partial', receipts: expect.arrayContaining([expect.objectContaining({ stage: 'list', status: 'failed' }), expect.objectContaining({ id: 'bad', stage: 'export', status: 'failed' })]) } });
  });
  it('marks unsupported and missing content incomplete, and honors incompleteSearch', async () => {
    mockApi(() => response({ files: [doc('image')], incompleteSearch: true }));
    const fetcher = global.fetch;
    vi.stubGlobal('fetch', vi.fn((input: string, init?: RequestInit) => input.includes('/documents/') ? Promise.resolve(response({ body: { content: [{ paragraph: { elements: [{ inlineObjectElement: { inlineObjectId: 'image' } }] } }] } })) : fetcher(input, init)));
    const s = await source();
    await expect(s.exportAll()).rejects.toMatchObject({ report: { status: 'partial', receipts: expect.arrayContaining([expect.objectContaining({ reason: 'incomplete-search' }), expect.objectContaining({ id: 'image', status: 'incomplete' })]) } });
  });
  it('traverses overlapping configured roots and deduplicates their documents', async () => {
    vi.stubEnv('GOOGLE_DOCS_FOLDER_IDS', "root,nested,root");
    mockApi(url => url.searchParams.get('q')!.includes("'root'") ? response({ files: [doc('one')] }) : response({ files: [doc('one'), doc('two')] }));
    const s = await source();
    expect((await s.listItems()).map(item => item.id)).toEqual(['one', 'two']);
  });
  it('marks missing document bodies and nested table content incomplete', async () => {
    mockApi(() => response({ files: [doc('missing'), doc('nested-table')] }));
    const fetcher = global.fetch;
    vi.stubGlobal('fetch', vi.fn((input: string, init?: RequestInit) => input.includes('/documents/') ? Promise.resolve(response(input.includes('/missing') ? { title: 'Missing' } : { body: { content: [{ table: { tableRows: [{ tableCells: [{ content: [{ table: { tableRows: [] } }] }] }] } }] } })) : fetcher(input, init)));
    const result = await (await source()).exportAllWithReport();
    expect(result.report.status).toBe('partial');
    expect(result.report.receipts).toEqual(expect.arrayContaining([expect.objectContaining({ id: 'missing', limitations: expect.arrayContaining(['missing-body']) }), expect.objectContaining({ id: 'nested-table', limitations: expect.arrayContaining(['nested-table']) })]));
  });
  it('does not report an unauthenticated source as an empty successful capture', async () => {
    vi.spyOn(GoogleAuthHelper.prototype, 'loadTokens').mockReturnValue(null);
    await expect(new GoogleDocsSource().exportAll()).rejects.toMatchObject({ report: { status: 'failed' } });
  });
});


describe('Google Docs validated folder scope', () => {
  it('captures nested shared-drive docs only with the root drive corpus and parent filters', async () => {
    vi.stubEnv('GOOGLE_DOCS_FOLDER_ID', 'shared-folder');
    const listed: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: string) => {
      const url = new URL(input);
      if (url.hostname === 'docs.googleapis.com') return response({ body: body('shared content') });
      if (url.pathname.startsWith('/drive/v3/files/')) {
        if (url.searchParams.get('supportsAllDrives') !== 'true') return response({}, 404);
        const id = url.pathname.split('/').at(-1)!;
        return response({ ...folder(id), driveId: 'selected-drive' });
      }
      if (url.searchParams.get('supportsAllDrives') !== 'true' || url.searchParams.get('includeItemsFromAllDrives') !== 'true' || url.searchParams.get('corpora') !== 'drive' || url.searchParams.get('driveId') !== 'selected-drive') return response({ files: [] });
      const query = url.searchParams.get('q')!;
      if (query.includes("'shared-folder' in parents")) { listed.push('shared-folder'); return response({ files: [{ ...folder('shared-child'), driveId: 'selected-drive' }] }); }
      if (query.includes("'shared-child' in parents")) { listed.push('shared-child'); return response({ files: [{ ...doc('shared-doc'), driveId: 'selected-drive' }] }); }
      throw new Error('Unscoped shared drive query');
    }));
    const result = await (await source()).exportAllWithReport();
    expect(result.items.map(item => item.id)).toEqual(['shared-doc']);
    expect((result.items[0] as any).content).toBe('shared content');
    expect(result.report.status).toBe('complete');
    expect(listed).toEqual(['shared-folder', 'shared-child']);
  });
  it('supports direct shared-drive document metadata retrieval', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: string) => {
      const url = new URL(input);
      if (url.hostname === 'docs.googleapis.com') return response({ body: body('direct shared content') });
      return url.searchParams.get('supportsAllDrives') === 'true' ? response({ ...doc('shared-doc'), driveId: 'selected-drive' }) : response({}, 404);
    }));
    const item = await (await source()).exportItem('shared-doc');
    expect(item).toMatchObject({ id: 'shared-doc', modified: new Date('2021-01-01') });
  });
  it('resolves the root alias and only certifies a validated empty folder', async () => {
    const requested: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: string) => {
      const url = new URL(input); requested.push(url.pathname);
      if (url.pathname.endsWith('/root')) return response(folder('actual-root'));
      return response({ files: url.searchParams.get('q')?.includes("'actual-root' in parents") ? [] : [doc('wrong-scope')] });
    }));
    const result = await (await source()).exportAllWithReport();
    expect(result.items).toEqual([]);
    expect(result.report.status).toBe('complete');
    expect(requested).toEqual(['/drive/v3/files/root', '/drive/v3/files']);
  });
  it.each([
    ['missing', () => response({}, 404)],
    ['inaccessible', () => response({}, 403)],
    ['deleted', () => response({ ...folder('bad-root'), trashed: true })],
    ['not-folder', () => response(doc('bad-root'))],
    ['cannot-list', () => response({ ...folder('bad-root'), capabilities: { canListChildren: false } })],
  ])('retains successful roots while accounting for a %s root', async (_name, invalidMetadata) => {
    vi.stubEnv('GOOGLE_DOCS_FOLDER_IDS', 'good-root,bad-root');
    let badChildrenQueried = false;
    vi.stubGlobal('fetch', vi.fn(async (input: string) => {
      const url = new URL(input);
      if (url.hostname === 'docs.googleapis.com') return response({ body: body('retained') });
      if (url.pathname.endsWith('/bad-root')) return invalidMetadata();
      if (url.pathname.endsWith('/good-root')) return response(folder('good-root'));
      if (url.searchParams.get('q')?.includes("'bad-root'")) badChildrenQueried = true;
      return response({ files: [doc('retained-doc')] });
    }));
    const result = await (await source()).exportAllWithReport();
    expect(result.items.map(item => item.id)).toEqual(['retained-doc']);
    expect(result.report.status).toBe('partial');
    expect(result.report.receipts).toContainEqual(expect.objectContaining({ id: 'bad-root', stage: 'list', status: 'failed' }));
    expect(badChildrenQueried).toBe(false);
  });
  it.each(['', '   ', ' , , '])('rejects an explicitly configured empty roots list %j', async value => {
    vi.stubEnv('GOOGLE_DOCS_FOLDER_IDS', value);
    const requests: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: string) => { requests.push(input); return response({ files: [] }); }));
    const result = await (await source()).exportAllWithReport();
    expect(result.report).toMatchObject({ status: 'failed', receipts: [expect.objectContaining({ reason: 'invalid-folder-configuration' })] });
    expect(requests).toEqual([]);
  });
});

describe('Google Docs selected tab completeness', () => {
  it('retains valid tab text and identifies a missing child tab body', async () => {
    mockApi(() => response({ files: [doc('mixed-tabs')] }));
    const fetcher = global.fetch;
    vi.stubGlobal('fetch', vi.fn((input: string, init?: RequestInit) => input.includes('/documents/') ? Promise.resolve(response({ tabs: [{ tabProperties: { tabId: 'good' }, documentTab: { body: body('Retained tab body') }, childTabs: [{ tabProperties: { tabId: 'missing-child' }, documentTab: {} }] }] })) : fetcher(input, init)));
    const result = await (await source()).exportAllWithReport();
    expect((result.items[0] as any).content).toBe('Retained tab body');
    expect(result.report).toMatchObject({ status: 'partial', receipts: [expect.objectContaining({ status: 'incomplete', limitations: expect.arrayContaining(['missing-body:tab:missing-child']) })] });
  });
  it('does not use an unselected legacy body to certify an empty tab projection', async () => {
    mockApi(() => response({ files: [doc('empty-tab')] }));
    const fetcher = global.fetch;
    vi.stubGlobal('fetch', vi.fn((input: string, init?: RequestInit) => input.includes('/documents/') ? Promise.resolve(response({ body: body('Unused legacy body'), tabs: [{ tabProperties: { tabId: 'empty' }, documentTab: {} }] })) : fetcher(input, init)));
    const result = await (await source()).exportAllWithReport();
    expect((result.items[0] as any).content).toBe('');
    expect(result.report).toMatchObject({ status: 'partial', receipts: [expect.objectContaining({ status: 'incomplete', limitations: expect.arrayContaining(['missing-body:tab:empty']) })] });
  });
});
