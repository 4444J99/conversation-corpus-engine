# Archive surfaces

One normalized layer, five ways to read it: an **Obsidian vault** (which is also the wiki), a
**static archive site** with an optional local full-text server, a **SQLite FTS5 search database**,
a **graph** (JSONL + GraphML) and an **MCP search tool**. Everything is derived from raw
provider exports, regenerable at any time, and written only into a private corpus store.

```
raw export (read-only)
  └─ archive_read_<provider>.py    streaming readers -> Conversation
       └─ archive_ingest.py        normalized JSONL store + manifest (denominators, hashes)
            └─ archive_topics.py   TF-IDF topic hubs + cosine neighbours (cross-provider)
                 ├─ archive_vault.py     Obsidian vault / wiki
                 ├─ archive_database.py  SQLite FTS5 search database
                 ├─ archive_graph.py     graph nodes, edges and GraphML
                 └─ archive_site.py      static site  (+ archive_serve.py, loopback only)

archive_layout.py mints every file, folder and note name (and every wikilink) with derive_name.
```

## Privacy model

- Raw exports are read, never written, moved or deduplicated. Output may not overlap a source root.
- Every write goes through `authorize_corpus_write`: the destination must be a strict descendant of
  a registered, non-symlinked, non-Git corpus store (see `cce corpus-store register`).
- Commands that only read an archive (`search`, `turns`, `serve`) attach through
  `authorize_corpus_read`: the same registration, path, symlink-on-the-way and Git-privacy checks,
  without the two whole-store walks (nested Git metadata, symlinks) that write custody adds and
  that cost tens of seconds on a large store. It returns a `CorpusReadAuthorization`, which no write
  path accepts. `verify` and every command that writes keep the full write custody.
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

# everything: ingest -> analyze -> vault -> database -> graph -> site -> verify
cce archive build --export-root /path/to/ai-exports \
  --project-root /path/to/cce-site --corpus-store-root /path/to/cce-corpus-store

# the same, with the limits spelled out (these are the defaults)
cce archive build ... --note-bytes 102400 --folder-entries 1800 --shard-bytes 50331648

