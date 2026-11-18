# Playbook: ChatGPT Session Scope Recovery

**Archetype:** THE ACQUISITOR
**Trigger:** `conversation_count` drops below 50% of prior known baseline

## Background

ChatGPT cookie-based sessions can authenticate successfully while having degraded
scope. A "ready" session returning 2/633 visible conversations is not a rate limit
— it's a server-side scope reduction. The binary cookie jar holds a valid
`accessToken` that passes auth but has been scoped down.

## Diagnosis

```bash
# Check current scope
python3 -c "
from conversation_corpus_engine.chatgpt_local_session import discover_chatgpt_local_session
import json
print(json.dumps(discover_chatgpt_local_session(), indent=2))
"
```

Check `conversation_count`. Historical baselines:
- S38 (2026-03-26): 633 conversations
- S39 (2026-03-30): 4 conversations
- S40 (2026-03-30): 2 conversations

If the count is <100, the session is degraded.

## Recovery Steps

1. **Quit the ChatGPT desktop app** completely (Cmd+Q, not just close window)
2. **Clear the binary cookie jar** (optional, only if step 3 doesn't work):
   ```bash
   rm ~/Library/HTTPStorages/com.openai.chat.binarycookies
   ```
3. **Re-launch the ChatGPT desktop app** and sign in with full credentials
4. **Wait 30 seconds** for the session to stabilize
5. **Re-run the diagnosis** command above
6. If count is still <100: try **Chrome fallback** — open chatgpt.com in Chrome,
   sign in, then re-run with Chrome cookies

## Fallback: Official Data Export

If session recovery fails:
1. Go to chatgpt.com → Settings → Data controls → Export data
2. Wait for the email with the download link (can take 1-24 hours)
3. Download and place the ZIP in `source-drop/chatgpt/inbox/`
4. Run: `cce provider refresh --provider chatgpt --mode upload --approve --promote`

## Prevention

The `scope_preflight_check()` function (added S40) automatically detects scope
degradation before import operations. It runs during
`cce provider import --provider chatgpt --mode local-session` (and therefore
`cce provider refresh` paths that import a ChatGPT local-session candidate)
before conversation detail fetches, raising `ChatGPTLocalSessionError` when the
visible count falls below 50% of the prior acquisition baseline. This prevents
silent partial imports and delta-state corruption.

The LaunchAgent `com.4jp.cce-refresh` can run `scripts/refresh_local_sessions.sh`
on a schedule; when scope is degraded, the import/refresh should fail fast rather
than importing partial data. The repo-owned script enforces timeout/throttle/nice
defaults for background runs; LaunchAgent interval and host install details are
operator-owned — see `docs/custody/cpu-throttling-layer4.md` for the Layer 4
template and verification notes.

## Repo-side status (GH#15 / IRF-CCE-026)

Implemented in-repo: `scope_preflight_check`, acquisition-state baseline
comparison, fail-fast guard in the ChatGPT local-session import flow, recovery
playbook, and tests for boundary behavior.

Remaining (external/operator): restore a full-scope authorized session or obtain
an official export, then run a successful full import. See issue #15 comments and
the handoff for the current operational checklist.

## Related

- Feedback memory: "ChatGPT API sessions degrade silently"
- GH#15: IRF-CCE-026
- GH#16: IRF-CCE-027 (wrong Projects API endpoint, blocked by #15)
