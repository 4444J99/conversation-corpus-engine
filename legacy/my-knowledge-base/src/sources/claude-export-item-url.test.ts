import { afterEach, describe, expect, it, vi } from 'vitest';

// Exercise the actual source module without opening a browser or reading a profile.
vi.mock('playwright', () => ({ chromium: { launchPersistentContext: () => { throw new Error('Live browser access forbidden in this test'); } } }));
afterEach(() => vi.restoreAllMocks());

describe('Claude capture module integration', () => {
  it.each([
    ['synthetic-conversation', 'https://claude.ai/chat/synthetic-conversation'],
    ['synthetic/id?query#fragment', 'https://claude.ai/chat/synthetic%2Fid%3Fquery%23fragment'],
  ])('imports the source and exports supplied ID %s to its encoded conversation URL', async (id, expectedUrl) => {
    const { ClaudeSource } = await import('./claude.js');
    const navigated: string[] = [];
    const page = {
      goto: async (url: string) => { navigated.push(url); },
      waitForSelector: async () => {},
      waitForTimeout: async () => {},
      evaluate: async () => ({ title: 'Synthetic conversation', messages: [{ role: 'user', content: 'Synthetic message' }] }),
    };
    const source = new ClaudeSource();
    Object.assign(source, { page });
    const item = await source.exportItem(id);
    expect(navigated).toEqual([expectedUrl]);
    expect(item).toMatchObject({ id, url: expectedUrl, title: 'Synthetic conversation', messages: [{ role: 'user', content: 'Synthetic message' }] });
  });
});
