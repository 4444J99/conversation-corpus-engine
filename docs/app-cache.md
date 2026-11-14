# Desktop-app cache recovery (`cce app-cache`)

Three providers' archives end where their last export ended. Their desktop apps keep local files
that hold more recent data of the person's own account. `cce app-cache` reads those files and
stages them in the shape the existing readers ingest. It is read-only against the app's folders and
writes only into a new, empty `--out` directory (mode 0700) that belongs in the private store, never
in a repository.

What it never does: make a network request, read cookies, tokens or any credential store, or try
to open an encrypted container. (The older `*_local_session` importers authenticate with an app's
cookie jar; this path does not.)

| Provider | Source (read-only) | Output | Ingested by |
|---|---|---|---|
| ChatGPT | the Codex/ChatGPT app's Chromium HTTP cache (`~/Library/Caches/Codex/Default/Cache/Cache_Data` and the `codex-browser-app` partition) | `conversations-000.json`, `cache-index.json`, `coverage.json` | `archive_read_chatgpt` |
| Perplexity | `~/Library/Caches/ai.perplexity.macv3/Cache.db` (+ `fsCachedData`) | `conversations-000.json`, `coverage.json` | `archive_read_perplexity` |
| Gemini | `~/Library/Caches/com.google.GeminiMacOS/Gemini/user1/ChatInfo2.store` | `gemini-store-inventory.json`, `coverage.json` | nothing: metadata only |

## ChatGPT

`cce app-cache chatgpt --out DIR [--root CACHE_DATA ...]`. Each entry's body is checked against the
CRC32 the cache stores before it is used. A conversation is staged only when its full `mapping` was
cached (the newest cached version wins). Conversations seen only in list pages or metadata fetches go
to `cache-index.json` with `body_cached: false`; paged `/messages` fetches are windows of a
conversation and are counted, not staged. Brotli bodies need the `brotli` module or command; without
one they are reported as `unsupported:br`.

## Perplexity

`cce app-cache perplexity --out DIR`. The cache holds Space knowledge notes and follow-up suggestions
Perplexity generated for a thread. Neither is the person's own typing: suggestions are staged with the
engine mode `COMPUTER_FOLLOWUP` and a "Perplexity-generated" label. The person's typed prompts are not
stored in these files; only Perplexity's own export has them. The shared container's clipboard history
is the system clipboard; it is counted in `coverage.json` and never read.

## Gemini

`cce app-cache gemini --out DIR`. `ChatInfo2.store` lists chats and cached messages, but every body is
in `ZENCRYPTEDPROTOBYTES`, an encrypted container. A SQLite authorizer on the connection denies any
read of that column. The report holds counts, last-updated times and sizes.

To bring the text in, use the export the reader already understands: Google Takeout, product
**My Activity > Gemini Apps**, format **JSON**. Place the extracted `Takeout` contents so that
`My Activity/Gemini Apps/MyActivity.json` sits under the archive's gemini provider root
(`archive_read_gemini.ACTIVITY`). `My Activity/AI Mode/*.json`, `Gemini in Workspace/Conversation
History` and `Gemini/gemini_scheduled_actions_data.html` are read when present. What a given export
contains for these chats is known only after the reader has read it.

## Rebuilding the library

Staging does not rebuild anything. The new-generation library build takes hours and is started by the
owner or a scheduler decision, with the staged directories declared as additional provider roots.
