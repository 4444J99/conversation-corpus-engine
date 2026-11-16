#!/usr/bin/env python3
"""ChatGPT local-session client.

Reads cookies from either:
1. The ChatGPT macOS desktop app's binary cookie jar
   (~/Library/HTTPStorages/com.openai.chat.binarycookies)
2. Chrome's Chromium cookie store (fallback when the native app session is stale)
   (~/Library/Application Support/Google/Chrome/Default/Cookies)

Authenticates via the chatgpt.com session API and fetches conversations
through the backend API.

Ported from the genesis script: conversation-corpus-site/archive/legacy-scripts/
export_chatgpt_history.py — the origin of the entire conversation corpus engine.
"""

from __future__ import annotations

import json
import re
import sqlite3
import struct
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import pbkdf2_hmac, sha256
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from .import_chatgpt_export_corpus import extract_node_text, walk_mapping_tree

DEFAULT_CHATGPT_COOKIE_JAR = Path("/Users/4jp/Library/HTTPStorages/com.openai.chat.binarycookies")
DEFAULT_CHROME_COOKIES = Path.home() / "Library/Application Support/Google/Chrome/Default/Cookies"
CHROME_SAFE_STORAGE_SERVICES = ("Chrome Safe Storage", "Chromium Safe Storage")
CHATGPT_COOKIE_HOST = ".chatgpt.com"
CHATGPT_HOST = "chatgpt.com"
CHATGPT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class ChatGPTLocalSessionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Cookie:
    domain: str
    name: str
    path: str
    value: str
    secure: bool
    http_only: bool


@dataclass(frozen=True)
class ChatGPTHttpSession:
    cookies: list[Cookie]
    session_payload: dict[str, Any]
    access_token: str  # allow-secret
    account_id: str
    device_id: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Binary cookie jar parsing (Apple NSHTTPCookieStorage format)
# ---------------------------------------------------------------------------


def read_c_string(chunk: bytes, start: int) -> str:
    end = chunk.find(b"\x00", start)
    if end == -1:
        end = len(chunk)
    return chunk[start:end].decode("utf-8", "replace")


def parse_binary_cookies(path: Path) -> list[Cookie]:
    data = path.read_bytes()
    if data[:4] != b"cook":
        raise ChatGPTLocalSessionError(f"Unsupported cookie jar format: {path}")

    page_count = struct.unpack(">I", data[4:8])[0]
    offset = 8
    page_sizes = [
        struct.unpack(">I", data[offset + i * 4 : offset + (i + 1) * 4])[0]
        for i in range(page_count)
    ]
    offset += 4 * page_count

    cookies: list[Cookie] = []
    for page_size in page_sizes:
        page = data[offset : offset + page_size]
        offset += page_size
        cookie_count = struct.unpack("<I", page[4:8])[0]
        cookie_offsets = [
            struct.unpack("<I", page[8 + i * 4 : 12 + i * 4])[0] for i in range(cookie_count)
        ]
        for cookie_offset in cookie_offsets:
            chunk = page[cookie_offset:]
            size = struct.unpack("<I", chunk[:4])[0]
            chunk = chunk[:size]
            flags = struct.unpack("<I", chunk[8:12])[0]
            domain_offset = struct.unpack("<I", chunk[16:20])[0]
            name_offset = struct.unpack("<I", chunk[20:24])[0]
            path_offset = struct.unpack("<I", chunk[24:28])[0]
            value_offset = struct.unpack("<I", chunk[28:32])[0]
            cookies.append(
                Cookie(
                    domain=read_c_string(chunk, domain_offset),
                    name=read_c_string(chunk, name_offset),
                    path=read_c_string(chunk, path_offset) or "/",
                    value=read_c_string(chunk, value_offset),
                    secure=bool(flags & 1),
                    http_only=bool(flags & 4),
                )
            )
    return cookies


# ---------------------------------------------------------------------------
# Cookie utilities
# ---------------------------------------------------------------------------


def cookie_matches_host(cookie: Cookie, host: str) -> bool:
    domain = cookie.domain.lstrip(".").lower()
    host = host.lower()
    return host == domain or host.endswith(f".{domain}")


