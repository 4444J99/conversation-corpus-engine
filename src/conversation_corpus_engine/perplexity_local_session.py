#!/usr/bin/env python3
"""Perplexity local-session client.

Reads cookies from the Perplexity macOS desktop app's binary cookie jar
(~/Library/HTTPStorages/ai.perplexity.macv3.binarycookies) and authenticates
via the Perplexity NextAuth session — unlike ChatGPT there is NO bearer token,
the ``__Secure-next-auth.session-token`` cookie on ``www.perplexity.ai`` carries
auth on its own.

Mirrors the shipped ChatGPT/Claude local-session adapters: same public shape
(``build_perplexity_session``, ``discover_perplexity_local_session``, a bundle
materializer). The binary-cookie parser is reused from
``chatgpt_local_session`` (single source of truth — never duplicated here).

His own account, his own data — a legitimate local puller, the same pattern as
the ChatGPT and Claude adapters.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .chatgpt_local_session import (
    Cookie,
    build_cookie_header,
    parse_binary_cookies,
)

DEFAULT_PERPLEXITY_COOKIE_JAR = Path(
    "/Users/4jp/Library/HTTPStorages/ai.perplexity.macv3.binarycookies"
)
PERPLEXITY_HOST = "www.perplexity.ai"
PERPLEXITY_COOKIE_HOST = "www.perplexity.ai"
PERPLEXITY_SESSION_COOKIE = "__Secure-next-auth.session-token"  # allow-secret
PERPLEXITY_USER_AGENT = "Perplexity-Mac"


class PerplexityLocalSessionError(RuntimeError):
    pass


@dataclass(frozen=True)
class PerplexityHttpSession:
    cookies: list[Cookie]
    session_payload: dict[str, Any]
    account_id: str
    account_email: str
    account_username: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


def resolve_perplexity_cookie_jar(
    cookie_jar: Path = DEFAULT_PERPLEXITY_COOKIE_JAR,
) -> Path:
    path = cookie_jar.resolve()
    if not path.exists():
        raise PerplexityLocalSessionError(
            f"Perplexity cookie jar does not exist: {path}\n"
            "Sign in to the Perplexity macOS app so it writes its session cookies."
        )
    data = path.read_bytes()[:4]
    if data != b"cook":
        raise PerplexityLocalSessionError(
            f"Perplexity cookie jar has unexpected format (expected 'cook' magic): {path}"
        )
    return path


def _request_json(
    cookies: list[Cookie],
    url: str,
    *,
    timeout: int = 30,
) -> Any:
    headers: dict[str, str] = {
        "Cookie": build_cookie_header(cookies, url),
        "User-Agent": PERPLEXITY_USER_AGENT,
        "Accept": "application/json,*/*",
        "Referer": f"https://{PERPLEXITY_HOST}/",
        "Origin": f"https://{PERPLEXITY_HOST}",
    }
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        raise PerplexityLocalSessionError(
            f"{error.code} while fetching {url}: {body[:500]}"
        ) from error
    except URLError as error:
        raise PerplexityLocalSessionError(f"Network error while fetching {url}: {error}") from error


def _fetch_session(cookies: list[Cookie]) -> dict[str, Any]:
    url = f"https://{PERPLEXITY_HOST}/api/auth/session"
    payload = _request_json(cookies, url)
    if not isinstance(payload, dict):
        raise PerplexityLocalSessionError("Perplexity session payload was not a JSON object.")
    return payload


def _session_is_valid(payload: dict[str, Any]) -> bool:
    user = payload.get("user") or {}
    return bool(user.get("id"))


def _build_session_from_cookies(cookies: list[Cookie]) -> PerplexityHttpSession | None:
    try:
        session_payload = _fetch_session(cookies)
    except PerplexityLocalSessionError:
        return None
    if not _session_is_valid(session_payload):
        return None
    user = session_payload.get("user") or {}
    return PerplexityHttpSession(
        cookies=cookies,
        session_payload=session_payload,
        account_id=user.get("id") or "",
        account_email=user.get("email") or "",
        account_username=user.get("username") or "",
    )


def build_perplexity_session(
    cookie_jar: Path = DEFAULT_PERPLEXITY_COOKIE_JAR,
) -> PerplexityHttpSession:
    try:
        path = resolve_perplexity_cookie_jar(cookie_jar)
        cookies = parse_binary_cookies(path)
        session = _build_session_from_cookies(cookies)
        if session is not None:
            return session
    except PerplexityLocalSessionError:
        pass

    raise PerplexityLocalSessionError(
        "Unable to establish a Perplexity session from the native app cookie jar. "
        "Sign in to the Perplexity macOS app and try again."
    )


def fetch_json(session: PerplexityHttpSession, url: str) -> Any:
    return _request_json(session.cookies, url)


# ---------------------------------------------------------------------------
# Step-block extraction (INITIAL_QUERY -> user query, FINAL -> assistant answer)
# ---------------------------------------------------------------------------


def _parse_entry_steps(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse one thread entry's ``text`` field into its list of step blocks.

    Each ``entries[i].text`` is a JSON *string* that parses to a list of step
    blocks with a ``step_type`` in {INITIAL_QUERY, SEARCH_WEB, SEARCH_RESULTS,
    FINAL}. Returns ``[]`` on any malformed entry.
    """
    raw = entry.get("text")
    if not isinstance(raw, str):
        return []
    try:
        steps = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(steps, list):
        return []
    return [step for step in steps if isinstance(step, dict)]


