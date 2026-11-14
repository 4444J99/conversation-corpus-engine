/**
 * Google Docs Knowledge Source (Read-Only)
 * Syncs documents from Google Drive and converts to markdown
 */

import { KnowledgeItem, ExportOptions, KnowledgeDocument } from '../types.js';
import { KnowledgeSource, SourceItemReference } from './interface.js';
import { GoogleAuthHelper } from './google-auth.js';
import { captureReport, CaptureReceipt, CaptureReport, CaptureResult, IncompleteCaptureError } from './capture-report.js';

export interface GoogleDocsConfig {
  enabled: boolean;
  client_id: string;
  client_secret: string;
  redirect_uri: string;
  scopes: string[];
  folders: Array<{
    id: string;
    name: string;
  }>;
}

export class GoogleDocsSource implements KnowledgeSource {
  id = 'google-docs';
  name = 'Google Docs';
  type: 'file' = 'file';

  private auth: GoogleAuthHelper | null = null;
  private config: GoogleDocsConfig | null = null;
  private initialized = false;
  private references = new Map<string, SourceItemReference>();
  lastCaptureReport: CaptureReport | null = null;

  async init(options?: ExportOptions): Promise<void> {
    if (this.initialized) return;

    // Load configuration from environment
    const clientId = process.env.GOOGLE_CLIENT_ID;
    const clientSecret = process.env.GOOGLE_CLIENT_SECRET;
    const redirectUri = process.env.GOOGLE_REDIRECT_URI || 'http://localhost:3000/auth/google/callback';

    if (!clientId || !clientSecret) {
      console.warn(
        '⚠️  Google Docs integration skipped: GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET not configured'
      );
      return;
    }

    this.auth = new GoogleAuthHelper(clientId, clientSecret, redirectUri);

    // Try to load existing tokens
    const tokens = this.auth.loadTokens();
    if (!tokens) {
      console.warn(
        '⚠️  Google Docs authentication required. Run: npm run auth:google-docs'
      );
      return;
    }

    if (!this.auth.areTokensValid()) {
      try {
        await this.auth.refreshToken();
      } catch (e) {
        console.warn('⚠️  Google Docs token refresh failed. Re-authenticate required.');
        return;
      }
    }

    this.config = {
      enabled: true,
      client_id: clientId,
      client_secret: clientSecret,
      redirect_uri: redirectUri,
      scopes: [
        'https://www.googleapis.com/auth/documents.readonly',
        'https://www.googleapis.com/auth/drive.readonly',
      ],
      folders: [...new Set((process.env.GOOGLE_DOCS_FOLDER_IDS ?? process.env.GOOGLE_DOCS_FOLDER_ID ?? 'root').split(',').map(id => id.trim()).filter(Boolean))].map(id => ({ id, name: id })),
    };

    this.initialized = true;
  }

  async listItems(): Promise<SourceItemReference[]> {
    const receipts: CaptureReceipt[] = [];
    const items = await this.scanItems(receipts);
    this.lastCaptureReport = captureReport(this.id, receipts);
    if (this.lastCaptureReport.status !== 'complete') {
      throw new IncompleteCaptureError({ items: [], report: this.lastCaptureReport });
    }
    return items;
  }

