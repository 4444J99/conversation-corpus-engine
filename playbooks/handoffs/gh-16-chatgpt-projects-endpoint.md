# Agent Handoff: GH#16 — Wrong ChatGPT Projects API Endpoint

**From:** Session S41 | **Date:** 2026-03-31 | **Phase:** HANDOFF
**Archetype:** THE ACQUISITOR | **IRF:** IRF-CCE-027
**Blocked by:** GH#15 (ChatGPT API scope degradation)

> **Resolution (2026-06-18, LIMEN-087):** `discover_chatgpt_projects()` and
> `fetch_chatgpt_project()` now target the ChatGPT **Projects** API
> (`backend-api/projects` / `backend-api/projects/{id}`) via the
> `PROJECTS_LIST_PATH` / `PROJECT_DETAIL_PATH` constants, not the gizmos
> discovery API. Listing items are parsed by `_parse_project_item()` /
> `_project_display_name()`, which read the flat Projects shape
> (`id`/`name`/`files`) first and fall back to legacy gizmo-wrapped entries for
> safety. Parsing + endpoint selection are covered by mocked unit tests in
> `tests/test_chatgpt_local_session.py`.
>
> **Still open — live verification:** the exact JSON shapes could not be observed
> from this environment (no valid session — see GH#15). When a session is
> available, confirm the Projects list/detail paths and response shapes via
> browser DevTools and adjust the two path constants + parsing helpers if they
> differ. The registry/routing/status machinery needs no changes either way.

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
- [x] Identify correct Projects API endpoint (`backend-api/projects[/{id}]`; live-verify shapes)
- [x] Update `discover_chatgpt_projects()` with correct endpoint + Projects-shape parsing
- [x] Update `fetch_chatgpt_project()` to the Projects detail endpoint
- [x] Unit tests for discovery endpoint + parsing (mocked); live end-to-end still pending GH#15
- [ ] GH#16 closed

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
