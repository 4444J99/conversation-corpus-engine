#!/usr/bin/env bash
set -euo pipefail

# Local-session refresh — ongoing incremental sync for all providers
# Runs via LaunchAgent or manually. Reads cookies from desktop apps
# and Chrome fallback, fetches new/updated conversations, caches payloads,
# and imports through the CCE pipeline.

export CCE_PROJECT_ROOT="${CCE_PROJECT_ROOT:-/Users/4jp/Workspace/organvm-i-theoria/conversation-corpus-site}"

LOGDIR="${CCE_PROJECT_ROOT}/reports"
mkdir -p "$LOGDIR"

exec >> "${LOGDIR}/local-session-refresh.log" 2>&1

echo "=== Local-session refresh: $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

FAILURES=0
PROVIDER_TIMEOUT=1200  # 20 minutes per provider

# ChatGPT local-session
echo "--- ChatGPT local-session ---"
if timeout "$PROVIDER_TIMEOUT" nice -n 19 taskpolicy -b python3 -m conversation_corpus_engine.cli provider refresh \
  --provider chatgpt \
  --mode local-session \
  --project-root "$CCE_PROJECT_ROOT" \
  --approve --promote --throttle 0.001; then
  echo "ChatGPT refresh: OK"
else
  rc=$?
  if [[ $rc -eq 124 ]]; then
    echo "ChatGPT refresh: TIMED OUT after ${PROVIDER_TIMEOUT}s"
  else
    echo "ChatGPT refresh: FAILED (exit $rc)"
  fi
  ((FAILURES++)) || true
fi

# Claude local-session
echo "--- Claude local-session ---"
if timeout "$PROVIDER_TIMEOUT" nice -n 19 taskpolicy -b python3 -m conversation_corpus_engine.cli provider refresh \
  --provider claude \
  --mode local-session \
  --project-root "$CCE_PROJECT_ROOT" \
  --approve --promote --throttle 0.001; then
  echo "Claude refresh: OK"
else
  rc=$?
  if [[ $rc -eq 124 ]]; then
    echo "Claude refresh: TIMED OUT after ${PROVIDER_TIMEOUT}s"
  else
    echo "Claude refresh: FAILED (exit $rc)"
  fi
  ((FAILURES++)) || true
fi

echo "=== Done: $(date -u +%Y-%m-%dT%H:%M:%SZ) (failures: ${FAILURES}) ==="

[[ $FAILURES -eq 0 ]] && exit 0 || exit 1
