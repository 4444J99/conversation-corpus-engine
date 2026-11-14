import type { CaptureReport } from './capture-report.js';
import { KnowledgeItem, ExportOptions, Conversation, KnowledgeDocument } from '../types.js';

export interface SourceItemReference {
  id: string;
  title: string;
  url?: string;
  metadata?: Record<string, any>;
}

export type SourceItem = Conversation | KnowledgeDocument;

export interface KnowledgeSource {
  id: string;
  name: string;
  type: 'chat' | 'file';
  lastCaptureReport?: CaptureReport | null;
  
  // Lifecycle
  init?(options?: ExportOptions): Promise<void>;
  close?(): Promise<void>;
  
  // Ingestion
  listItems?(): Promise<SourceItemReference[]>;
  exportItem?(id: string): Promise<KnowledgeItem>;
  exportAll(options?: ExportOptions): Promise<KnowledgeItem[]>;
  
  // Real-time
  watch?(callback: (item: KnowledgeItem) => Promise<void>): Promise<void>;
}