cce archive search "kubernetes ingress" --provider claude --project-root … --corpus-store-root …
cce archive serve --project-root … --corpus-store-root …      # http://127.0.0.1:8765/
```

The Copilot reader reads SingleFile captures of Copilot web research pages: `.html` or `.htm` files
whose SingleFile header names the Copilot host. Each page becomes one conversation, the report
rendered as markdown with its numbered sources kept in place and in `meta.citations`. The capture
date is the only timestamp, and the pages hold no user turn (none is invented). HTML files that are
not Copilot pages are skipped and counted, and every other file in the folder is counted by
extension and never opened, so a folder of captures with other documents beside them can be read in
place: `--source copilot=/path/to/copilot-captures`.

`--export-root` expects one folder per provider: `chatgpt/`, `claude/`, `gemini/` (including
`NotebookLM/`), `grok/`, `perplexity/`, `copilot/` (the `copilotreports` provider reads the
`.docx`/`.txt` files of the same folder) and `local-memory/` (the `localmemory` provider). The
coding-CLI, notes, Drive, document and agent families come from `--library-root`. Override any with
`--source provider=PATH`. A
ChatGPT account export is a snapshot; pass the live delta with `--chatgpt-extra delta.json` (it wins
over the same conversation in the export). Output defaults to `<store>/archive-all/`. Every name
below is minted by `derive_name` (see *Naming contract*), so `cce archive verify-names` accepts a
whole build:

| Path | What |
|---|---|
| `manifest-all.json` | per-provider counts, denominators (`derived_ids`, `unnameable_ids`, `duplicates_folded`, `duplicates_folded_identical`, `duplicates_folded_differing`), turns by author (`authors`), the shard list with sizes and sha256 (`layout`), channels read / not read, source sha256, naming / schema / reader versions, source fingerprint |
| `provider-<p>/shard-<p>-<yyyymm>.jsonl`, `provider-<p>/index-<p>-<yyyymm>.jsonl` | the provider-neutral store, sliced by provider and month: full conversations, and one light summary row each. A month that outgrows `--shard-bytes` rolls to `-002`, `-003`; a conversation with no real month goes to `undated` |
| `index-all-enrichment.jsonl`, `report-all-topics.json` | topics and nearest neighbours per conversation; the topic hub table |
| `vault-all/` | open this folder in Obsidian; conversations sit in a folder per month (see *The bounded layout*) |
| `manifest-all-vault.json` | the files the vault build generated, kept beside the manifest so the vault holds only notes and Obsidian's own settings; a rebuild prunes what it no longer writes, and removes the `.cce-vault-manifest.json` an earlier build left inside the vault |
| `database-all-turns/` | the turn store: SQLite shards by author (`container-all-authored`, `container-all-machine`), provider and month, each with two FTS5 indexes (see *The two containers*) |
| `graph-all/` | `index-all-nodes.jsonl`, `index-all-edges.jsonl`, `graph-all.graphml`, `report-all-graph.json` |
| `site-all/` | open `home-all.html` directly, or use `cce archive serve`; pages sit in the same month folders as the vault |
| `feed-<p>-prompts.jsonl`, `receipt-<p>-prompts-<16hex>-<yyyymmdd>.json` | the prompt feed and its receipt, written by `cce archive project feed` (see *The prompt feed*) |

### The bounded layout (layout version 3)

An archive that cannot be opened is as lost as one that cannot be read. Two failures ruled it out:
a handful of files so large that no editor, browser or indexer survives them (a 42 MB page for
one thread, a 334 MB search database, 164 MB of ChatGPT in one file), and folders with thousands of
entries (a custody walk over a million files before a server may start). Every surface now keeps to
limits, and `cce archive census` checks any tree against them.

| Limit | Default | What happens past it |
|---|---|---|
| `--note-bytes` | 102,400 (ceiling 131,072) | a note, page or hub page is **split into numbered parts**; nothing is truncated |
| `--folder-entries` | 1,800 (ceiling 2,000) | a month folder spills into one folder per day, a day into hash buckets |
| `--shard-bytes` | 48 MiB (data ceiling 64 MiB) | a provider-month shard rolls to `-002`, `-003` |
| SQLite files | exempt from the size ceiling | they are paged by their engine, never rendered, and sharded by provider and month anyway |
| one oversize conversation | exempt, by name, from the data ceiling | a conversation is never cut, so a row larger than a shard is its own shard; the manifest marks it `oversize_row` and the census lists it under `violations.exempt` with the reason |

- **Splitting is lossless.** A message that does not fit is cut at line boundaries; a fenced code
  block cut this way is closed at the end of the piece and reopened at the start of the next, so
  each piece renders alone; one line longer than the cap is cut on a character boundary. Parts are
  `part-<provider>-p002-<hex>` (the hex hashes the name of the note they continue) in a folder named
  like the conversation note, each linking to the previous part, the start and the next. The
  conversation note stays the first part, so every existing `[[wikilink]]` still resolves.
  `archive_split.rejoin` returns the original text byte for byte, which the tests assert.
- **Hubs paginate too.** A provider, month, topic or collection hub that outgrows a note continues
  on `part-all-p002-<hex>` pages; the hub keeps its own name for page one.
- **Folders.** `vault-all/conversations/month-all-202506/conversation-<uid>.md` (and the same under
  `site-all/conversations/`). The plan is a pure function of the conversations and their part
  counts, so a rebuild places every file where it was. Undated conversations go to `undated`.
- `cce archive verify` fails on any file or folder past its ceiling (except an oversize-row shard
  the manifest names), on a vault or site whose conversation count differs from the manifest, on
  author or shard counts that do not reconcile, and on a family whose counts differ from its
  source's own (the library's record and message counts; local memory's file accounting and the
  proof that every folded conversation is already in its owning provider). A first-generation
  store (no `layout_version`) is not held to the limits.

### The two containers

The atom is the **turn**: one message. Every turn has exactly one author:

- `human` — the person typed or wrote it: role `user`, channel `typed` (a prompt typed into a chat
  or a coding CLI; the prompt feed's own rule, so the human count of a chat provider equals its feed
  rows, less abandoned-branch asks, plus the typed turns the feed skipped for holding no text, plus
  the `written` turns) or channel `written` (text the person authored or supplied outside a chat:
  a note, a Drive document, a file they wrote, a project instruction, a notebook source);
- `machine` — everything else: replies, reasoning, tool output, system text, memories and plans an
  agent wrote, text a harness injected into a user turn, and every prompt a parent agent wrote
  for a subagent.

An author is claimed where the source proves it. Where it cannot, the reader classifies by
provenance (who put the text into the system) and records the basis in the turn's
`meta.author_basis`; the manifest counts the bases per provider (`author_basis`), so the doubt is
visible and countable instead of silent.

`author` is derived from the stored `role` and `channel`, so no conversation row changes. The turn
store holds each turn once, in the container of its author:

```
database-all-turns/
  manifest-all-turns.json
  container-all-authored/provider-chatgpt/shard-chatgpt-202506.sqlite
  container-all-machine/provider-chatgpt/shard-chatgpt-202506.sqlite
