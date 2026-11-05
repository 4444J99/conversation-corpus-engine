# Omni-Source Knowledge Consolidation — Master Plan
**Project:** `my-knowledge-base` · **Date:** 2026-06-06 · **Status:** ACTIVE

> **Goal:** Every piece of knowledge you've ever produced or consumed — conversations, docs, notes,
> bookmarks, reading history, repos — ingested, atomized, enriched, and unified into a single
> queryable knowledge base, with manifests exported to Obsidian and Notion.

---

## Current Infrastructure (What Already Exists)

| Adapter | File | Status |
|---------|------|--------|
| Claude exports | `src/sources/claude.ts` | ✅ Working |
| ChatGPT exports | `src/sources/chatgpt.ts` | ✅ Built |
| Gemini exports | `src/sources/gemini.ts` | ✅ Built |
| Apple Notes | `src/sources/apple-notes.ts` | ✅ Built |
| Browser bookmarks | `src/sources/bookmarks.ts` | ✅ Built |
| Web clipper | `src/sources/web-clipper.ts` | ✅ Built |
| Google Docs | `src/sources/google-docs.ts` | ✅ Built |
| Local files | `src/sources/local.ts` | ✅ Working |
| Obsidian export | `src/export-obsidian.ts` | ✅ Built |
| Notion | ❌ Not started | TODO |
| Grok / Perplexity | ❌ Not started | TODO |
| Wikipedia (from history) | ❌ Not started | TODO |
| iOS Notes (direct) | ❌ Not started | TODO |
| Reading lists | ❌ Not started | TODO |

---

## Wave 1 — Finish Wiki + Verify Pipeline (TODAY)

> Gentle, background work. No new code.

- [x] Phase A: Topic extraction resumed at 1 req/s (running now)
- [ ] Phase B: `npm run wiki:synth -- --rps 1`
- [ ] Phase C: `npm run wiki:link`
- [ ] Review `compiled-wiki/index.md` — sanity check quality
- [ ] Export to Obsidian: `npm run export:obsidian`

---

## Wave 2 — AI Conversation Archives (HIGH VALUE, EASY)

> All your thinking, crystallized. Huge ROI.

### 2a. ChatGPT Export
- **How:** ChatGPT Settings → Data Controls → Export Data → download ZIP
- **Contains:** All conversations as `conversations.json`
- **Adapter:** `src/sources/chatgpt.ts` already exists
- **Action:**
  ```bash
  # Place export ZIP in intake/chatgpt/
  npm run ingest -- --source=chatgpt --path=intake/chatgpt/
  ```

### 2b. Gemini Export
- **How:** Google Takeout → select "Gemini Apps Activity" → download
- **Contains:** Conversation JSON + HTML
- **Adapter:** `src/sources/gemini.ts` already exists
- **Action:** Similar to ChatGPT

### 2c. Grok (X/Twitter)
- **How:** X Settings → Your Account → Download archive → includes Grok
- **Adapter:** Needs to be built (parse `grok-data.js` from archive)
- **Effort:** ~2 hrs

### 2d. Perplexity
- **How:** Perplexity Settings → Export → JSON
- **Adapter:** Needs to be built (similar structure to ChatGPT)
- **Effort:** ~1 hr

---

## Wave 3 — Documents & Notes (HIGH VALUE)

### 3a. Apple Notes / iOS Notes
- **How:** JXA script on Mac reads Notes.app directly (no export needed)
- **Adapter:** `src/sources/apple-notes.ts` exists — needs testing
- **Action:** `npm run ingest -- --source=apple-notes`

### 3b. Google Docs
- **How:** Google OAuth + Drive API (or Takeout)
- **Adapter:** `src/sources/google-docs.ts` + `src/sources/google-auth.ts` exist
- **Needs:** OAuth credentials in `.env`
- **Fallback:** Google Takeout → "Google Docs" → downloads as `.docx`

### 3c. Obsidian Vault
- **How:** Direct local file read — it's just Markdown
- **Adapter:** Use `local.ts` with path pointing to Obsidian vault
- **Action:** Add entry to `config/sources.yaml`

### 3d. Notion
- **How:** Notion Settings → Settings → Export content → Markdown + CSV
- **Adapter:** Needs to be built (parse exported MD/CSV)
- **Effort:** ~2 hrs (mostly path normalization)
- **Alt:** Notion API with `notion-client` (incremental sync)

---

## Wave 4 — Browser Intelligence (UNIQUE VALUE)

> This is where you turn passive consumption into active knowledge.

### 4a. Bookmarks
- **How:** Export from Chrome/Safari/Firefox as HTML (`bookmarks.html`)
- **Adapter:** `src/sources/bookmarks.ts` exists
- **Processing:** Parse titles + URLs → store as atomic units with tags