def extract_user_query(steps: list[dict[str, Any]]) -> str:
    """User query = the INITIAL_QUERY step block -> ``content.query``."""
    for step in steps:
        if step.get("step_type") == "INITIAL_QUERY":
            content = step.get("content") or {}
            query = content.get("query")
            if isinstance(query, str):
                return query
    return ""


def extract_assistant_answer(steps: list[dict[str, Any]]) -> str:
    """Assistant answer = the FINAL step block.

    ``content.answer`` is itself a JSON string parsing to ``{"answer": "<md>"}``;
    the inner ``.answer`` markdown is returned. Falls back to the raw string if
    it does not parse as the nested envelope.
    """
    for step in steps:
        if step.get("step_type") == "FINAL":
            content = step.get("content") or {}
            answer = content.get("answer")
            if not isinstance(answer, str):
                return ""
            try:
                inner = json.loads(answer)
            except (json.JSONDecodeError, TypeError):
                return answer
            if isinstance(inner, dict) and isinstance(inner.get("answer"), str):
                return inner["answer"]
            return answer
    return ""


def _normalize_title(title: str) -> str:
    return " ".join((title or "").split()).strip()


def normalize_thread(thread_summary: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Perplexity thread (summary + detail) to a corpus record.

    Shape:
        {thread_uid, conversation_id, title_raw, title_normalized,
         messages:[{role, text}], url, source}
    """
    uuid = thread_summary.get("uuid") or detail.get("uuid") or ""
    title_raw = thread_summary.get("title") or detail.get("title") or ""
    link = thread_summary.get("link") or ""
    if link.startswith("/"):
        url = f"https://{PERPLEXITY_HOST}{link}"
    elif link:
        url = link
    else:
        url = f"https://{PERPLEXITY_HOST}/search/{uuid}"

    messages: list[dict[str, str]] = []
    for entry in detail.get("entries") or []:
        steps = _parse_entry_steps(entry)
        if not steps:
            continue
        query = extract_user_query(steps)
        if query:
            messages.append({"role": "user", "text": query})
        answer = extract_assistant_answer(steps)
        if answer:
            messages.append({"role": "assistant", "text": answer})

    return {
        "thread_uid": uuid,
        "conversation_id": uuid,
        "title_raw": title_raw,
        "title_normalized": _normalize_title(title_raw),
        "messages": messages,
        "url": url,
        "source": "perplexity-local-session",
    }


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _list_recent_threads(
    session: PerplexityHttpSession,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Page through ``list_recent`` until a short page is returned."""
    all_threads: list[dict[str, Any]] = []
    offset = 0
    while True:
        query = urlencode({"limit": limit, "offset": offset})
        url = f"https://{PERPLEXITY_HOST}/rest/thread/list_recent?{query}"
        page = fetch_json(session, url)
        if not isinstance(page, list):
            break
        all_threads.extend(page)
        if len(page) < limit:
            break
        offset += limit
    return all_threads


def _fetch_thread_detail(session: PerplexityHttpSession, uuid: str) -> dict[str, Any]:
    url = f"https://{PERPLEXITY_HOST}/rest/thread/{uuid}"
    detail = fetch_json(session, url)
    if not isinstance(detail, dict):
        raise PerplexityLocalSessionError(f"Thread detail for {uuid} was not a JSON object.")
    return detail


def discover_perplexity_local_session(
    cookie_jar: Path = DEFAULT_PERPLEXITY_COOKIE_JAR,
) -> dict[str, Any]:
    session = build_perplexity_session(cookie_jar)
    user = session.session_payload.get("user") or {}
    threads = _list_recent_threads(session)

    return {
        "generated_at": now_iso(),
        "cookie_jar": str(cookie_jar.resolve()),
        "adapter_type": "perplexity-local-session",
        "collection_scope": "local-session",
        "session_state": "ready",
        "account_id": session.account_id,
        "account_email": session.account_email,
        "account_username": session.account_username,
        "subscription_tier": user.get("subscription_tier") or "",
        "conversation_count": len(threads),
        "recommended_command": (
            "cce provider import --provider perplexity --mode local-session --register --build"
        ),
        "calibration_only": True,
    }


# ---------------------------------------------------------------------------
# Bundle fetching + materialization
# ---------------------------------------------------------------------------


def fetch_perplexity_local_session_bundle(
    cookie_jar: Path = DEFAULT_PERPLEXITY_COOKIE_JAR,
    *,
    limit: int = 50,
) -> dict[str, Any]:
    """Fetch every thread + detail and normalize each to a corpus record."""
    session = build_perplexity_session(cookie_jar)
    summaries = _list_recent_threads(session, limit=limit)

    normalized_threads: list[dict[str, Any]] = []
    detail_failures: list[dict[str, Any]] = []
    fetched_count = 0

    for summary in summaries:
        uuid = summary.get("uuid")
        if not uuid:
            continue
        try:
            detail = _fetch_thread_detail(session, uuid)
            normalized_threads.append(normalize_thread(summary, detail))
            fetched_count += 1
        except Exception as exc:  # noqa: BLE001 - record and continue
            detail_failures.append({"uuid": uuid, "error": str(exc)})

    acquisition_report = {
        "generated_at": now_iso(),
        "total_listed": len(summaries),
        "fetched_count": fetched_count,
        "failure_count": len(detail_failures),
    }

    return {
        "generated_at": now_iso(),
        "cookie_jar": str(cookie_jar.resolve()),
        "adapter_type": "perplexity-local-session",
        "collection_scope": "local-session",
        "account_id": session.account_id,
        "account_email": session.account_email,
        "account_username": session.account_username,
        "thread_summaries": summaries,
        "threads": normalized_threads,
        "thread_detail_failures": detail_failures,
        "total_count": len(summaries),
        "fetched_count": fetched_count,
        "acquisition_report": acquisition_report,
    }


def materialize_perplexity_local_session(
    output_root: Path,
    *,
    cookie_jar: Path = DEFAULT_PERPLEXITY_COOKIE_JAR,
    limit: int = 50,
) -> dict[str, Any]:
    """Fetch all threads and write normalized per-thread records under ``output_root``.

    Writes ``threads/<uuid>.json`` per thread plus a ``threads-index.json``
    manifest. Returns a summary dict (never the conversation bodies).
    """
    output_root = output_root.resolve()
    bundle = fetch_perplexity_local_session_bundle(cookie_jar, limit=limit)

    threads_dir = output_root / "threads"
    threads_dir.mkdir(parents=True, exist_ok=True)

    index_entries: list[dict[str, Any]] = []
    written = 0
    for thread in bundle["threads"]:
        uuid = thread.get("thread_uid")
        if not uuid:
            continue
        (threads_dir / f"{uuid}.json").write_text(
            json.dumps(thread, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        written += 1
        index_entries.append(
            {
                "thread_uid": uuid,
                "title_normalized": thread.get("title_normalized", ""),
                "message_count": len(thread.get("messages") or []),
                "url": thread.get("url", ""),
            }
        )

    index = {
        "generated_at": now_iso(),
        "source": "perplexity-local-session",
        "account_email": bundle["account_email"],
        "account_username": bundle["account_username"],
        "thread_count": written,
        "fetched_count": bundle["fetched_count"],
        "detail_failures": bundle["thread_detail_failures"],
        "threads": index_entries,
    }
    (output_root / "threads-index.json").write_text(
        json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    return {
        "generated_at": now_iso(),
        "output_root": str(output_root),
        "threads_written": written,
        "fetched_count": bundle["fetched_count"],
        "failure_count": len(bundle["thread_detail_failures"]),
    }


def render_discovery_text(payload: dict[str, Any]) -> str:
    lines = [
        f"Perplexity cookie jar: {payload.get('cookie_jar', 'unknown')}",
        f"Generated: {payload['generated_at']}",
        f"Session state: {payload['session_state']}",
        f"Account: {payload.get('account_email') or payload.get('account_id') or 'unknown'}",
        f"Threads: {payload['conversation_count']}",
        f"Calibration only: {payload['calibration_only']}",
        f"Recommended command: {payload['recommended_command']}",
    ]
    return "\n".join(lines)