```

A shard holds its author's turns, their conversation rows, and FTS5 indexes over text and titles.
`human + machine == messages` per provider and in total (checked by `verify`). Each turn carries
the position of its conversation in the normalized shard (`cseq`), so `archive turns` without an
author merges the two authors of a provider-month back into reading order: shard, conversation,
turn. A conversation with no messages has no turn to hold, so its row goes to the machine
container and it stays findable by title.

```bash
cce archive search "lantern wick" --author human          # what I typed
cce archive search "lantern wick" --author machine --month 202506 --provider chatgpt
cce archive turns --author human --provider chatgpt --month 202506 --limit 20
cce archive census --root "$STORE/archive-all"              # sizes and fan-out, exit 1 on a violation
cce archive fixture --out /tmp/fx --stress 120               # a 380 KB reply and 120 short conversations in one month
```

`/api/search` takes `author` and `month` as well as `provider` and `kind`; the MCP tool
`cce_archive_search` takes `author` and `month`. A store built before the turn store answers from
its single database and reports that it cannot filter by author or month.

### Every family, one atom

The store is two containers of one atom whatever the source: a chat turn, a coding-CLI turn, a
note, a Drive document, a file and an agent memory are all a turn with an author, a provider (the
source family), a source id, an ordinal and a timestamp, sharded and bounded the same way.

| Provider (family) | Source | Human (container 1) | Machine (container 2) |
|---|---|---|---|
| `chatgpt` `claude` `gemini` `grok` `perplexity` `notebooklm` `copilot` | account exports, saved pages | typed prompts; project instructions and documents, notebook sources and notes, a Grok personality (`written`) | replies, reasoning, tool output, memories, notebook chat history and artifacts |
| `claudecode` `codex` `opencode` `antigravity` `geminicli` `copilotcli` | library generation | what the person typed, a slash or shell command, pasted text | subagent prompts, harness-injected text, replies, reasoning, tool output |
| `applenotes` `googledrive` | library generation | every turn (note body, table cells, document text) | — |
| `documents` | library generation (authored files) | every turn (`written`) | — |
| `agentmemory` `agentplans` | library generation (agent memory trees, plans, Codex outputs) | — | every turn |
| `copilotreports` | the `.docx`/`.txt` reports saved beside Copilot pages | — | every turn |
| `localmemory` | the local-session memory folders | — | every record (conversation payloads are folded into their provider, account files excluded) |

**The library bridge.** `cce library build` is the capture pipeline: it reads the coding CLIs'
native stores, the notes and Drive captures and the document roots, merges what several captures
say about one identity, and publishes a verified generation. `cce archive ingest --library-root
<store|library-all|generation>` streams that generation's records into the archive providers
above (`archive_read_library.py`), so nothing is re-read from an original and the archive's counts
are held to the library's own: `verify` fails when a family's conversations plus folded
duplicates differ from the library's record count, or the messages read differ from the library's
message count. The library keeps every document under one provider; the bridge splits them by
provenance into `documents`, `agentmemory` and `agentplans`. The ChatGPT pages the library holds
beyond the account export join `chatgpt` (an id the export already has is folded, the export's
version staying).

**Who typed a CLI turn.** A coding CLI writes everything the model sees as a user turn
(`archive_provenance.py`). A turn is human only in a root session and only when its text is not a
harness wrapper: every user turn of a `subagent` session is a prompt the parent agent wrote, and
a turn that opens with a known injected tag (`system-reminder`, `task-notification`,
`environment_context`, `session_context`, …) or a known harness template (a compaction summary, a
skill body, hook feedback) is machine. A slash command, a shell command and pasted text are the
person's. A tag the rules do not know is machine and counted as `unclassified-tag:<name>`; a
plain-text template the rules do not list stays human, so a CLI family's human count is an upper
bound that the manifest's `author_basis` lets a reader audit. Antigravity's text is recovered from
protobuf leaves with no role, so all of it is machine.

```bash
cce archive ingest --export-root "$EXPORTS" --library-root "$OLD_STORE" --provider claudecode --merge
```

`--merge` keeps the manifest entries of providers a call does not read, so a store this size is
built one provider at a time and a failed one is read again alone; without it the manifest lists
exactly the providers of the call. A conversation larger than a shard (one Antigravity session is
about 170 MiB) is never cut: its shard is that one row, the manifest marks it `oversize_row`, and
the census lists the file under `violations.exempt` with the reason instead of ignoring the
ceiling — a data file past the ceiling that the manifest does not name is still a violation.

### The normalized layer (version 2)

Conversations carry the schema string `archive-conversation.v2`; the manifest carries
`archive-manifest.v2`. Version 1 files still load (missing message fields are filled in on read),
so `verify` and the readers accept a first-generation store. `project feed` and `project corpus`
do not: a version 1 upgrade mints different message ids and counts attachment text as typed, so
those two commands exit non-zero and ask for `cce archive build`.

- Each **message** has an `id`: the vendor message id where the export has one (the ChatGPT node
  id, the Claude message uuid), otherwise `message-<provider>-<16hex>` over provider, conversation
  uid, role, timestamp and text, with an ordinal inside the hashed string for exact repeats. It
  also has a `channel` (`typed`, `reply`, `thinking`, `tool`, `system`, `memory`, `project` or
  `written`) and `typed_chars`, the characters the person typed before any appended attachment
  text.
- A conversation with **no vendor id** (or one the naming contract cannot express) gets
  `derived-<16hex>` over provider, creation stamp and the first three messages' text. The manifest
  counts `derived_ids`, and `unnameable_ids` for the subset that had a vendor id the contract
  cannot express (see *Naming contract*).
- A **repeated uid** keeps its first occurrence: every later conversation with the same uid is
  folded away, whether or not its content matches. The manifest counts them in
  `duplicates_folded`, split into `duplicates_folded_identical` (same content, wherever it was
  read from) and `duplicates_folded_differing` (the first occurrence won over different content).
- ChatGPT messages on abandoned branches are kept in `extra.alternate_messages` (own id, a
  `branch: alternate` flag, the id of the node they hang from); the message list itself remains the
  `current_node` chain, so the counts do not move.
- The **source fingerprint** hashes a canonical copy of the ingest result without absolute paths or
  error text, so the same export read on two hosts gives the same fingerprint.

## Commands

| Command | Purpose |
|---|---|
| `cce archive ingest` | read raw exports into the normalized store and write the manifest |
| `cce archive analyze` | topic hubs and similarity |
| `cce archive vault / database / graph / site` | one emitter each (`database` builds the turn store) |
| `cce archive build` | every stage, then `verify` |
| `cce archive verify` | cross-check every surface against the manifest denominators |
| `cce archive search QUERY` | FTS5 search (`--provider`, `--kind`, `--author`, `--month`, `--limit`, `--titles`, `--json`) |
| `cce archive turns` | list the turns of a slice in reading order (`--author`, `--provider`, `--month`, `--uid`, `--limit`, `--offset`) |
| `cce archive census --root DIR` | check a tree against the size and folder limits; exit 1 on a violation |
| `cce archive serve` | site + `/api/search` on `127.0.0.1` (read-only) |
| `cce archive names` | print the naming contract: kinds, scopes, version (`--json`) |
| `cce archive verify-names --root DIR` | check every file and directory under `DIR` against the naming contract |
| `cce archive project feed --root ARCHIVE --out DIR` | write each provider's prompt feed and receipt (`--provider` to pick) |
| `cce archive project corpus --root ARCHIVE --out DIR` | write each provider's engine corpus folder for the federation (`--provider` to pick) |
| `cce archive fixture --out DIR [--stress N]` | write deterministic synthetic ChatGPT and Claude exports that share one ask; `--stress` adds one very long thread and N short conversations in one month |

## The prompt feed

`cce archive project feed --root <archive-all> --out <dir>` turns the normalized layer into one
file per provider, `feed-<provider>-prompts.jsonl`, with one row per ask the person typed, plus
`receipt-<provider>-prompts-<16hex>-<yyyymmdd>.json` beside it. Feeds hold private conversation
text, so `--out` must be outside any Git work tree or ignored by it; otherwise the command refuses
(exit 2).

- **Which asks.** Role `user`, channel `typed`, in a conversation of kind `chat`, `search` or
  `activity`. ChatGPT asks on abandoned branches (edits, regenerations) are included and carry
  `branch: alternate`; within a conversation they follow the current-branch asks. Projects,
  memories and notebooks are excluded. If a reader appended attachment text to an ask, only the
  typed part (`typed_chars`) enters the feed.
- **Row fields.** `source` = `archive-<provider>-messages`, `session_ref` = the conversation uid,
  `event_ref` = the stable `message-<provider>-<16hex>` reference of the message (the vendor id
  hashed with the conversation uid, so it is unique across conversations), `event_index` = the
  ordinal in the feed, `text_index` = 0, `text`, `timestamp`, `source_locator` =
  `<uid>#<event_ref>`, `body_kind` = `direct`, `provenance` = `unknown_user_input`. The writer has
  no provenance parameter: a vendor export can never claim to be operator-typed input.