def build_cookie_header(cookies: list[Cookie], url: str) -> str:
    host = urlparse(url).hostname or ""
    selected = [
        f"{cookie.name}={cookie.value}" for cookie in cookies if cookie_matches_host(cookie, host)
    ]
    return "; ".join(selected)


def find_cookie_value(cookies: list[Cookie], host: str, name: str) -> str:
    for cookie in cookies:
        if cookie.name == name and cookie_matches_host(cookie, host):
            return cookie.value
    return ""


# ---------------------------------------------------------------------------
# Chrome Chromium cookie reading (fallback when native app session is stale)
# ---------------------------------------------------------------------------


def _find_chrome_safe_storage_password() -> str:
    for service in CHROME_SAFE_STORAGE_SERVICES:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            pw = result.stdout.strip()  # allow-secret
            if pw:
                return pw
    raise ChatGPTLocalSessionError(
        "Unable to read Chrome Safe Storage password from the macOS keychain."
    )


def _decrypt_chrome_cookie(encrypted_value: bytes, host_key: str, key: bytes) -> str:
    if not encrypted_value:
        return ""
    if not encrypted_value.startswith(b"v10"):
        return encrypted_value.decode("utf-8", errors="replace")
    iv = b" " * 16
    result = subprocess.run(
        ["openssl", "enc", "-aes-128-cbc", "-d", "-K", key.hex(), "-iv", iv.hex(), "-nopad"],
        input=encrypted_value[3:],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        return ""
    padded = result.stdout
    pad_length = padded[-1]
    if pad_length < 1 or pad_length > 16:
        return ""
    decrypted = padded[:-pad_length]
    host_hash = sha256(host_key.encode("utf-8")).digest()
    if decrypted.startswith(host_hash):
        decrypted = decrypted[len(host_hash) :]
    return decrypted.decode("utf-8", errors="replace")


def load_chatgpt_cookies_from_chrome(
    chrome_cookies: Path = DEFAULT_CHROME_COOKIES,
) -> list[Cookie]:
    if not chrome_cookies.exists():
        raise ChatGPTLocalSessionError(f"Chrome Cookies database not found: {chrome_cookies}")
    safe_pw = _find_chrome_safe_storage_password()  # allow-secret
    key = pbkdf2_hmac("sha1", safe_pw.encode("utf-8"), b"saltysalt", 1003, 16)
    connection = sqlite3.connect(chrome_cookies)
    try:
        rows = connection.execute(
            "SELECT host_key, name, value, encrypted_value FROM cookies "
            "WHERE host_key LIKE ? OR host_key LIKE ?",
            ("%chatgpt%", "%openai%"),
        ).fetchall()
    finally:
        connection.close()

    cookies: list[Cookie] = []
    session_parts: dict[str, str] = {}
    for host_key, name, value, encrypted_value in rows:
        decrypted = value if value else _decrypt_chrome_cookie(encrypted_value, host_key, key)
        if not decrypted:
            continue
        try:
            decrypted.encode("latin-1")
        except UnicodeEncodeError:
            continue
        if name.startswith("__Secure-next-auth.session-token."):
            session_parts[name] = decrypted
            continue
        if name == "__Secure-next-auth.session-token":
            cookies.append(Cookie(host_key, name, "/", decrypted, True, True))
            continue
        cookies.append(Cookie(host_key, name, "/", decrypted, False, False))

    if session_parts and not any(c.name == "__Secure-next-auth.session-token" for c in cookies):
        combined = "".join(session_parts[k] for k in sorted(session_parts))
        if combined:
            cookies.append(
                Cookie(
                    CHATGPT_COOKIE_HOST,
                    "__Secure-next-auth.session-token",
                    "/",
                    combined,
                    True,
                    True,
                )
            )
    return cookies


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


def resolve_chatgpt_cookie_jar(
    cookie_jar: Path = DEFAULT_CHATGPT_COOKIE_JAR,
) -> Path:
    path = cookie_jar.resolve()
    if not path.exists():
        raise ChatGPTLocalSessionError(
            f"ChatGPT cookie jar does not exist: {path}\n"
            "The ChatGPT macOS desktop app must be installed and signed in."
        )
    data = path.read_bytes()[:4]
    if data != b"cook":
        raise ChatGPTLocalSessionError(
            f"ChatGPT cookie jar has unexpected format (expected 'cook' magic): {path}"
        )
    return path


def _request_json(
    cookies: list[Cookie],
    url: str,
    *,
    extra_headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> Any:
    headers: dict[str, str] = {
        "Cookie": build_cookie_header(cookies, url),
        "User-Agent": CHATGPT_USER_AGENT,
        "Accept": "application/json,text/plain,*/*",
        "Referer": f"https://{CHATGPT_HOST}/",
        "Origin": f"https://{CHATGPT_HOST}",
    }
    if extra_headers:
        headers.update(extra_headers)
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        raise ChatGPTLocalSessionError(
            f"{error.code} while fetching {url}: {body[:500]}"
        ) from error
    except URLError as error:
        raise ChatGPTLocalSessionError(f"Network error while fetching {url}: {error}") from error


def _fetch_session(cookies: list[Cookie]) -> dict[str, Any]:
    url = f"https://{CHATGPT_HOST}/api/auth/session"
    payload = _request_json(cookies, url)
    if not isinstance(payload, dict):
        raise ChatGPTLocalSessionError("ChatGPT session payload was not a JSON object.")
    return payload


def _session_has_valid_token(payload: dict[str, Any]) -> bool:
    if "accessToken" not in payload:
        return False
    return payload.get("error") != "RefreshAccessTokenError"


def _build_session_from_cookies(cookies: list[Cookie]) -> ChatGPTHttpSession | None:
    try:
        session_payload = _fetch_session(cookies)
    except ChatGPTLocalSessionError:
        return None
    if not _session_has_valid_token(session_payload):
        return None
    access_token = session_payload["accessToken"]  # allow-secret
    account = session_payload.get("account") or {}
    account_id = account.get("id") or account.get("account_id") or ""
    device_id = find_cookie_value(cookies, CHATGPT_HOST, "oai-did")
    return ChatGPTHttpSession(
        cookies=cookies,
        session_payload=session_payload,
        access_token=access_token,  # allow-secret
        account_id=account_id,
        device_id=device_id,
    )


def build_chatgpt_session(
    cookie_jar: Path = DEFAULT_CHATGPT_COOKIE_JAR,
) -> ChatGPTHttpSession:
    # Try 1: native ChatGPT macOS app binary cookie jar
    try:
        path = resolve_chatgpt_cookie_jar(cookie_jar)
        native_cookies = parse_binary_cookies(path)
        session = _build_session_from_cookies(native_cookies)
        if session is not None:
            return session
    except ChatGPTLocalSessionError:
        pass

    # Try 2: Chrome Chromium cookie store (same decryption as Claude adapter)
    try:
        chrome_cookies = load_chatgpt_cookies_from_chrome()
        session = _build_session_from_cookies(chrome_cookies)
        if session is not None:
            return session
    except ChatGPTLocalSessionError:
        pass

    raise ChatGPTLocalSessionError(
        "Unable to establish a ChatGPT session from either the native app "
        "cookie jar or Chrome cookies. Sign in to chatgpt.com in either "
        "the ChatGPT desktop app or Chrome."
    )


def _auth_headers(session: ChatGPTHttpSession) -> dict[str, str]:
    headers: dict[str, str] = {
        "Authorization": f"Bearer {session.access_token}",  # allow-secret
    }
    if session.account_id:
        headers["OpenAI-Account-ID"] = session.account_id
    if session.device_id:
        headers["OAI-Device-Id"] = session.device_id
    return headers


def fetch_json(session: ChatGPTHttpSession, url: str) -> Any:
    return _request_json(session.cookies, url, extra_headers=_auth_headers(session))


# ---------------------------------------------------------------------------
# Discovery and bundle fetching
# ---------------------------------------------------------------------------


def discover_chatgpt_local_session(
    cookie_jar: Path = DEFAULT_CHATGPT_COOKIE_JAR,
) -> dict[str, Any]:
    session = build_chatgpt_session(cookie_jar)
    account = session.session_payload.get("account") or {}
    user = session.session_payload.get("user") or {}

    url = f"https://{CHATGPT_HOST}/backend-api/conversations?{urlencode({'offset': 0, 'limit': 1})}"
    conversations_page = fetch_json(session, url)
    total_count = conversations_page.get("total", 0)

    return {
        "generated_at": now_iso(),
        "cookie_jar": str(cookie_jar.resolve()),
        "adapter_type": "chatgpt-local-session",
        "collection_scope": "local-session",
        "session_state": "ready",
        "account_id": account.get("id") or account.get("account_id") or "",
        "account_email": user.get("email") or account.get("email") or "",
        "account_name": user.get("name") or "",
        "conversation_count": total_count,
        "recommended_command": (
            "cce provider import --provider chatgpt --mode local-session --register --build"
        ),
        "calibration_only": True,
    }


# ---------------------------------------------------------------------------
# Acquisition state persistence + payload caching
# ---------------------------------------------------------------------------


def _acquisition_state_path(output_root: Path) -> Path:
    return output_root / "source" / "acquisition-state.json"


def _conversation_cache_dir(output_root: Path) -> Path:
    return output_root / "source" / "conversation-cache"


def load_prior_acquisition(output_root: Path) -> dict[str, dict[str, Any]]:
    state_path = _acquisition_state_path(output_root)
    if not state_path.exists():
        return {}
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        return payload.get("conversations", {})
    except (json.JSONDecodeError, OSError):
        return {}


def save_acquisition_state(
    output_root: Path,
    conversations: dict[str, dict[str, Any]],
    *,
    report: dict[str, Any],
) -> None:
    state_path = _acquisition_state_path(output_root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": now_iso(),
        "conversation_count": len(conversations),
        "conversations": conversations,
        "last_acquisition_report": report,
    }
    state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


SCOPE_DEGRADATION_THRESHOLD = 0.5  # flag if current count < 50% of prior


def scope_preflight_check(
    conversation_count: int,
    output_root: Path,
) -> dict[str, Any]:
    """Compare current conversation_count against prior acquisition state.

    Returns a status dict: ok, degraded, or unknown (no prior state).
    Raises ChatGPTLocalSessionError if degraded — prevents importing partial data.
    """
    prior = load_prior_acquisition(output_root)
    prior_count = len(prior)
    if prior_count == 0:
        return {
            "status": "unknown",
            "current": conversation_count,
            "prior": 0,
            "delta_pct": 0.0,
            "message": "No prior acquisition state — cannot assess scope.",
        }
    if prior_count > 0 and conversation_count < prior_count * SCOPE_DEGRADATION_THRESHOLD:
        delta_pct = round(((conversation_count - prior_count) / prior_count) * 100, 1)
        msg = (
            f"Session scope degraded: {conversation_count} conversations visible "
            f"(prior: {prior_count}, {delta_pct}%). "
            f"Re-launch the ChatGPT desktop app and sign in fresh. "
            f"See playbooks/scope-recovery.md."
        )
        raise ChatGPTLocalSessionError(msg)
    delta_pct = round(((conversation_count - prior_count) / prior_count) * 100, 1)
    return {
        "status": "ok",
        "current": conversation_count,
        "prior": prior_count,
        "delta_pct": delta_pct,
        "message": f"Scope OK: {conversation_count} conversations (prior: {prior_count}).",
    }


def cache_conversation_payload(
    output_root: Path, conversation_id: str, payload: dict[str, Any]
) -> Path:
    cache_dir = _conversation_cache_dir(output_root)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{conversation_id}.json"
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return path


def load_cached_conversation(output_root: Path, conversation_id: str) -> dict[str, Any] | None:
    path = _conversation_cache_dir(output_root) / f"{conversation_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _normalize_conversation_detail(detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "conversation_id": detail.get("conversation_id") or detail.get("id"),
        "title": detail.get("title") or "",
        "create_time": detail.get("create_time"),
        "update_time": detail.get("update_time"),
        "mapping": detail.get("mapping") or {},
        "current_node": detail.get("current_node"),
        "is_archived": detail.get("is_archived", False),
        "gizmo_id": detail.get("gizmo_id"),
    }


# ---------------------------------------------------------------------------
# Bundle fetching (delta-aware)
# ---------------------------------------------------------------------------


def fetch_chatgpt_local_session_bundle(
    cookie_jar: Path = DEFAULT_CHATGPT_COOKIE_JAR,
    *,
    limit: int = 100,
    offset: int = 0,
    prior_state: dict[str, dict[str, Any]] | None = None,
    output_root: Path | None = None,
) -> dict[str, Any]:
    session = build_chatgpt_session(cookie_jar)
    account = session.session_payload.get("account") or {}
    user = session.session_payload.get("user") or {}

    # Paginate all conversations
    all_items: list[dict[str, Any]] = []
    current_offset = offset
    while True:
        url = (
            f"https://{CHATGPT_HOST}/backend-api/conversations?"
            f"{urlencode({'offset': current_offset, 'limit': limit, 'is_archived': 'false'})}"
        )
        conversations_page = fetch_json(session, url)
        items = conversations_page.get("items") or []
        all_items.extend(items)
        if len(items) < limit:
            break
        current_offset += limit

    total_count = conversations_page.get("total", len(all_items)) if all_items else 0
    prior = prior_state or {}
    full_refresh = not prior

    conversations_json: list[dict[str, Any]] = []
    detail_failures: list[dict[str, Any]] = []
    fetched_count = 0
    reused_count = 0
    skipped_count = 0

    for item in all_items:
        conversation_id = item.get("id")
        if not conversation_id:
            skipped_count += 1
            continue

        prior_entry = prior.get(conversation_id, {})
        prior_update_time = prior_entry.get("update_time")
        current_update_time = item.get("update_time")

        if (
            not full_refresh
            and prior_update_time is not None
            and current_update_time is not None
            and prior_update_time == current_update_time
            and output_root is not None
        ):
            cached = load_cached_conversation(output_root, conversation_id)
            if cached is not None:
                conversations_json.append(cached)
                reused_count += 1
                continue

        try:
            detail_url = f"https://{CHATGPT_HOST}/backend-api/conversation/{conversation_id}"
            detail = fetch_json(session, detail_url)
            normalized = _normalize_conversation_detail(detail)
            conversations_json.append(normalized)
            fetched_count += 1
            if output_root is not None:
                cache_conversation_payload(output_root, conversation_id, normalized)
        except Exception as exc:
            detail_failures.append({"id": conversation_id, "error": str(exc)})

    user_payload = {
        "id": account.get("id") or account.get("account_id") or "",
        "email": user.get("email") or account.get("email") or "",
        "chatgpt_plus_user": user.get("chatgpt_plus_user", False),
    }

    acquisition_report = {
        "generated_at": now_iso(),
        "total_listed": len(all_items),
        "fetched_count": fetched_count,
        "reused_count": reused_count,
        "skipped_count": skipped_count,
        "failure_count": len(detail_failures),
        "full_refresh": full_refresh,
    }

    return {
        "generated_at": now_iso(),
        "cookie_jar": str(cookie_jar.resolve()),
        "adapter_type": "chatgpt-local-session",
        "collection_scope": "local-session",
        "account": account,
        "user": user_payload,
        "conversation_summaries": all_items,
        "conversations": conversations_json,
        "conversation_detail_failures": detail_failures,
        "total_count": total_count,
        "fetched_count": fetched_count,
        "reused_count": reused_count,
        "acquisition_report": acquisition_report,
    }


# ---------------------------------------------------------------------------
# Project extraction
# ---------------------------------------------------------------------------


def fetch_chatgpt_project(
    project_id: str,
    output_root: Path,
    *,
    cookie_jar: Path = DEFAULT_CHATGPT_COOKIE_JAR,
) -> dict[str, Any]:
    """Extract a ChatGPT Project's files and conversations to a local directory."""
    session = build_chatgpt_session(cookie_jar)
    output_root = output_root.resolve()

    detail_path = PROJECT_DETAIL_PATH.format(project_id=project_id)
    project = fetch_json(session, f"https://{CHATGPT_HOST}/{detail_path}")
    files = project.get("files") or []
    project_name = _project_display_name(project) or project_id

    meta_dir = output_root / "metadata"
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "project.json").write_text(json.dumps(project, indent=2) + "\n", encoding="utf-8")

    files_dir = output_root / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    convos_dir = output_root / "conversations"
    convos_dir.mkdir(parents=True, exist_ok=True)

    conv_groups: dict[str, list[dict[str, Any]]] = {}
    for f in files:
        meta = (f.get("metadata") or {}).get("project_save") or {}
        cid = meta.get("conversation_id", "")
        if cid:
            conv_groups.setdefault(cid, []).append(f)

    def _slugify(text: str) -> str:
        s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
        return s[:60] if s else "untitled"

    extracted_files = 0
    seen_names: dict[str, int] = {}
    conv_mappings: dict[str, dict[str, Any]] = {}

    for cid, file_group in conv_groups.items():
        try:
            time.sleep(3)
            detail = fetch_json(session, f"https://{CHATGPT_HOST}/backend-api/conversation/{cid}")
            mapping = detail.get("mapping") or {}
            conv_mappings[cid] = detail
        except Exception:
            continue

        for f in file_group:
            meta = (f.get("metadata") or {}).get("project_save") or {}
            msg_id = meta.get("message_id", "")
            name = f.get("name", "unnamed.txt")

            if name in seen_names:
                seen_names[name] += 1
                base, ext = name.rsplit(".", 1) if "." in name else (name, "txt")
                out_name = f"{base}-{seen_names[name]}.{ext}"
            else:
                seen_names[name] = 0
                out_name = name

            node = mapping.get(msg_id, {})
            parts = (node.get("message") or {}).get("content", {}).get("parts", [])
            text_parts = [p for p in parts if isinstance(p, str)]
            if text_parts:
                (files_dir / out_name).write_text("\n\n".join(text_parts), encoding="utf-8")
                extracted_files += 1

    extracted_convos = 0
    for cid, detail in conv_mappings.items():
        mapping = detail.get("mapping") or {}
        title = detail.get("title") or "Untitled"
        nodes = walk_mapping_tree(mapping)
        lines = [f"# {title}", "", f"conversation_id: {cid}", ""]
        for node in nodes:
            msg = node.get("message")
            if not msg:
                continue
            role = (msg.get("author", {}).get("role", "") or "").lower()
            if role == "system":
                continue
            text = extract_node_text(node)
            if not text.strip():
                continue
            content_type = (msg.get("content") or {}).get("content_type", "")
            if role == "user":
                label = "User"
            elif role == "assistant":
                label = "Assistant"
            elif role == "tool":
                if content_type == "tether_browsing_display":
                    continue
                label = "Assistant (artifact)"
            else:
                label = role.title()
            lines.extend([f"## {label}", "", text, ""])
        slug = _slugify(title)
        (convos_dir / f"{slug}--{cid[:8]}.md").write_text("\n".join(lines), encoding="utf-8")
        extracted_convos += 1

    return {
        "generated_at": now_iso(),
        "project_id": project_id,
        "project_name": project_name,
        "output_root": str(output_root),
        "file_count": extracted_files,
        "total_files": len(files),
        "conversation_count": extracted_convos,
        "conversation_ids": list(conv_mappings.keys()),
    }


