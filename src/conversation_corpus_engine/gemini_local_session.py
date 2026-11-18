"""Gemini local-session client.

Provides a "full content path" for Gemini conversations from the local macOS
app session/cache.

Two source strategies (tried in order):
1. Local app cache decryption: reads ChatInfo2.store (SwiftData/SQLite) and
   decrypts ZENCRYPTEDPROTOBYTES using the Gemini Safe Storage key from the
   macOS keychain (Chrome-style AES-128-CBC derivation, as exercised by
   scripts/spike_gemini_cache.py). This yields conversation/message content
   when the local store's encryption scheme matches.
2. Chrome cookie session: reads Gemini/Google cookies from Chrome's Chromium
   cookie store to authenticate against gemini.google.com. Web-API endpoint
   mapping (batchexecute RPCs) is environment-dependent; when the mapping is
   unavailable this path reports a clear, actionable error rather than
   importing partial data.

The adapter mirrors the shipped ChatGPT/Claude/Perplexity local-session shape
(``discover_gemini_local_session``, ``fetch_gemini_local_session_bundle``).
"""

from __future__ import annotations

import json
import sqlite3
import struct
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import pbkdf2_hmac, sha256
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .chatgpt_local_session import Cookie, build_cookie_header

DEFAULT_GEMINI_STORE = Path.home() / (
    "Library/Caches/com.google.GeminiMacOS/Gemini/user1/ChatInfo2.store"
)
DEFAULT_CHROME_COOKIES = Path.home() / "Library/Application Support/Google/Chrome/Default/Cookies"
CHROME_SAFE_STORAGE_SERVICES = ("Chrome Safe Storage", "Chromium Safe Storage")
GEMINI_SAFE_STORAGE_SERVICES = ("Gemini Safe Storage",)
GEMINI_HOST = "gemini.google.com"
GEMINI_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
MAGIC_HEADER = bytes.fromhex("2ebafeca2ebafeca")
CORE_DATA_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)


class GeminiLocalSessionError(RuntimeError):
    pass


@dataclass(frozen=True)
class GeminiHttpSession:
    cookies: list[Cookie]
    account_email: str
    account_id: str


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Keychain / decryption helpers (local cache full-content path)
# ---------------------------------------------------------------------------


def _find_safe_storage_password(services: tuple[str, ...], label: str) -> str:
    for service in services:
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
    raise GeminiLocalSessionError(f"No {label} password found in macOS keychain")


def _derive_key(password: str) -> bytes:  # allow-secret
    return pbkdf2_hmac("sha1", password.encode("utf-8"), b"saltysalt", 1003, 16)