- **Determinism.** Rows are written in the store's reading order (conversation shards oldest
  month first, `undated` last, the reader's order inside a shard), then message order, with sorted
  keys, so two runs over one store give identical bytes. A repeated `event_ref` aborts the whole
  write and leaves no file behind. A store that disagrees with its manifest is refused.
- **Order is not identity.** A first-generation store kept conversations in the reader's emission
  order, so the feed of a rebuilt (sharded) store holds the same rows (same `session_ref`,
  `event_ref`, `text`, `timestamp`, `source_locator`) at other positions: `event_index` and the
  feed's sha256 change, nothing else does. A consumer keys a row on `(session_ref, event_ref)`
  and treats `event_index` as the row's place in this one file.
- **The receipt** holds counts and digests, never text: the feed's sha256, the row count, the
  alternate-branch count, the number of asks trimmed of attachment text, the excluded messages
  counted by reason (`kind-notebook`, `kind-memory`, `role-assistant`, `role-tool`, `empty-text`,
  ...), `messages_seen` (which always equals rows plus excluded), the manifest's source
  fingerprint, `NAMING_VERSION` and the reader versions. Its date is the manifest's latest-activity
  day. A newer receipt for the same provider replaces older ones.

`cce archive fixture --out <dir>` writes invented ChatGPT and Claude exports (a conversation tree
with an edited ask, a Claude chat with reasoning and a tool call, a project and a memory archive)
whose first conversations share one identical ask. The bytes are the same on every run, so the
manifest fingerprint is too. `fixture-all.json` beside them records the digest of the shared ask
and the number of asks each provider should yield.