def render_discovery_text(payload: dict[str, Any]) -> str:
    lines = [
        f"ChatGPT cookie jar: {payload.get('cookie_jar', 'unknown')}",
        f"Generated: {payload['generated_at']}",
        f"Session state: {payload['session_state']}",
        f"Account: {payload.get('account_email') or payload.get('account_id') or 'unknown'}",
        f"Conversations: {payload['conversation_count']}",
        f"Calibration only: {payload['calibration_only']}",
        f"Recommended command: {payload['recommended_command']}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Project registry (The Post Office)
# ---------------------------------------------------------------------------

# ChatGPT Projects API (distinct from the GPT Store "gizmos" API).
#
# ChatGPT Projects and custom GPTs ("gizmos") are separate features served by
# different backend routes. The gizmos discovery API returns custom GPTs, NOT
# Projects, so the Post Office must talk to the Projects API instead
# (GH#16 / IRF-CCE-027). Project IDs are prefixed "g-p-" (gizmo-project).
#
# The JSON shapes parsed below follow the Projects API (flat ``id``/``name`` per
# item), while staying tolerant of legacy gizmo-wrapped entries for safety. When
# a valid ChatGPT session is available, live-verify these paths/shapes via
# browser DevTools (see playbooks/handoffs/gh-16-chatgpt-projects-endpoint.md).
PROJECTS_LIST_PATH = "backend-api/projects"
PROJECT_DETAIL_PATH = "backend-api/projects/{project_id}"

EXTRACTION_STATES = (
    "discovered",
    "queued",
    "extracting",
    "extracted",
    "partial",
    "failed",
    "routed",
    "delivered",
)


def _project_registry_path(project_root: Path) -> Path:
    return project_root / "state" / "chatgpt-project-registry.json"


def load_project_registry(project_root: Path) -> dict[str, Any]:
    path = _project_registry_path(project_root)
    if not path.exists():
        return {
            "generated_at": now_iso(),
            "account_id": "",
            "project_count": 0,
            "projects": {},
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {
            "generated_at": now_iso(),
            "account_id": "",
            "project_count": 0,
            "projects": {},
        }


def save_project_registry(project_root: Path, registry: dict[str, Any]) -> Path:
    path = _project_registry_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    registry["generated_at"] = now_iso()
    registry["project_count"] = len(registry.get("projects") or {})
    path.write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")
    return path


def _blank_project_entry(
    project_id: str, name: str, interactions: int, file_count: int
) -> dict[str, Any]:
    return {
        "name": name,
        "interactions": interactions,
        "file_count": file_count,
        "discovered_at": now_iso(),
        "extraction_state": "discovered",
        "extracted_at": None,
        "extraction_manifest": None,
        "route": None,
    }


def merge_project_discovery(
    existing: dict[str, Any], discovered: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Merge freshly discovered projects into the existing registry.

    Preserves extraction_state, extraction_manifest, and route for projects
    that were already tracked. Updates name/interactions/file_count from
    the latest discovery.
    """
    projects = dict(existing.get("projects") or {})
    for pid, disc in discovered.items():
        if pid in projects:
            entry = projects[pid]
            entry["name"] = disc.get("name", entry.get("name", ""))
            entry["interactions"] = disc.get("interactions", entry.get("interactions", 0))
            entry["file_count"] = disc.get("file_count", entry.get("file_count", 0))
        else:
            projects[pid] = _blank_project_entry(
                pid,
                disc.get("name", ""),
                disc.get("interactions", 0),
                disc.get("file_count", 0),
            )
    result = dict(existing)
    result["projects"] = projects
    result["project_count"] = len(projects)
    result["generated_at"] = now_iso()
    return result


def _project_display_name(item: dict[str, Any]) -> str:
    """Extract a project's display name from a Projects-API or legacy gizmo entry."""
    name = item.get("name") or item.get("title")
    if name:
        return name
    gizmo = item.get("gizmo") or item.get("resource") or {}
    display = gizmo.get("display") or {}
    return display.get("name") or gizmo.get("name") or gizmo.get("title") or ""


def _parse_project_item(item: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Normalize one Projects-API listing item to ``(project_id, info)``.

    Reads the flat Projects shape (``id``/``name``) first and falls back to a
    legacy gizmo-wrapped entry. Returns ``None`` when no project id is present.
    """
    pid = item.get("id") or ""
    if not pid:
        gizmo = item.get("gizmo") or item.get("resource") or {}
        pid = gizmo.get("id") or ""
    if not pid:
        return None
    interactions = (
        item.get("num_conversations")
        or item.get("conversation_count")
        or (item.get("vanity_metrics") or {}).get("num_conversations")
        or 0
    )
    info = {
        "name": _project_display_name(item),
        "interactions": interactions,
        "file_count": len(item.get("files") or []),
    }
    return pid, info


def discover_chatgpt_projects(
    cookie_jar: Path = DEFAULT_CHATGPT_COOKIE_JAR,
) -> dict[str, dict[str, Any]]:
    """Fetch all ChatGPT Project metadata from the Projects API.

    Uses the ChatGPT Projects API (``backend-api/projects``), NOT the GPT Store
    "gizmos" discovery API. Projects and custom GPTs are distinct ChatGPT
    features served by different backend routes; the gizmos endpoint returns
    custom GPTs, not Projects (GH#16 / IRF-CCE-027). Returns ``{project_id: info}``.
    """

    session = build_chatgpt_session(cookie_jar)
    projects: dict[str, dict[str, Any]] = {}
    offset = 0
    limit = 100
    while True:
        url = (
            f"https://{CHATGPT_HOST}/{PROJECTS_LIST_PATH}?"
            f"{urlencode({'offset': offset, 'limit': limit})}"
        )
        try:
            page = fetch_json(session, url)
        except ChatGPTLocalSessionError:
            break
        items = page.get("items") or page.get("list", {}).get("items") or []
        if not items:
            break
        for item in items:
            parsed = _parse_project_item(item)
            if parsed is not None:
                pid, info = parsed
                projects[pid] = info
        if len(items) < limit:
            break
        offset += limit
        time.sleep(1)
    return projects


def set_project_route(
    project_root: Path,
    project_id: str,
    destination: str,
    *,
    organ: str = "",
    repo: str = "",
) -> dict[str, Any]:
    """Set the delivery destination for a project in the registry."""
    registry = load_project_registry(project_root)
    projects = registry.get("projects") or {}
    if project_id not in projects:
        raise ChatGPTLocalSessionError(
            f"Project {project_id} not in registry. Run 'cce project discover' first."
        )
    entry = projects[project_id]
    entry["route"] = {
        "destination": destination,
        "organ": organ,
        "repo": repo,
        "routed_at": now_iso(),
    }
    if entry["extraction_state"] == "discovered":
        entry["extraction_state"] = "queued"
    save_project_registry(project_root, registry)
    return entry


def render_project_status(registry: dict[str, Any]) -> str:
    """Render a human-readable status table from the project registry."""
    projects = registry.get("projects") or {}
    if not projects:
        return "No projects in registry. Run 'cce project discover' first."

    state_counts: dict[str, int] = {}
    for entry in projects.values():
        state = entry.get("extraction_state", "unknown")
        state_counts[state] = state_counts.get(state, 0) + 1

    lines = [
        f"Projects: {len(projects)}",
        "",
        "State breakdown:",
    ]
    for state in EXTRACTION_STATES:
        count = state_counts.get(state, 0)
        if count:
            lines.append(f"  {state}: {count}")

    lines.extend(["", "Per-project:"])
    for _pid, entry in sorted(projects.items(), key=lambda x: x[1].get("name", "")):
        name = entry.get("name", "")
        state = entry.get("extraction_state", "unknown")
        route = entry.get("route") or {}
        dest = route.get("destination", "")
        dest_suffix = f" -> {dest}" if dest else ""
        lines.append(f"  {name:40s} [{state:12s}]{dest_suffix}")

    return "\n".join(lines)


def sync_chatgpt_projects(
    project_root: Path,
    *,
    batch_size: int = 5,
    cookie_jar: Path = DEFAULT_CHATGPT_COOKIE_JAR,
) -> dict[str, Any]:
    """Extract queued/stale projects to their routed destinations.

    Returns a summary of what was extracted, skipped, and failed.
    """

    registry = load_project_registry(project_root)
    projects = registry.get("projects") or {}

    candidates = [
        (pid, entry)
        for pid, entry in projects.items()
        if entry.get("extraction_state") in ("queued", "partial") and entry.get("route")
    ]

    extracted: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    skipped = 0

    for i, (pid, entry) in enumerate(candidates[:batch_size]):
        route = entry["route"]
        destination = Path(route["destination"])

        entry["extraction_state"] = "extracting"
        save_project_registry(project_root, registry)

        try:
            manifest = fetch_chatgpt_project(pid, destination, cookie_jar=cookie_jar)
            entry["extraction_state"] = "delivered"
            entry["extracted_at"] = now_iso()
            entry["extraction_manifest"] = {
                "files_extracted": manifest.get("file_count", 0),
                "conversations_extracted": manifest.get("conversation_count", 0),
                "output_root": str(destination),
            }
            extracted.append({"project_id": pid, "name": entry.get("name", ""), **manifest})
        except Exception as exc:
            entry["extraction_state"] = "failed"
            failed.append({"project_id": pid, "name": entry.get("name", ""), "error": str(exc)})

        save_project_registry(project_root, registry)
        if i < len(candidates[:batch_size]) - 1:
            time.sleep(3)

    skipped = len(candidates) - min(batch_size, len(candidates))

    return {
        "generated_at": now_iso(),
        "batch_size": batch_size,
        "candidates_total": len(candidates),
        "extracted_count": len(extracted),
        "failed_count": len(failed),
        "skipped_count": skipped,
        "extracted": extracted,
        "failed": failed,
    }
