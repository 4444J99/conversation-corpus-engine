# CPU throttling — Layer 4 orchestration (issue #25)

**Issue:** [#25 CPU throttling fix — Layer 1-3 deployed, Layer 4 pending](https://github.com/4444J99/conversation-corpus-engine/issues/25) (IRF-SYS-081)

## Status

| Layer | Scope | Repo state |
| --- | --- | --- |
| 1 | `taskpolicy -b` E-core/QoS confinement in refresh script | Present in `scripts/refresh_local_sessions.sh` |
| 2 | `--throttle` threaded through CLI/import/refresh code | Present in source and scripts (`--throttle 0.001` for background runs) |
| 3 | Trigram Jaccard pre-filter for near-duplicate detection | Present in source (see git history / implementation) |
| 4 | Orchestration tuning: LaunchAgent interval 6h→12h, nice priority 10→19, timeout 2700s→1200s | **Partially repo-owned:** timeout 1200s and `nice -n 19` are in `scripts/refresh_local_sessions.sh`; LaunchAgent interval/priority policy is operator-owned outside this repo unless a plist is tracked here |

Layer 4 was deferred in the issue pending verification that Layers 1–3 are sufficient. If production metrics show sufficiency, document a measured no-op disposition for the LaunchAgent change; if not, apply the operator-owned settings below.

## Repo-owned script settings (current)

- `scripts/refresh_local_sessions.sh`: `PROVIDER_TIMEOUT=1200` (20 minutes per provider), `nice -n 19 taskpolicy -b ... --throttle 0.001`
- `scripts/refresh_chatgpt_local_session.sh`: minimal ChatGPT-only refresh; add matching `nice`/`taskpolicy`/`--throttle`/timeout only if used as the scheduled entrypoint (currently it does not include those controls)

## Operator-owned LaunchAgent settings (template)

The scheduled refresh LaunchAgent is not tracked in this repository. When applying Layer 4 on a host, use these values (adjust label/program paths to the installed environment):

```xml
<!-- Example: com.4jp.cce-refresh.plist (user LaunchAgent) -->
<key>Label</key><string>com.4jp.cce-refresh</string>
<key>ProgramArguments</key>
<array>
  <string>/bin/bash</string>
  <string>/path/to/conversation-corpus-engine/scripts/refresh_local_sessions.sh</string>
</array>
<key>StartInterval</key><integer>43200</integer><!-- 12 hours -->
<key>Nice</key><integer>19</integer>
<key>ProcessType</key><string>Background</string>
<!-- Optional but recommended: bound runaway jobs at the launchd layer too -->
<!-- <key>ExitTimeOut</key><integer>3600</integer> -->
```

Notes:
- `Nice` in the plist complements (does not replace) `nice -n 19` in the script; keep both for defense in depth.
- `StartInterval` 43200 = 12 hours (was 21600 / 6 hours in earlier guidance; verify against observed need before changing a live agent).
- After editing: `plutil -lint /path/to/plist`, `launchctl bootout gui/$UID /path/to/plist` (or unload), then `launchctl bootstrap gui/$UID /path/to/plist` (or load). Run two scheduled cycles and capture receipts before closing the issue.

## Verification still required (operator/measurement)

Issue comments request measured evidence before unconditional Layer 4 deployment: refresh CPU/wall time, timeout/error behavior, corpus completeness/identity parity, and bounded synthetic-fixture benchmarks where appropriate. Installing/changing a user LaunchAgent and production scheduling are operator-owned actions and are not performed by this change.