## The corpus projection

`cce archive project corpus --root <archive-all> --out <store>` shapes the normalized layer as
engine corpora, so the federation can accept archive text. For each provider it writes
`corpus-<provider>-archive/`, the folder that is also the registry key and the `corpus_id` in its
`contract.json` (checked against `corpus-contract.schema.json` before anything is swapped in). Each
folder holds the five collections federation requires (`threads-index`, `semantic-v3-index`,
`pairs-index`, `doctrine-briefs`, `family-dossiers`) and the four it uses when present
(`canonical-families`, `action-ledger`, `unresolved-ledger`, `canonical-entities`). Register one
with `upsert_corpus(project_root, <store>/corpus-<provider>-archive, corpus_id=<same name>)`.
Like the feed, the output holds private text, so `--out` must be outside any Git work tree or
ignored by it.

- **Which text.** The current branch only. Conversations of kind `chat`, `search` or `activity`;
  projects, memories and notebooks are counted as excluded. Per message, channels `typed` and
  `reply`: reasoning, tool output and system text stay out, and a user message is cut at
  `typed_chars`. Off-branch messages stay in the archive.
- **Ids.** Every id is minted by `derive_name` from the conversation uid and digests of exact
  text, never from a title or a slug of a sentence: `thread-<provider>-<uid tail>`,
  `family-<provider>-<uid tail>`, `pair-<provider>-<16hex>` (conversation uid plus the opening
  message's stable reference), `action-<provider>-<16hex>` and `question-<provider>-<16hex>`
  (the exact sentence), and `topic-all-<term>` for a concept entity (the digest of the term when
  it is not one valid word). Renaming a conversation changes no id. The same sentence in two
  conversations is one action; a name collision aborts the run.
- **Families.** A conversation is its own family. Joining families across conversations or
  providers is a reviewed federation decision, not a projector guess.
- **Heuristics.** Sentence splitting, keywords, vector terms, action and open-question
  detection, concept entities and the prompt/response pairing are the functions in `corpus_text`,
  the one module the two export importers also use.
- **Repeatable.** Two runs write the same bytes (every timestamp is read from the manifest). The
  corpus is built beside its final place and swapped in, so a failed run, including a store that
  disagrees with its manifest, leaves the previous corpus untouched.
- **Nothing eligible.** A provider whose conversations are all excluded writes no corpus and
  reports `status: empty`. If an earlier run left `corpus-<provider>-archive` in `--out`, it is
  removed (`removed_stale: true`) so its text is not federated; only a folder this projector wrote
  is ever removed. A project that still registers the folder sees it as an invalid contract with
  no content, which the federation tolerates, until the entry is unregistered.

The export importers (`import_chatgpt_export_corpus`, `import_claude_export_corpus`) are not yet
rewired onto the archive. They share `corpus_text` with the projector and stay until a golden
comparison of an importer corpus against the projected one lands. Follow-up: importer-to-projector
golden comparison, before either `import_*_export_corpus` module is retired.

A projected corpus folder keeps the engine's own collection names inside (`corpus/threads-index`
and the rest), so `verify-names` applies to the archive folder and to a store's top-level
folders, not to the inside of a corpus. A federation build rewrites each corpus's `contract.json`
(clock time, five counts), so the file is no longer byte-identical to a fresh projection after one.

