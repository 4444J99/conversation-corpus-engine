# Archive surfaces — normalized layer, native readers, vault / site / KB / graph

Issue: #74
PR: #75

## Objective

Turn the private AI-exports corpus (ChatGPT, Claude, Gemini Takeout + NotebookLM, Grok,
Perplexity) into browsable, searchable, linkable surfaces: a knowledge base, an archive with a
front end, a wiki, an Obsidian vault and a graph — from one provider-neutral layer, with no
private text ever entering git.

## Gap this closes

- Gemini / Grok / Perplexity exports go through the generic `document-export` path today:
  flattened markdown, conversation structure lost, no per-message provenance.
- No surface emits something a human can browse, search, or open in Obsidian.
- The engine corpus (threads / families / entities) is an analysis layer; it has no reader-facing
  projection and no cross-provider graph.

## Design

One normalized layer, many emitters. Nothing downstream reads a vendor shape twice.

```
raw export (read-only)
  └─ archive_read_<provider>.py   streaming readers -> Conversation
       └─ archive_ingest.py       normalized JSONL store + manifest + denominators (private store)
            ├─ archive_vault.py   Obsidian vault (= wiki); hubs, topics, backlinks, graph colours
            ├─ archive_kb.py      SQLite FTS5 knowledge base + search
            ├─ archive_graph.py   nodes/edges JSONL + GraphML
            └─ archive_site.py    static front end + local FTS server (stdlib http.server)
```

- Schema `cce.archive.conversation.v1`: provider, source id, title, created/updated, kind
  (`chat` | `search` | `activity` | `notebook`), messages `[{role, ts, text, attachments[]}]`,
  collection (project / notebook / memory scope), provenance `{source_file, source_sha256}`.
- Attachments: filenames and sizes only. Contents are never ingested (the Gemini activity folder
  carries tax / health / legal documents); `attachments_root` is a configurable pointer.
- Custody: every write goes through `authorize_corpus_write` — destination must be a strict
  descendant of the registered, non-git corpus store and must not overlap a source root.
- All projections are derived and regenerable. Durability = source custody + code in git.
- Determinism: stable ids (`provider:source-id` slugs), sorted iteration, no wall-clock in
  content; second build is byte-identical (tested).
- Scale: 16 GB host. Readers stream one shard / one conversation at a time; the ingest never
  holds the corpus in memory. Gemini `MyActivity.json` (45 MB) and Claude `conversations.json`
  (97 MB) are loaded once, sequentially.

## Data-grounding obligations (per limen data-grounding skill)

- Every build writes `manifest.json`: per provider the discovered / ingested / skipped counts,
  message totals, date range, and the explicit list of channels read and not read.
- Home / index notes print the denominators. A window is never stated in the corpus's language.
- Conversation text is speech acts, not events: notes carry a standing banner saying so.

## Out of scope (tracked)

- Bridge normalized layer -> engine corpora / federation (families, entities, actions).
- Offsite encrypted copy; custody of non-ChatGPT raw exports (single copy on Scratch, same
  physical disk as Archive4T).
- Attachment content extraction (OCR, PDF text) — privacy gate.

## Verification

- `pytest` with synthetic fixtures per provider (no real text), asserting counts, determinism,
  HTML escaping, wikilink integrity, FTS round-trip, custody refusal, no message body under any
  tracked path.
- `ruff check` + `ruff format --check`.
- Real-data run against `/Volumes/Scratch/ai-exports` into a private store; report denominators.
