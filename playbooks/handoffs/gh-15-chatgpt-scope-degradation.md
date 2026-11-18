# Agent Handoff: GH#15 — ChatGPT API Scope Degradation

**From:** Session S41 | **Date:** 2026-03-31 | **Phase:** HANDOFF
**Archetype:** THE ACQUISITOR | **IRF:** IRF-CCE-026

## Current State

The ChatGPT local-session adapter reports a valid session (`session_state: "ready"`)
but the API returns only a fraction of the user's conversations. Observed in S39:
633 conversations → 4 (0.6% remaining).

The `scope_preflight_check()` function correctly detects and blocks this degradation,
preventing import of partial data. **The code is working as designed.** The problem
is external: the ChatGPT API session's scope is restricted.

### Relevant Code

| File | Lines | Function |
|------|-------|----------|
| `chatgpt_local_session.py` | 459 | `SCOPE_DEGRADATION_THRESHOLD = 0.5` (50% floor) |
| `chatgpt_local_session.py` | 462-499 | `scope_preflight_check()` — compares current vs. prior count |
| `chatgpt_local_session.py` | 390-415 | `discover_chatgpt_local_session()` — discovery with count |
| `chatgpt_local_session.py` | 301-308 | `_fetch_session()` — auth via `chatgpt.com/api/auth/session` |
| `chatgpt_local_session.py` | 311-316 | `_session_has_valid_token()` — checks for `accessToken` and `RefreshAccessTokenError` |
| `chatgpt_local_session.py` | 339-365 | `build_chatgpt_session()` — tries native app, falls back to Chrome |

### API Endpoints

| Endpoint | Purpose |
|----------|---------|
| `https://chatgpt.com/api/auth/session` | Session validation, access token retrieval |
| `https://chatgpt.com/backend-api/conversations?offset=0&limit=1` | Discovery: get conversation count |
| `https://chatgpt.com/backend-api/conversations?offset=N&limit=100&is_archived=false` | Paginated listing |
| `https://chatgpt.com/backend-api/conversation/{id}` | Individual conversation detail |

### Cookie Sources

1. **Native app** (primary): `~/Library/HTTPStorages/com.openai.chat.binarycookies`
   - Apple NSHTTPCookieStorage binary format, parsed by `parse_binary_cookies()`
2. **Chrome** (fallback): `~/Library/Application Support/Google/Chrome/Default/Cookies`
   - Chromium SQLite database, decrypted via `_decrypt_chrome_cookie()` using
     Chrome Safe Storage keychain password

## Completed Work

- [x] Scope preflight check implemented (`chatgpt_local_session.scope_preflight_check`)
- [x] 50% threshold enforcement with clear error message and recovery playbook path
- [x] Chrome cookie fallback path for session establishment
- [x] Pre-flight wired into `import_chatgpt_local_session_corpus` before detail fetch/import (guards direct import and refresh paths that import)
- [x] Tests for scope degradation detection (boundary tests at 49%/50%)
- [x] `playbooks/scope-recovery.md` present and referenced from failure guidance
- [ ] Re-authenticate ChatGPT session (operator)
- [ ] Verify conversation count recovery (operator)
- [ ] Root-cause investigation of degradation trigger (external/OpenAI-side behavior)

**Repo-side note (current head):** code now fails fast on degraded scope; it cannot
restore missing server-side visibility. Remaining work to close GH#15 is primarily
operational (fresh signed-in session or official export) followed by a full,
idempotent import per the recovery playbook and issue execution plan.

## Key Decisions

| Decision | Rationale |
|----------|-----------|
| 50% threshold, not stricter | Allows for normal account activity (archiving, deletion) while catching catastrophic loss |
| Raise exception, don't warn | Partial import is worse than no import — corrupts delta-sync state |
| Error message includes recovery playbook path | Operator can self-serve without reading source code |

## Critical Context

- **"Ready" does not mean "full scope."** This is the key insight (documented in
  `feedback_api_session_fragility.md`). The access token can be valid (no
  `RefreshAccessTokenError`) while the backend-api returns a restricted view.
  Always check `conversation_count` before bulk operations.
- **The degradation is not deterministic.** It may be caused by:
  - Session token expiry at the API layer (not the auth layer)
  - OpenAI rate-limiting or anti-scraping measures
  - Desktop app losing sync with the web session
  - Account-level restrictions
- **Delta-sync depends on prior state.** The acquisition state at
  `{output_root}/source/acquisition-state.json` records all previously seen
  conversations. If a degraded import were allowed through, the delta-sync would
  treat missing conversations as "unchanged" and skip them permanently.
- **The Chrome fallback** uses a different session entirely. If the native app
  session is degraded, Chrome may still have a valid full-scope session (or vice versa).

## Next Actions

1. **Re-authenticate** (human action required):
   - Open the ChatGPT macOS desktop app
   - Sign out completely, then sign back in
   - Wait for the app to fully load and sync

2. **Verify recovery:**
   ```bash
   cce provider discover
   # Look for chatgpt conversation_count — should be ~633 or more
   ```

3. **If native app still degraded, try Chrome:**
   - Sign in to `chatgpt.com` in Chrome
   - The adapter will fall back automatically if native cookies fail

4. **If both sources degraded:**
   - Check if OpenAI has changed their API structure
   - Inspect network requests in browser DevTools while loading chatgpt.com
   - The `conversations` endpoint may have changed pagination or auth requirements

5. **Once recovered:**
   ```bash
   cce provider import --provider chatgpt --mode local-session --register --build
   cce provider refresh --provider chatgpt --approve
   ```

6. Close GH#15, update IRF-CCE-026.

## Risks & Warnings

- **Do NOT lower the 50% threshold** to work around degradation. The threshold
  protects the delta-sync state from corruption. Fix the auth, not the guard.
- **Do NOT delete acquisition-state.json** to bypass the check. This would trigger
  a full re-fetch (no prior state = no threshold comparison) but would lose all
  delta-sync history, forcing re-download of every conversation.
- If the API has changed structurally (new auth headers, different response format),
  the fix is in `_request_json()` or `_auth_headers()`, not in the threshold logic.
- Per feedback memory: **never delete fetched API data.** If conversations were
  cached in `source/conversation-cache/`, preserve them even if the session is stale.