## Naming contract

Every name the archive layer mints comes from `archive_naming.derive_name`:
`kind [-scope] [-subject] [-identity] [-date]`, lowercase words joined by single hyphens, at most
80 characters (an over-long name is an error, never truncated). The kind is one word from a closed
table that says which of the other segments each kind requires or forbids; the scope is a provider
word or `all`; a subject is validated, never derived from a title; an identity is either a plain
vendor identifier used as-is or the first 16 hex characters of SHA-256 over the exact identity
string; the date is a real calendar day taken from the data and appears only on kinds that require
it. The conversation name is `conversation-` plus today's `stable_uid`, so existing uids are
reproduced byte for byte. One disclosed deviation from "`stable_uid` is frozen": a vendor id that
is valid for `stable_uid` but that the contract cannot express as a name (an edge or doubled
hyphen, or a name over 80 characters) is replaced by a derived id, with the original kept in
`extra.vendor_id`, and the manifest counts it in `unnameable_ids`. `NAMING_VERSION` is recorded in every receipt; adding a kind or widening one bumps it (1: the first table; 2: the `manifest` kind accepts an optional subject; 3: the kinds `shard`, `part`, `day`, `bucket` and `container`, and the structural folder word `undated`; 4: the provider words of the families that are not web chat exports: `claudecode`, `codex`, `opencode`, `antigravity`, `geminicli`, `copilotcli`, `applenotes`, `googledrive`, `documents`, `agentmemory`, `agentplans`, `copilotreports`, `localmemory`), and names minted under an earlier version stay valid.
`cce archive names --json` prints the full table and `cce archive verify-names` checks a tree. Only
four plain folder words are outside the grammar (`conversations`, `hubs`, `meta`, `undated`); everything
else a build writes is a minted name, which `verify-names` confirms on a whole archive folder.