  private async scanItems(receipts: CaptureReceipt[]): Promise<SourceItemReference[]> {
    if (!this.initialized || !this.auth) {
      receipts.push({ id: this.id, stage: 'list', status: 'failed', reason: 'source-unavailable' });
      return [];
    }
    this.references.clear();
    if (!this.config?.folders.length) {
      receipts.push({ id: this.id, stage: 'list', status: 'failed', reason: 'invalid-folder-configuration' });
      return [];
    }
    let token: string;
    try {
      token = await this.auth.getValidAccessToken();
    } catch {
      receipts.push({ id: this.id, stage: 'list', status: 'failed', reason: 'authentication-failed' });
      return [];
    }
    const pending = (this.config?.folders || []).map(folder => folder.id);
    const visited = new Set<string>();
    let requests = 0;
    while (pending.length) {
      const requestedFolderId = pending.shift()!;
      if (visited.has(requestedFolderId)) continue;
      visited.add(requestedFolderId);
      if (++requests > 10000) {
        receipts.push({ id: requestedFolderId, stage: 'list', status: 'failed', reason: 'request-limit' });
        break;
      }
      let folderMetadata: any;
      try {
        folderMetadata = await this.getDriveMetadata(token, requestedFolderId, 'id,mimeType,trashed,driveId,capabilities(canListChildren)');
      } catch {
        receipts.push({ id: requestedFolderId, stage: 'list', status: 'failed', reason: 'folder-unavailable' });
        continue;
      }
      let invalidReason: string | undefined;
      if (typeof folderMetadata?.id !== 'string' || !folderMetadata.id || (requestedFolderId !== 'root' && folderMetadata.id !== requestedFolderId)) invalidReason = 'invalid-folder-metadata';
      else if (folderMetadata.mimeType !== 'application/vnd.google-apps.folder') invalidReason = 'not-a-folder';
      else if (folderMetadata.trashed !== false) invalidReason = 'folder-trashed-or-unknown';
      else if (folderMetadata.capabilities?.canListChildren !== true) invalidReason = 'folder-not-listable';
      else if (folderMetadata.driveId !== undefined && (typeof folderMetadata.driveId !== 'string' || !folderMetadata.driveId)) invalidReason = 'invalid-drive-identity';
      if (invalidReason) {
        receipts.push({ id: requestedFolderId, stage: 'list', status: 'failed', reason: invalidReason });
        continue;
      }
      const folderId = folderMetadata.id as string;
      if (folderId !== requestedFolderId && visited.has(folderId)) continue;
      visited.add(folderId);
      const pageTokens = new Set<string>();
      let pageToken: string | undefined;
      do {
        if (++requests > 10000) {
          receipts.push({ id: folderId, stage: 'list', status: 'failed', reason: 'request-limit' });
          return [...this.references.values()];
        }
        try {
          const url = new URL('https://www.googleapis.com/drive/v3/files');
          const escapedId = folderId.replace(/\\/g, '\\\\').replace(/'/g, "\\'");
          url.searchParams.set('q', `'${escapedId}' in parents and trashed=false and (mimeType='application/vnd.google-apps.document' or mimeType='application/vnd.google-apps.folder')`);
          url.searchParams.set('spaces', 'drive');
          url.searchParams.set('supportsAllDrives', 'true');
          url.searchParams.set('includeItemsFromAllDrives', 'true');
          url.searchParams.set('corpora', folderMetadata.driveId ? 'drive' : 'user');
          if (folderMetadata.driveId) url.searchParams.set('driveId', folderMetadata.driveId);
          url.searchParams.set('pageSize', '100');
          url.searchParams.set('fields', 'nextPageToken,incompleteSearch,files(id,name,mimeType,driveId,createdTime,modifiedTime,version,webViewLink,owners,lastModifyingUser)');
          if (pageToken) url.searchParams.set('pageToken', pageToken);
          const response = await fetch(url.toString(), { headers: { Authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(30000) });
          if (!response.ok) throw new Error(`http-${response.status}`);
          const data = await response.json() as any;
          if (!Array.isArray(data.files)) throw new Error('invalid-list-response');
          if (data.incompleteSearch) receipts.push({ id: folderId, stage: 'list', status: 'incomplete', reason: 'incomplete-search' });
          for (const file of data.files) {
            if (typeof file.id !== 'string' || !file.id) {
              receipts.push({ id: folderId, stage: 'list', status: 'failed', reason: 'invalid-file-reference' });
            } else if (file.mimeType === 'application/vnd.google-apps.folder') {
              if (!visited.has(file.id)) pending.push(file.id);
            } else if (file.mimeType === 'application/vnd.google-apps.document') {
              this.references.set(file.id, { id: file.id, title: file.name, url: file.webViewLink, metadata: file });
            }
          }
          pageToken = data.nextPageToken;
          if (pageToken && (typeof pageToken !== 'string' || pageTokens.has(pageToken))) {
            receipts.push({ id: folderId, stage: 'list', status: 'failed', reason: 'repeated-page-token' });
            break;
          }
          if (pageToken) pageTokens.add(pageToken);
        } catch {
          // Never log raw transport errors: they may carry authorization headers.
          receipts.push({ id: folderId, stage: 'list', status: 'failed', reason: 'drive-list-failed' });
          break;
        }
      } while (pageToken);
    }
    return [...this.references.values()];
  }

  async exportItem(id: string): Promise<KnowledgeItem> {
    if (!this.initialized || !this.auth) throw new Error('Google Docs source not initialized');
    return this.fetchAndConvertDocument(await this.auth.getValidAccessToken(), id);
  }

  async exportAll(options: ExportOptions = {}): Promise<KnowledgeItem[]> {
    const result = await this.exportAllWithReport(options);
    if (result.report.status !== 'complete') throw new IncompleteCaptureError(result);
    return result.items;
  }

  async exportAllWithReport(options: ExportOptions = {}): Promise<CaptureResult> {
    if (!this.initialized) await this.init(options);
    const receipts: CaptureReceipt[] = [];
    const references = await this.scanItems(receipts);
    const items: KnowledgeItem[] = [];
    for (const reference of references) {
      try {
        const item = await this.exportItem(reference.id) as KnowledgeDocument;
        items.push(item);
        const limitations = item.metadata.captureLimitations as string[];
        receipts.push({ id: reference.id, stage: 'export', status: limitations.length ? 'incomplete' : 'captured', limitations });
      } catch {
        receipts.push({ id: reference.id, stage: 'export', status: 'failed', reason: 'document-export-failed' });
      }
    }
    const report = captureReport(this.id, receipts);
    this.lastCaptureReport = report;
    return { items, report };
  }

  private async getDriveMetadata(accessToken: string, id: string, fields: string): Promise<any> {
    const url = new URL(`https://www.googleapis.com/drive/v3/files/${encodeURIComponent(id)}`);
    url.searchParams.set('fields', fields);
    url.searchParams.set('supportsAllDrives', 'true');
    const response = await fetch(url.toString(), { headers: { Authorization: `Bearer ${accessToken}` }, signal: AbortSignal.timeout(30000) });
    if (!response.ok) throw new Error(`Drive metadata error: HTTP ${response.status}`);
    return response.json();
  }

  /**
   * Fetch document from Google Docs API and convert to markdown
   */
  private async fetchAndConvertDocument(
    accessToken: string,
    documentId: string
  ): Promise<KnowledgeDocument> {
    const url = `https://docs.googleapis.com/v1/documents/${encodeURIComponent(documentId)}?includeTabsContent=true`;

    const response = await fetch(url, {
      headers: {
        Authorization: `Bearer ${accessToken}`,
        'Content-Type': 'application/json',
      },
      signal: AbortSignal.timeout(30000),
    });

    if (!response.ok) {
      throw new Error(`Google Docs API error: HTTP ${response.status}`);
    }

    const docData = await response.json() as any;

    const parts = this.selectedDocumentParts(docData);
    const markdown = this.convertToMarkdown(parts);
    const now = new Date();
    let sourceMetadata = this.references.get(documentId)?.metadata;
    if (!sourceMetadata) {
      sourceMetadata = await this.getDriveMetadata(accessToken, documentId, 'id,driveId,createdTime,modifiedTime,version,owners,lastModifyingUser');
    }
    const captureLimitations = this.contentLimitations(parts);
    const created = sourceMetadata?.createdTime ? new Date(sourceMetadata.createdTime) : now;
    const modified = sourceMetadata?.modifiedTime ? new Date(sourceMetadata.modifiedTime) : now;

    return {
      id: documentId,
      title: docData.title || 'Untitled',
      content: markdown,
      format: 'markdown',
      created,
      modified,
      url: `https://docs.google.com/document/d/${documentId}`,
      metadata: {
        source: 'google-docs',
        documentId,
        revisionId: docData.revisionId,
        driveVersion: sourceMetadata?.version,
        sourceCreatedTime: sourceMetadata?.createdTime,
        sourceModifiedTime: sourceMetadata?.modifiedTime,
        owners: sourceMetadata?.owners,
        lastModifyingUser: sourceMetadata?.lastModifyingUser,
        capturedAt: now.toISOString(),
        timestampOrigin: { created: sourceMetadata?.createdTime ? 'source' : 'capture', modified: sourceMetadata?.modifiedTime ? 'source' : 'capture' },
        captureLimitations,
        captureScope: 'document text projection; excludes comments, revision history and attachment bytes',
      },
    };
  }

  /**
   * Convert Google Docs structure to markdown
   * Handles headings, paragraphs, lists, tables, code blocks
   */
  private selectedDocumentParts(docData: any): Array<{ id: string; data: any }> {
    if (!Array.isArray(docData.tabs) || !docData.tabs.length) return [{ id: 'legacy', data: docData }];
    const parts: Array<{ id: string; data: any }> = [];
    const visit = (tab: any, path: string) => {
      const tabId = typeof tab?.tabProperties?.tabId === 'string' ? tab.tabProperties.tabId : path;
      parts.push({ id: `tab:${tabId}`, data: tab?.documentTab });
      if (Array.isArray(tab?.childTabs)) tab.childTabs.forEach((child: any, index: number) => visit(child, `${path}.${index}`));
    };
    docData.tabs.forEach((tab: any, index: number) => visit(tab, String(index)));
    return parts;
  }

  private convertToMarkdown(parts: Array<{ id: string; data: any }>): string {
    const body = parts.flatMap(part => Array.isArray(part.data?.body?.content) ? part.data.body.content : []);
    const lines: string[] = [];

    let inList = false;
    let listLevel = 0;

    for (const element of body) {
      if (element.paragraph) {
        const para = element.paragraph;

        // Detect heading style
        const styleId = para.paragraphStyle?.namedStyleType;
        const headingLevel = this.getHeadingLevel(styleId);

        // Handle list items
        if (para.bullet) {
          inList = true;
          const level = para.bullet.listProperties?.nestingLevel || 0;
          const prefix = level === 0 ? '- ' : '  '.repeat(level) + '- ';
          const text = this.extractParagraphText(para);
          lines.push(prefix + text);
          listLevel = level;
          continue;
        } else if (inList) {
          inList = false;
          lines.push('');
        }

        // Handle headings
        if (headingLevel > 0) {
          const heading = '#'.repeat(headingLevel) + ' ' + this.extractParagraphText(para);
          lines.push(heading);
          lines.push('');
          continue;
        }

        // Handle regular paragraphs
        const text = this.extractParagraphText(para);
        if (text.length > 0) {
          lines.push(text);
          lines.push('');
        }
      } else if (element.table) {
        // Convert table to markdown
        const tableMarkdown = this.convertTable(element.table);
        lines.push(tableMarkdown);
        lines.push('');
      }
    }

    return lines.join('\n').trim();
  }

  /**
   * Extract text from a paragraph element
   */
  private extractParagraphText(para: any): string {
    const texts: string[] = [];

    for (const run of para.elements || []) {
      if (run.textRun) {
        let text = run.textRun.content || '';

        // Handle text formatting
        const style = run.textRun.textStyle || {};
        if (style.bold) text = `**${text}**`;
        if (style.italic) text = `*${text}*`;
        if (style.strikethrough) text = `~~${text}~~`;

        texts.push(text);
      }
    }

    return texts.join('').trim();
  }

  /**
   * Convert Google Docs table to markdown
   */
  private convertTable(table: any): string {
    const rows = table.tableRows || [];
    const markdown: string[] = [];

    for (let i = 0; i < rows.length; i++) {
      const row = rows[i];
      const cells = row.tableCells || [];
      const cellTexts = cells.map((cell: any) => {
        const content = cell.content || [];
        const texts: string[] = [];

        for (const elem of content) {
          if (elem.paragraph) {
            texts.push(this.extractParagraphText(elem.paragraph));
          }
        }

        return texts.join(' ').trim();
      });

      markdown.push('| ' + cellTexts.join(' | ') + ' |');

      // Add separator after first row (header)
      if (i === 0) {
        const separator = cellTexts.map(() => '---').join(' | ');
        markdown.push('| ' + separator + ' |');
      }
    }

    return markdown.join('\n');
  }

  /**
   * Determine heading level from Google Docs style
   */
  private getHeadingLevel(styleId?: string): number {
    if (!styleId) return 0;

    const styleMap: Record<string, number> = {
      HEADING_1: 1,
      HEADING_2: 2,
      HEADING_3: 3,
      HEADING_4: 4,
      HEADING_5: 5,
      HEADING_6: 6,
    };

    return styleMap[styleId] || 0;
  }

  private contentLimitations(parts: Array<{ id: string; data: any }>): string[] {
    const limitations = new Set<string>();
    const visit = (value: any) => {
      if (!value || typeof value !== 'object') return;
      if (value.tableCells) {
        for (const cell of value.tableCells) {
          if ((cell.content || []).some((element: any) => element.table)) limitations.add('nested-table');
        }
      }
      for (const [key, child] of Object.entries(value)) {
        if (['inlineObjectElement', 'positionedObjects', 'inlineObjects', 'equation', 'richLink', 'person', 'footnotes', 'headers', 'footers', 'tableOfContents', 'footnoteReference', 'autoText', 'dateElement', 'suggestedInsertionIds', 'suggestedDeletionIds'].includes(key) && child && (typeof child !== 'object' || Object.keys(child).length)) limitations.add(key);
        visit(child);
      }
    };
    for (const part of parts) {
      if (!Array.isArray(part.data?.body?.content)) limitations.add(part.id === 'legacy' ? 'missing-body' : `missing-body:${part.id}`);
      visit(part.data);
    }
    return [...limitations];
  }

  async close(): Promise<void> {
    this.initialized = false;
    this.auth = null;
    this.config = null;
    this.references.clear();
  }
}