### 4b. Browser History
- **How:**
  - **Chrome:** Copy `~/Library/Application Support/Google/Chrome/Default/History` (SQLite)
  - **Safari:** Copy `~/Library/Safari/History.db` (SQLite)
- **Adapter:** Needs to be built (read SQLite, filter by domain/date)
- **Effort:** ~2 hrs

### 4c. Reading Lists
- **Safari:** `~/Library/Safari/Bookmarks.plist` (ReadingList array)
- **Chrome:** `Bookmarks` file (JSON, "Other Bookmarks > Reading list")
- **Processing:** Same as bookmarks but tagged as `reading-list`

### 4d. Wikipedia Articles (Fetched)
- **Source:** Extract Wikipedia URLs from browser history
- **Processing:** Fetch article content via Wikipedia API, store summary + sections
- **Unique value:** Converts passive reading into searchable knowledge units
- **Effort:** ~3 hrs (history extraction + Wikipedia fetch + dedup)

---

## Wave 5 — Repo Intelligence

> Turn your codebase history into structured knowledge.

### 5a. Local & Remote Repos
- **Scope:** All repos under `~/Code`, `~/Workspace`, `~/Projects`
- **What to extract:**
  - `README.md`, `CHANGELOG.md`, `docs/` trees → atomize
  - Git commit messages (narrative of decisions over time)
  - Issue/PR text (from `.github/` or fetched via GitHub API)
- **Adapter:** Extend `local.ts` with git-aware mode
- **config/sources.yaml:** Enable `workspace-projects`, add `~/Code`

### 5b. GitHub Issues/PRs
- **How:** GitHub CLI (`gh`) or GitHub API
- **What:** Closed issues, PRs with discussions = rich decision history
- **Effort:** ~3 hrs

---

## Wave 6 — Enrichment Pipeline (AFTER INGESTION)

> Once data is in, make it smart.

### 6a. Data Cleanup
- Dedup: detect near-identical atomic units (cosine similarity > 0.97 → merge)
- Normalize: strip export artifacts, fix encoding, sanitize HTML remnants
- Tag: auto-tag by source (`[claude]`, `[chatgpt]`, `[notes]`, etc.)

### 6b. Semantic Enrichment
```bash
npm run generate-embeddings -- --yes      # vector embeddings for all new units
npm run smart-tag -- --limit 500 --save  # AI tagging
npm run extract-insights all --save      # insight extraction
npm run find-relationships --limit 20 --save  # cross-source links
```

### 6c. Summarization
```bash
npm run summarize all --save             # conversation summaries
```

---

## Wave 7 — Manifests & Export

### 7a. Obsidian Manifest
- **What:** Full vault export with wikilinks, frontmatter, tags
- **How:** `npm run export:obsidian` (already built)
- **Output:** Obsidian-compatible vault folder

### 7b. Notion Manifest
- **What:** Structured database view — searchable table of all knowledge units
- **How:** Notion API → create/update pages
- **Effort:** ~4 hrs (Notion client setup + page templating)

### 7c. Ongoing Sync
- Obsidian: chokidar watcher → auto-re-export on change
- Notion: webhook or cron sync

---

## Prioritized Execution Order

```
Wave 1  ███████ TODAY     — Wiki pipeline finish + Obsidian export
Wave 2  ██████  THIS WEEK — ChatGPT, Gemini, Grok, Perplexity exports
Wave 3  █████   THIS WEEK — Apple Notes, Google Docs, Obsidian vault, Notion export
Wave 4  ████    NEXT WEEK — Bookmarks, History, Reading lists, Wikipedia
Wave 5  ███     NEXT WEEK — Repo READMEs, commit history, GitHub issues
Wave 6  ██████  ONGOING   — Enrichment runs (can parallelize with ingestion)
Wave 7  ████    AFTER W3  — Notion adapter, Obsidian sync
```

---

## Open Questions / Decisions Needed

1. **Google OAuth:** Do you have API credentials for Google Docs/Drive, or prefer Takeout? <!-- allow-secret -->
2. **Notion API token:** Do you want API sync (live) or one-time export (simpler)? <!-- allow-secret -->
3. **Browser:** Primary browser — Chrome, Safari, or both?
4. **Repo scope:** Which paths? `~/Code` + `~/Workspace` + `~/Projects`?
5. **Wikipedia depth:** Just titles/summaries, or full article sections?
6. **Grok/Perplexity:** Do you have exports available, or need scrapers?

---

## Files to Create / Modify

| File | Action |
|------|--------|
| `src/sources/grok.ts` | Create |
| `src/sources/perplexity.ts` | Create |
| `src/sources/browser-history.ts` | Create |
| `src/sources/wikipedia-fetch.ts` | Create |
| `src/sources/notion-export.ts` | Create |
| `config/sources.yaml` | Enable workspace, add Code dir |
| `src/export-notion.ts` | Create |
| `.gemini/plans/2026-06-06-omnisource-master-plan.md` | Save plan |