`build` and `verify` exit non-zero if a provider failed to read, a vault wikilink dangles, the
turn store disagrees with the manifest, the vault or the site has a different number of
conversation notes or pages than the manifest has conversations, the author or shard counts do not
reconcile, or a file or folder is past its ceiling. A failing provider never stops the others.

## The vault is the wiki

The vault layout is lowercase throughout: `conversations/` (one `conversation-<uid>.md` per
conversation, in a folder per month such as `month-all-202506/`, or in `undated/`; a thread too
long for one note continues in `part-...` notes in a folder named like the note), `hubs/`, `meta/`,
and `home-all.md` at the top. `home-all.md` states the
denominators. `hubs/` holds one note per provider (`provider-chatgpt`), month
(`month-all-202506`), topic (`topic-all-<term>`) and collection (`collection-<provider>-<subject>`;
a project, notebook or GPT whose identifier is not a valid subject word is named by its 16 hex
character digest), so Obsidian's backlinks, graph and search work out of the box; the graph is
coloured by provider via `.obsidian/graph.json`. `meta/` holds `coverage-all.md` and
`guide-all.md`. An undated conversation has no month note, and a topic term that is not one plain
lowercase word has no hub (it stays a plain label; the build reports `topics_without_hub`).
Titles are validated against the naming contract, never derived from a conversation title.
Every `[[wikilink]]` is written by one function, `archive_layout.wikilink`, from the same name the
target file is given. Each conversation note has
frontmatter (`uid`, `provider`, `kind`, `created`, `tags`, `source_sha256`, …), a *Related* line
of nearest neighbours and the transcript, with reasoning / tool / source messages collapsed.

Message text is neutralised: `[[links]]`, `![[embeds]]`, `#tags` and leading `#` headings in a
conversation cannot create vault structure. Rebuilds track the files they generated, prune the
stale ones, and never touch notes you add yourself.

## Data-grounding rules the surfaces follow

- Scope is stated, not implied: every provider reports discovered / empty / duplicate / skipped
  counts and which export channels were and were not read (`meta/coverage-all.md`, site *Coverage*).
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

## Custody of the raw exports

The raw provider exports this layer reads have their own custody record:
[`custody/ai-exports-custody.md`](custody/ai-exports-custody.md) and its receipt. It replaces the Custody
section of the archived `4444J99/limen` architecture page, which is stale and frozen. Read it before building
a store from, or deleting anything derived from, the raw exports: it says where they live, which files exist
only once, and which independent copy has been verified.

## Known limits

- Topic hubs are lexical (TF-IDF); they are a navigation aid, not a semantic ontology.
- Attachment contents (images, PDFs, audio) are not searchable.
- The bridge from this layer into engine corpora and federation (families, entities, review
  queue) is not built yet; the two layers share a store but not an index.
