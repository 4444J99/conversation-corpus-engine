export interface DeviceGrant {
  schema: "knovv-device.v1";
  id: string;
  signing_public: string;
  generation: number;
  revoked: boolean;
  signature: string;
}
export interface Revision {
  schema: "knovv-revision.v1";
  id: string;
  doc: string;
  parent: string | null;
  object: string;
  device: string;
  epoch: number;
  signature: string;
}
export interface Publication {
  schema: "knovv-publication.v1";
  vault: string;
  generation: number;
  epoch: number;
  parent: string | null;
  manifest: string;
  envelopes: string[];
  signature: string;
}
export interface SourceSpan {
  uid: string;
  version: string;
  digest?: string;
  message: number;
  start: number;
  end: number;
  sequence?: string;
}
export interface Document {
  id: string;
  kind: "wiki" | "project" | "zettel" | "source" | "capture";
  title: string;
  body: string;
  summary: string;
  author: string;
  last_editor?: string;
  authorship_basis: string;
  status: string;
  created: string;
  updated: string;
  sources: SourceSpan[];
  links: string[];
  tags: string[];
  logical_id?: string;
  provenance?: Record<string, unknown>;
  source_url?: string;
  relationships?: { type: string; target: string }[];
  source_history?: { version: string; id: string }[];
  attachments?: AttachmentRef[];
}
export interface AttachmentRef {
  object: string;
  name: string;
  media_type: string;
  size: number;
  epoch: number;
}
export interface DocumentMeta extends Omit<Document, "body"> {
  ordinal: number;
  object: string;
}
export interface Manifest {
  schema: "knovv-catalog.v1";
  vault: string;
  epoch: number;
  id_key: string;
  count: number;
  metadata: string[];
  lookup: Record<string, string>;
  search: Record<string, string>;
  search_prefix?: 2 | 4;
  resolve?: Record<string, string>;
  epoch_keys?: Record<string, string>;
  navigation: DocumentMeta[];
  receipts: Record<string, unknown>[];
  publication_time: string;
}
export interface DevicePackage {
  schema: "knovv-device-package.v1";
  credential: Record<string, string>;
  trust: { owner: string; vault: string; generation: number; head: string };
}
export interface JournalEntry {
  seq: number;
  envelope: Revision;
}
