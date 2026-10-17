# Archive surfaces

One normalized layer, five ways to read it: an **Obsidian vault** (which is also the wiki), a
**static archive site** with an optional local full-text server, a **SQLite FTS5 knowledge base**,
a **graph** (JSONL + GraphML) and an **MCP search tool**. Everything is derived from raw
provider exports, regenerable at any time, and written only into a private corpus store.

```
raw export (read-only)
  └─ archive_read_<provider>.py    streaming readers -> Conversation
       └─ archive_ingest.py        normalized JSONL store + manifest (denominators, hashes)
            └─ archive_topics.py   TF-IDF topic hubs + cosine neighbours (cross-provider)
                 ├─ archive_vault.py   Obsidian vault / wiki
                 ├─ archive_kb.py      SQLite FTS5 knowledge base
                 ├─ archive_graph.py   nodes.jsonl, edges.jsonl, archive.graphml
                 └─ archive_site.py    static site  (+ archive_serve.py, loopback only)
```

## Privacy model

- Raw exports are read, never written, moved or deduplicated. Output may not overlap a source root.
- Every write goes through `authorize_corpus_write`: the destination must be a strict descendant of
  a registered, non-symlinked, non-Git corpus store (see `cce corpus-store register`).
- **Attachment bytes are never ingested.** Attachments appear as file names (and sizes) only.
  Claude's `extracted_content` is dropped unless `--include-attachment-text` is passed.
- Account identity, billing, login history and settings files are listed as *not read*, on purpose.
- Of Gemini's Takeout, only Gemini Apps, AI Mode, Gemini in Workspace and the scheduled-actions
  export are read; the other `My Activity` products are out of scope and reported as such.
- Turns the user deleted at the source (`turn_deleted_time`) are skipped and counted.
- The site is script-free per conversation, builds DOM with `textContent` only, ships a strict CSP
  and a loopback-only, read-only server that validates the `Host` header.

## Quick start

```bash
# once: a private store outside Git, registered against a project root
mkdir -m 700 /path/to/cce-corpus-store /path/to/cce-site
cce corpus-store register --root /path/to/cce-corpus-store --project-root /path/to/cce-site

# everything: ingest -> analyze -> vault -> kb -> graph -> site -> verify
cce archive build --export-root /path/to/ai-exports \
  --project-root /path/to/cce-site --corpus-store-root /path/to/cce-corpus-store

cce archive search "kubernetes ingress" --provider claude --project-root … --corpus-store-root …
cce archive serve --project-root … --corpus-store-root …      # http://127.0.0.1:8765/
```

`--export-root` expects one folder per provider: `chatgpt/`, `claude/`, `gemini/` (including
`NotebookLM/`), `grok/`, `perplexity/`. Override any with `--source provider=PATH`. A ChatGPT
account export is a snapshot; pass the live delta with `--chatgpt-extra delta.json` (it wins over
the same conversation in the export). Output defaults to `<store>/_archive/`:

| Path | What |
|---|---|
| `manifest.json` | per-provider counts, denominators, channels read / not read, source sha256 |
| `normalized/<provider>/{conversations,index}.jsonl` | the provider-neutral store |
| `analysis/` | `enrichment.jsonl` (topics, neighbours per conversation), `topics.json` |
| `vault/` | open this folder in Obsidian |
| `kb/archive.sqlite` | FTS5 knowledge base |
| `graph/` | `nodes.jsonl`, `edges.jsonl`, `archive.graphml`, `summary.json` |
| `site/` | open `index.html` directly, or use `cce archive serve` |

## Commands

| Command | Purpose |
|---|---|
| `cce archive ingest` | read raw exports into the normalized store and write the manifest |
| `cce archive analyze` | topic hubs and similarity |
| `cce archive vault / kb / graph / site` | one emitter each |
| `cce archive build` | every stage, then `verify` |
| `cce archive verify` | cross-check every surface against the manifest denominators |
| `cce archive search QUERY` | FTS5 search (`--provider`, `--kind`, `--limit`, `--titles`, `--json`) |
| `cce archive serve` | site + `/api/search` on `127.0.0.1` (read-only) |
| `cce archive names` | print the naming contract: kinds, scopes, version (`--json`) |
| `cce archive verify-names --root DIR` | check every file and directory under `DIR` against the naming contract |

## Naming contract

Every name the archive layer mints comes from `archive_naming.derive_name`:
`kind [-scope] [-subject] [-identity] [-date]`, lowercase words joined by single hyphens, at most
80 characters (an over-long name is an error, never truncated). The kind is one word from a closed
table that says which of the other segments each kind requires or forbids; the scope is a provider
word or `all`; a subject is validated, never derived from a title; an identity is either a plain
vendor identifier used as-is or the first 16 hex characters of SHA-256 over the exact identity
string; the date is a real calendar day taken from the data and appears only on kinds that require
it. The conversation name is `conversation-` plus today's `stable_uid`, so existing uids are
reproduced byte for byte. `NAMING_VERSION` is recorded in every receipt; adding a kind bumps it.
`cce archive names --json` prints the full table and `cce archive verify-names` checks a tree.

`build` and `verify` exit non-zero if a provider failed to read, a vault wikilink dangles, the
knowledge base disagrees with the manifest, or the site has a different number of conversation
pages than the manifest has conversations. A failing provider never stops the others.

## The vault is the wiki

`Home.md` states the denominators. `Hubs/` holds one note per provider, month, topic and
collection (project / notebook / GPT), so Obsidian's backlinks, graph and search work out of the
box; the graph is coloured by provider via `.obsidian/graph.json`. Each conversation note has
frontmatter (`uid`, `provider`, `kind`, `created`, `tags`, `source_sha256`, …), a *Related* line
of nearest neighbours and the transcript, with reasoning / tool / source messages collapsed.

Message text is neutralised: `[[links]]`, `![[embeds]]`, `#tags` and leading `#` headings in a
conversation cannot create vault structure. Rebuilds track the files they generated, prune the
stale ones, and never touch notes you add yourself.

## Data-grounding rules the surfaces follow

- Scope is stated, not implied: every provider reports discovered / empty / duplicate / skipped
  counts and which export channels were and were not read (`Meta/coverage.md`, site *Coverage*).
- A ChatGPT conversation is linearised along `current_node`; hidden scaffolding, abandoned
  regenerations and empty nodes are dropped and **counted**, with the identity
  `message_nodes = kept + hidden + dropped + off_branch` recorded in the manifest.
- Conversation text records **speech acts, not events**. Every note and page says so; corroborate
  with commits, transactions or files before treating a plan as done.
- "Messages" in this archive are visible, non-empty messages on the live branch — a stricter
  denominator than raw message nodes. Quote the manifest, not memory.

## Adding a provider

A reader is a module with `PROVIDER`, `READER` (version string), `detect(root)`,
`channels(root)` and `iter_conversations(root, tracker)` yielding `Conversation` objects. Use the
tracker for provenance (`tracker.provenance(path)`) and for counters (`tracker.note(name)`).
Register it in `PROVIDER_SPECS` in `archive_ingest.py` and give it a synthetic fixture in
`tests/archive_support.py` — never a real export.

## Known limits

- Topic hubs are lexical (TF-IDF); they are a navigation aid, not a semantic ontology.
- Attachment contents (images, PDFs, audio) are not searchable.
- The bridge from this layer into engine corpora and federation (families, entities, review
  queue) is not built yet; the two layers share a store but not an index.
