# Agent Handoff: GH#16 — Wrong ChatGPT Projects API Endpoint

**From:** Session S41 | **Date:** 2026-03-31 | **Phase:** HANDOFF
**Archetype:** THE ACQUISITOR | **IRF:** IRF-CCE-027
**Blocked by:** GH#15 (ChatGPT API scope degradation)

> **Code status (2026-06-18, LIMEN-087):** `discover_chatgpt_projects()` and
> `fetch_chatgpt_project()` no longer call the GPT Store gizmos discovery
> endpoint. They target `backend-api/projects` / `backend-api/projects/{id}`
> via `PROJECTS_LIST_PATH` / `PROJECT_DETAIL_PATH`, and parsing is tolerant of
> flat and legacy gizmo-wrapped entries (mocked unit tests in
> `tests/test_chatgpt_local_session.py`).
>
> **Still open — live verification and possible endpoint correction:** the exact
> Projects list/detail/chat/file endpoints and response shapes could not be
> observed from this environment (no valid session — see GH#15). GH#16 issue
> comments report browser-reverse-engineered findings (e.g.
> `/backend-api/gizmos/snorlax/sidebar` for listing, project-scoped
> conversation/file routes, and a silent no-op trap for
> `/backend-api/conversations?gizmo_id=...`) that must be confirmed on an
> authorized session before treating the API contract as correct. `fetch_chatgpt_project()`
> may also under-cover chats if it only derives conversations from saved-file
> metadata; validate and fix with observed evidence. Keep tokens/private project
> data out of GitHub; attach only sanitized receipts. The registry/routing/status
> machinery is endpoint-agnostic.

## Current State

`discover_chatgpt_projects()` uses the **GPT Store discovery endpoint**, not the
ChatGPT Projects API. ChatGPT Projects (launched 2024) are a distinct feature from
GPTs/Gizmos — they have their own UI, storage model, and likely their own API path.

### Current (Wrong) Implementation

```python
# chatgpt_local_session.py:877-917
def discover_chatgpt_projects(cookie_jar=DEFAULT_CHATGPT_COOKIE_JAR):
    session = build_chatgpt_session(cookie_jar)
    projects = {}
    offset = 0
    limit = 100
    while True:
        url = (
            f"https://{CHATGPT_HOST}/backend-api/gizmos/discovery/mine?"
            f"{urlencode({'offset': offset, 'limit': limit})}"
        )
        # ... pagination loop
```

Line 884 comment: `"ChatGPT 'gizmos' API returns projects as gizmo entries with
resource_type 'project'"` — this assumption is likely wrong. The gizmos/discovery
endpoint returns **custom GPTs**, not Projects.

### Related Code

| File | Lines | Function | Issue |
|------|-------|----------|-------|
| `chatgpt_local_session.py` | 877-917 | `discover_chatgpt_projects()` | Wrong endpoint |
| `chatgpt_local_session.py` | 651-767 | `fetch_chatgpt_project()` | Uses `backend-api/gizmos/{project_id}` for detail — also may be wrong |
| `chatgpt_local_session.py` | 847-874 | `merge_project_discovery()` | Registry merge logic — correct regardless of endpoint |
| `chatgpt_local_session.py` | 799-827 | `load/save_project_registry()` | State persistence — correct regardless of endpoint |
| `chatgpt_local_session.py` | 920-944 | `set_project_route()` | Routing logic — correct regardless of endpoint |
| `chatgpt_local_session.py` | 948-979 | `render_project_status()` | Display logic — correct regardless of endpoint |
| `chatgpt_local_session.py` | 981+ | `sync_chatgpt_projects()` | Extraction orchestrator — may need detail endpoint fix |
| `cli.py` | 270-273, 830-837 | `cce project discover` | CLI wiring — correct |

### The Post Office System

The project registry (`state/chatgpt-project-registry.json`) implements a "Post Office"
pattern with extraction states: `discovered → queued → extracting → extracted →
partial → failed → routed → delivered`. The registry machinery is sound — only the
API discovery endpoint is wrong.

## Completed Work

- [x] Project registry (Post Office) system (S39)
- [x] `merge_project_discovery()` — incremental registry updates (S39)
- [x] `set_project_route()` — destination assignment (S39)
- [x] `fetch_chatgpt_project()` — project detail extraction (S38)
- [x] `sync_chatgpt_projects()` — batch extraction orchestrator (S39)
- [x] CLI: `cce project discover`, `cce project extract`, `cce project sync` (S39)
- [x] Remove GPT Store gizmos discovery usage from project discovery path
- [x] Point discovery/detail to Projects-style path constants + tolerant parsing (pending live verification)
- [x] Unit tests for discovery endpoint + parsing (mocked, including pagination/error cases where added)
- [ ] Live-verify correct Projects endpoints/shapes on an authorized session (GH#15)
- [ ] Confirm/fix project chat enumeration and file download coverage in `fetch_chatgpt_project()`
- [ ] GH#16 closed with sanitized integration evidence

## Key Decisions

| Decision | Rationale |
|----------|-----------|
| Gizmos endpoint was used initially | At the time of S38, Projects were new and their API was undocumented |
| Registry machinery is endpoint-agnostic | `merge_project_discovery()` works on any dict of `{id: {name, ...}}` |
| Project detail uses `gizmos/{id}` | This was observed working for GPT entries but may not work for Projects |

## Critical Context

- **Blocked by GH#15.** Cannot test alternative endpoints without a valid ChatGPT
  session. Resolve scope degradation first.
- **ChatGPT Projects vs. GPTs:**
  - GPTs (formerly "Custom GPTs"): AI assistants with custom instructions, created
    via the GPT Builder. API path: `backend-api/gizmos/`
  - Projects: Collaborative workspaces with files, conversations, and instructions.
    API path: **unknown** — needs network inspection.
- **Investigation method:** Open ChatGPT in a browser, navigate to the Projects
  section, and observe network requests in DevTools. Look for:
  - Project listing endpoint (probably `backend-api/projects` or similar)
  - Individual project detail endpoint
  - File listing within a project
- **The `fetch_chatgpt_project()` function at line 666** uses
  `backend-api/gizmos/{project_id}` for project detail AND file listing. This
  endpoint returns `gizmo`, `files`, and other metadata. For actual Projects,
  the response shape may differ entirely.
- **Response format assumptions** in `fetch_chatgpt_project()`:
  - Expects `project.get("gizmo")` for metadata
  - Expects `project.get("files")` with `metadata.project_save.conversation_id`
  - Expects file content in conversation mapping nodes
  - All of these may need updating for the Projects API

## Next Actions

1. **Wait for GH#15 resolution** — need a valid session first

2. **Investigate correct endpoint** (requires browser DevTools):
   - Open `chatgpt.com` in Chrome
   - Navigate to Projects section in the sidebar
   - Open DevTools Network tab, filter for `backend-api`
   - Click on a project to load it
   - Record the exact API paths used for listing and detail

3. **Likely endpoint candidates:**
   ```
   backend-api/projects                          # project listing
   backend-api/projects/{id}                     # project detail
   backend-api/projects/{id}/files               # project files
   backend-api/projects/{id}/conversations       # project conversations
   ```

4. **Update `discover_chatgpt_projects()`:**
   - Change the URL to the correct endpoint
   - Update response parsing (the JSON shape will differ from gizmos)
   - Preserve the pagination pattern (offset/limit) if the new endpoint supports it

5. **Update `fetch_chatgpt_project()`:**
   - Change the detail URL
   - Update response parsing for the new JSON shape
   - The file extraction logic may need significant rework

6. **Test end-to-end:**
   ```bash
   cce project discover --project-root ../conversation-corpus-site
   cce project extract --project-id <id> --output-root /tmp/test-project
   ```

7. Close GH#16, update IRF-CCE-027.

## Risks & Warnings

- **Do NOT guess the endpoint.** OpenAI's backend API is undocumented and changes
  without notice. Always verify via browser network inspection.
- The `time.sleep(3)` in `fetch_chatgpt_project()` (line 699) is a rate-limit guard.
  Do not remove it — the conversation detail endpoint is rate-limited.
- Per feedback memory: **never delete fetched API data.** If project data was
  previously cached under the gizmos endpoint, preserve it. The data may be valid
  even if the discovery path was wrong.
- The `render_project_status()` and `sync_chatgpt_projects()` functions should NOT
  need changes — they operate on registry state, not API responses. Verify but
  don't refactor preemptively.
