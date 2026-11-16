import { CaptureReport, IncompleteCaptureError } from './capture-report.js';
import { KnowledgeSource } from './interface.js';
import { ClaudeSource } from './claude.js';
import { ChatGPTSource } from './chatgpt.js';
import { GeminiSource } from './gemini.js';
import { LocalFileSource } from './local.js';
import { GoogleDocsSource } from './google-docs.js';
import { AppleNotesSource } from './apple-notes.js';
import { BookmarkSource } from './bookmarks.js';
import { ClaudeExportSource } from './claude-export.js';
import { ChatGPTExportSource } from './chatgpt-export.js';
import { KnowledgeItem, ExportOptions } from '../types.js';

export class SourceManager {
  private sources: KnowledgeSource[] = [];
  lastCaptureReports: CaptureReport[] = [];

  constructor() {
    // Initialize with default sources
    this.sources.push(new ClaudeSource());
    this.sources.push(new ChatGPTSource());
    this.sources.push(new GeminiSource());
    this.sources.push(new LocalFileSource());
    this.sources.push(new GoogleDocsSource());
    this.sources.push(new ClaudeExportSource());
    this.sources.push(new ChatGPTExportSource());
    // Only add Apple Notes and Bookmarks on macOS
    if (process.platform === 'darwin') {
      this.sources.push(new AppleNotesSource());
      this.sources.push(new BookmarkSource());
    }
  }

  async ingestAll(options: ExportOptions = {}): Promise<KnowledgeItem[]> {
    const allItems: KnowledgeItem[] = [];
    this.lastCaptureReports = [];

    for (const source of this.sources) {
      console.log(`
--- Ingesting from: ${source.name} ---`);
      try {
        const items = await source.exportAll(options);
        allItems.push(...items);
        if (source.lastCaptureReport) this.lastCaptureReports.push(source.lastCaptureReport);
      } catch (error) {
        if (error instanceof IncompleteCaptureError) {
          allItems.push(...error.items);
          this.lastCaptureReports.push(error.report);
          console.error(`Capture ${error.report.status} for ${source.name}; retained ${error.items.length} items. Inspect lastCaptureReports for receipts.`);
        } else {
          this.lastCaptureReports.push({ source: source.id, status: 'failed', receipts: [{ id: source.id, stage: 'export', status: 'failed', reason: 'source-export-failed' }] });
          console.error(`Failed to ingest from ${source.name}.`);
        }
      }
    }

    return allItems;
  }

  async watchAll(callback: (item: KnowledgeItem) => Promise<void>): Promise<void> {
    for (const source of this.sources) {
      if (source.watch) {
        console.log(`--- Starting Watcher: ${source.name} ---`);
        try {
          await source.watch(callback);
        } catch (error) {
          console.error(`❌ Failed to start watcher for ${source.name}:`, error);
        }
      }
    }
  }

  getSource(id: string): KnowledgeSource | undefined {
    return this.sources.find(s => s.id === id);
  }

  getSources(): KnowledgeSource[] {
    return this.sources;
  }
}