def _decrypt_aes_cbc(ciphertext: bytes, key: bytes, iv: bytes = b" " * 16) -> bytes | None:
    result = subprocess.run(
        ["openssl", "enc", "-aes-128-cbc", "-d", "-K", key.hex(), "-iv", iv.hex(), "-nopad"],
        input=ciphertext,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        return None
    padded = result.stdout
    pad_length = padded[-1]
    if pad_length < 1 or pad_length > 16:
        return padded
    return padded[:-pad_length]


def _decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    val = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        val |= (b & 0x7F) << shift
        shift += 7
        pos += 1
        if not (b & 0x80):
            break
    return val, pos


def _decode_protobuf_fields(data: bytes, *, depth: int = 0) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    if depth > 8:
        return fields
    pos = 0
    while pos < len(data):
        try:
            tag, pos = _decode_varint(data, pos)
        except Exception:
            break
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:
            try:
                val, pos = _decode_varint(data, pos)
            except Exception:
                break
            fields.append({"field": field_num, "type": "varint", "value": val})
        elif wire_type == 1:
            if pos + 8 > len(data):
                break
            val = struct.unpack("<d", data[pos : pos + 8])[0]
            fields.append({"field": field_num, "type": "fixed64", "value": val})
            pos += 8
        elif wire_type == 2:
            try:
                length, pos = _decode_varint(data, pos)
            except Exception:
                break
            if length < 0 or pos + length > len(data):
                break
            content = data[pos : pos + length]
            pos += length
            try:
                text = content.decode("utf-8")
                if all((c.isprintable() or c in "\n\r\t") for c in text):
                    fields.append(
                        {"field": field_num, "type": "string", "value": text, "length": length}
                    )
                    continue
            except UnicodeDecodeError:
                pass
            sub = _decode_protobuf_fields(content, depth=depth + 1)
            if sub:
                fields.append({"field": field_num, "type": "message", "value": sub, "length": length})
            else:
                fields.append(
                    {"field": field_num, "type": "bytes", "value": content.hex()[:200], "length": length}
                )
        elif wire_type == 5:
            if pos + 4 > len(data):
                break
            val = struct.unpack("<f", data[pos : pos + 4])[0]
            fields.append({"field": field_num, "type": "fixed32", "value": val})
            pos += 4
        else:
            break
    return fields


def _strip_gemini_header(data: bytes) -> bytes:
    if data[:8] == MAGIC_HEADER and len(data) > 12:
        return data[12:]
    return data


def _try_decrypt_payload(payload: bytes, key: bytes) -> bytes | None:
    for iv in (b" " * 16, b"\x00" * 16):
        decrypted = _decrypt_aes_cbc(payload, key, iv=iv)
        if decrypted and len(decrypted) > 4:
            return decrypted
    if payload[:3] == b"v10":
        for iv in (b" " * 16, b"\x00" * 16):
            decrypted = _decrypt_aes_cbc(payload[3:], key, iv=iv)
            if decrypted:
                return decrypted
    fields = _decode_protobuf_fields(payload)
    for f in fields:
        if f.get("type") == "bytes" and f.get("length", 0) > 32:
            try:
                blob = bytes.fromhex(str(f.get("value", "")))
            except ValueError:
                continue
            for iv in (b" " * 16, b"\x00" * 16):
                decrypted = _decrypt_aes_cbc(blob, key, iv=iv)
                if decrypted and len(decrypted) > 4:
                    return decrypted
    return None


def _collect_strings(fields: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for f in fields:
        if f.get("type") == "string":
            val = str(f.get("value", "")).strip()
            if val:
                out.append(val)
        elif f.get("type") == "message":
            out.extend(_collect_strings(f.get("value") or []))
    return out


def _iso_coredata(core_data_seconds: float | None) -> str:
    if core_data_seconds is None:
        return ""
    return (CORE_DATA_EPOCH + timedelta(seconds=float(core_data_seconds))).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def extract_local_cache_content(store: Path = DEFAULT_GEMINI_STORE) -> dict[str, Any]:
    """Decrypt and extract full conversation content from the local Gemini app cache."""
    if not store.exists():
        raise GeminiLocalSessionError(f"Gemini app cache not found: {store}")
    try:
        password = _find_safe_storage_password(GEMINI_SAFE_STORAGE_SERVICES, "Gemini Safe Storage")  # allow-secret
        key = _derive_key(password)  # allow-secret
    except GeminiLocalSessionError:
        try:
            password = _find_safe_storage_password(  # allow-secret
                CHROME_SAFE_STORAGE_SERVICES, "Chrome Safe Storage"
            )
            key = _derive_key(password)  # allow-secret
        except GeminiLocalSessionError as exc:
            raise GeminiLocalSessionError(
                "Unable to obtain a keychain key to decrypt Gemini local cache content. "
                "Grant keychain access for Gemini Safe Storage and retry."
            ) from exc

    conn = sqlite3.connect(store)
    try:
        chats = conn.execute(
            "SELECT ZCHATUUID, ZROBINCONVERSATIONID, ZLASTUPDATEDTIME, ZENCRYPTEDPROTOBYTES "
            "FROM ZCHATINFOSTOREDMODEL ORDER BY ZLASTUPDATEDTIME"
        ).fetchall()
        messages = conn.execute(
            "SELECT ZCHATUUID, ZMESSAGEUUID, ZMESSAGEINDEX, ZCREATEDTIME, ZISUSERTURN, "
            "ZENCRYPTEDPROTOBYTES FROM ZCHATMESSAGESTOREDMODEL ORDER BY ZCHATUUID, ZMESSAGEINDEX"
        ).fetchall()
    finally:
        conn.close()

    chat_meta: dict[str, dict[str, Any]] = {}
    chat_texts: dict[str, list[str]] = {}
    for chat_uuid, robin, updated, blob in chats:
        texts: list[str] = []
        if blob:
            payload = _strip_gemini_header(bytes(blob))
            decrypted = _try_decrypt_payload(payload, key)
            if decrypted:
                texts = _collect_strings(_decode_protobuf_fields(decrypted))
        chat_meta[chat_uuid] = {
            "chat_uuid": chat_uuid,
            "robin_conversation_id": robin or "",
            "updated_at": _iso_coredata(updated),
        }
        if texts:
            chat_texts[chat_uuid] = texts

    msg_by_chat: dict[str, list[dict[str, Any]]] = {}
    for chat_uuid, msg_uuid, idx, created, is_user, blob in messages:
        texts: list[str] = []
        if blob:
            payload = _strip_gemini_header(bytes(blob))
            decrypted = _try_decrypt_payload(payload, key)
            if decrypted:
                texts = _collect_strings(_decode_protobuf_fields(decrypted))
        body = max(texts, key=len) if texts else ""
        msg_by_chat.setdefault(chat_uuid, []).append(
            {
                "message_uuid": msg_uuid or "",
                "index": idx,
                "created_at": _iso_coredata(created),
                "role": "user" if is_user else "assistant",
                "text": body,
                "extracted_strings": texts,
            }
        )

    conversations: list[dict[str, Any]] = []
    for chat_uuid, meta in chat_meta.items():
        msgs = msg_by_chat.get(chat_uuid, [])
        title_candidates = [t for t in chat_texts.get(chat_uuid, []) if 0 < len(t) <= 120]
        first_user = next(
            (m["text"] for m in msgs if m.get("role") == "user" and m.get("text")), ""
        )
        title = (
            title_candidates[0]
            if title_candidates
            else (first_user[:80] if first_user else "Untitled Gemini Conversation")
        )
        conversations.append(
            {
                "conversation_id": chat_uuid,
                "uuid": chat_uuid,
                "robin_conversation_id": meta.get("robin_conversation_id", ""),
                "title": title,
                "updated_at": meta.get("updated_at", ""),
                "messages": [m for m in msgs if m.get("text")],
                "raw_message_count": len(msgs),
            }
        )

    known = set(chat_meta)
    for chat_uuid, msgs in msg_by_chat.items():
        if chat_uuid in known:
            continue
        first_user = next(
            (m["text"] for m in msgs if m.get("role") == "user" and m.get("text")), ""
        )
        conversations.append(
            {
                "conversation_id": chat_uuid,
                "uuid": chat_uuid,
                "robin_conversation_id": "",
                "title": first_user[:80] if first_user else "Untitled Gemini Conversation",
                "updated_at": "",
                "messages": [m for m in msgs if m.get("text")],
                "raw_message_count": len(msgs),
                "orphan": True,
            }
        )

    extracted_with_content = sum(1 for c in conversations if c.get("messages"))
    return {
        "generated_at": now_iso(),
        "source": "gemini-local-cache",
        "store_path": str(store),
        "conversation_count": len(conversations),
        "conversations_with_content": extracted_with_content,
        "conversations": conversations,
    }


# ---------------------------------------------------------------------------
# Chrome cookie session path (web API - endpoint mapping dependent)
# ---------------------------------------------------------------------------


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


def load_gemini_cookies_from_chrome(
    chrome_cookies: Path = DEFAULT_CHROME_COOKIES,
) -> list[Cookie]:
    if not chrome_cookies.exists():
        raise GeminiLocalSessionError(f"Chrome Cookies database not found: {chrome_cookies}")
    safe_pw = _find_safe_storage_password(CHROME_SAFE_STORAGE_SERVICES, "Chrome Safe Storage")
    key = pbkdf2_hmac("sha1", safe_pw.encode("utf-8"), b"saltysalt", 1003, 16)
    connection = sqlite3.connect(chrome_cookies)
    try:
        rows = connection.execute(
            "SELECT host_key, name, value, encrypted_value, path, is_secure, is_httponly "
            "FROM cookies WHERE host_key LIKE ? OR host_key LIKE ?",
            ("%gemini.google%", "%.google.com%"),
        ).fetchall()
    finally:
        connection.close()
    cookies: list[Cookie] = []
    for host_key, name, value, encrypted_value, path, secure, http_only in rows:
        decrypted = value if value else _decrypt_chrome_cookie(encrypted_value, host_key, key)
        if not decrypted:
            continue
        try:
            decrypted.encode("latin-1")
        except UnicodeEncodeError:
            continue
        cookies.append(
            Cookie(host_key, name, path or "/", decrypted, bool(secure), bool(http_only))
        )
    if not cookies:
        raise GeminiLocalSessionError("No Gemini/Google cookies found in Chrome cookie store")
    return cookies


def build_gemini_web_session(
    cookie_source: Path = DEFAULT_CHROME_COOKIES,
) -> GeminiHttpSession:
    cookies = load_gemini_cookies_from_chrome(cookie_source)
    return GeminiHttpSession(cookies=cookies, account_email="", account_id="")


# ---------------------------------------------------------------------------
# Discovery / bundle (full content path)
# ---------------------------------------------------------------------------


def _resolve_store_path(source: Path | None) -> Path:
    if source is None:
        return DEFAULT_GEMINI_STORE
    resolved = source.resolve()
    if resolved.is_file():
        return resolved
    candidate = resolved / "Gemini" / "user1" / "ChatInfo2.store"
    if candidate.exists():
        return candidate
    return DEFAULT_GEMINI_STORE


def discover_gemini_local_session(source: Path | None = None) -> dict[str, Any]:
    store = _resolve_store_path(source)
    cache_available = store.exists()
    chrome_available = DEFAULT_CHROME_COOKIES.exists()
    conversation_count = 0
    content_count = 0
    note = ""
    if cache_available:
        try:
            bundle = extract_local_cache_content(store)
            conversation_count = bundle.get("conversation_count", 0)
            content_count = bundle.get("conversations_with_content", 0)
        except Exception as exc:  # noqa: BLE001
            note = f"cache extraction unavailable: {exc}"
    elif chrome_available:
        note = "local cache missing; Chrome cookie session available but web endpoint mapping required"
    else:
        note = "no local cache or Chrome cookies found"
    return {
        "generated_at": now_iso(),
        "source": str(store),
        "adapter_type": "gemini-local-session",
        "collection_scope": "local-session",
        "session_state": "ready" if (cache_available or chrome_available) else "unavailable",
        "cache_available": cache_available,
        "chrome_cookies_available": chrome_available,
        "conversation_count": conversation_count,
        "conversations_with_content": content_count,
        "note": note,
        "recommended_command": (
            "cce provider import --provider gemini --mode local-session --register --build"
        ),
        "calibration_only": True,
    }


def fetch_gemini_local_session_bundle(
    source: Path | None = None,
    *,
    limit: int = 0,
) -> dict[str, Any]:
    """Fetch Gemini conversations via the full local content path."""
    store = _resolve_store_path(source)
    cache_error: Exception | None = None
    if store.exists():
        try:
            bundle = extract_local_cache_content(store)
            convs = bundle.get("conversations") or []
            if limit and limit > 0:
                convs = convs[:limit]
                bundle["conversations"] = convs
            bundle["fetched_count"] = len(convs)
            bundle["total_count"] = bundle.get("conversation_count", len(convs))
            bundle["adapter_type"] = "gemini-local-session"
            bundle["collection_scope"] = "local-session"
            bundle["conversation_detail_failures"] = []
            bundle["acquisition_report"] = {
                "generated_at": now_iso(),
                "source": "local-cache",
                "total_listed": len(convs),
                "fetched_count": len(convs),
                "failure_count": 0,
                "full_refresh": True,
            }
            if bundle.get("conversations_with_content", 0) > 0 or convs:
                return bundle
            cache_error = GeminiLocalSessionError(
                "Local Gemini cache was read but no decryptable conversation content was extracted."
            )
        except Exception as exc:  # noqa: BLE001
            cache_error = exc

    if DEFAULT_CHROME_COOKIES.exists():
        try:
            build_gemini_web_session(DEFAULT_CHROME_COOKIES)
            raise GeminiLocalSessionError(
                "Gemini web API endpoint mapping is not yet implemented for full content fetch. "
                "Local cache decryption did not yield content. "
                "Provide a Google Takeout export (Gemini Apps/MyActivity.json) or extend endpoint mapping."
            )
        except GeminiLocalSessionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise GeminiLocalSessionError(f"Gemini web session path failed: {exc}") from exc

    if cache_error:
        raise GeminiLocalSessionError(str(cache_error)) from cache_error
    raise GeminiLocalSessionError(
        "No Gemini local-session source available: local app cache missing and no Chrome cookies found."
    )


def render_discovery_text(payload: dict[str, Any]) -> str:
    lines = [
        f"Gemini source: {payload.get('source', 'unknown')}",
        f"Generated: {payload.get('generated_at', '')}",
        f"Session state: {payload.get('session_state', '')}",
        f"Cache available: {payload.get('cache_available')}",
        f"Chrome cookies available: {payload.get('chrome_cookies_available')}",
        f"Conversations: {payload.get('conversation_count', 0)}",
        f"Conversations with content: {payload.get('conversations_with_content', 0)}",
        f"Note: {payload.get('note', '')}",
        f"Recommended command: {payload.get('recommended_command', '')}",
    ]
    return "\n".join(lines)
