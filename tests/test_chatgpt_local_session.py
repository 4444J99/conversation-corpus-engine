from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conversation_corpus_engine import chatgpt_local_session as module  # noqa: E402
from conversation_corpus_engine.chatgpt_local_session import (
    ChatGPTLocalSessionError,
    scope_preflight_check,
)


def _seed_acquisition_state(output_root: Path, conversation_count: int) -> None:
    """Write a minimal acquisition-state.json with N conversations."""
    import json  # noqa: PLC0415

    state_dir = output_root / "source"
    state_dir.mkdir(parents=True, exist_ok=True)
    convs = {f"conv-{i}": {"update_time": i} for i in range(conversation_count)}
    payload = {
        "generated_at": "2026-01-01T00:00:00+00:00",
        "conversation_count": conversation_count,
        "conversations": convs,
        "last_acquisition_report": {},
    }
    (state_dir / "acquisition-state.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


class TestScopePreflightCheck:
    def test_ok_when_count_within_threshold(self, tmp_path: Path) -> None:
        _seed_acquisition_state(tmp_path, 100)
        result = scope_preflight_check(90, tmp_path)
        assert result["status"] == "ok"
        assert result["current"] == 90
        assert result["prior"] == 100
        assert result["delta_pct"] == -10.0

    def test_unknown_when_no_prior_state(self, tmp_path: Path) -> None:
        result = scope_preflight_check(50, tmp_path)
        assert result["status"] == "unknown"
        assert result["current"] == 50
        assert result["prior"] == 0

    def test_degraded_raises_when_below_threshold(self, tmp_path: Path) -> None:
        _seed_acquisition_state(tmp_path, 633)
        with pytest.raises(ChatGPTLocalSessionError, match="Session scope degraded"):
            scope_preflight_check(4, tmp_path)

    def test_ok_when_count_grows(self, tmp_path: Path) -> None:
        _seed_acquisition_state(tmp_path, 100)
        result = scope_preflight_check(150, tmp_path)
        assert result["status"] == "ok"
        assert result["delta_pct"] == 50.0

    def test_ok_at_exact_threshold_boundary(self, tmp_path: Path) -> None:
        _seed_acquisition_state(tmp_path, 100)
        result = scope_preflight_check(50, tmp_path)
        assert result["status"] == "ok"

    def test_degraded_just_below_threshold(self, tmp_path: Path) -> None:
        _seed_acquisition_state(tmp_path, 100)
        with pytest.raises(ChatGPTLocalSessionError):
            scope_preflight_check(49, tmp_path)


def build_binary_cookie_jar(cookies: list[dict[str, str]]) -> bytes:
    """Build a minimal Apple binary cookie jar for testing."""
    pages: list[bytes] = []
    page_cookies: list[bytes] = []
    for cookie in cookies:
        domain = cookie["domain"].encode("utf-8") + b"\x00"
        name = cookie["name"].encode("utf-8") + b"\x00"
        path = cookie.get("path", "/").encode("utf-8") + b"\x00"
        value = cookie["value"].encode("utf-8") + b"\x00"
        flags = 0
        if cookie.get("secure"):
            flags |= 1
        if cookie.get("http_only"):
            flags |= 4

        header_size = 32
        domain_offset = header_size
        name_offset = domain_offset + len(domain)
        path_offset = name_offset + len(name)
        value_offset = path_offset + len(path)
        total_size = value_offset + len(value)

        chunk = bytearray(total_size)
        struct.pack_into("<I", chunk, 0, total_size)
        struct.pack_into("<I", chunk, 8, flags)
        struct.pack_into("<I", chunk, 16, domain_offset)
        struct.pack_into("<I", chunk, 20, name_offset)
        struct.pack_into("<I", chunk, 24, path_offset)
        struct.pack_into("<I", chunk, 28, value_offset)
        chunk[domain_offset : domain_offset + len(domain)] = domain
        chunk[name_offset : name_offset + len(name)] = name
        chunk[path_offset : path_offset + len(path)] = path
        chunk[value_offset : value_offset + len(value)] = value
        page_cookies.append(bytes(chunk))

    cookie_count = len(page_cookies)
    offsets_area_size = 8 + 4 * cookie_count
    cookie_offsets = []
    running_offset = offsets_area_size
    for pc in page_cookies:
        cookie_offsets.append(running_offset)
        running_offset += len(pc)

    page = bytearray()
    page += struct.pack("<I", 0x00000100)  # page header magic
    page += struct.pack("<I", cookie_count)
    for co in cookie_offsets:
        page += struct.pack("<I", co)
    for pc in page_cookies:
        page += pc
    page_bytes = bytes(page)
    pages.append(page_bytes)

    data = bytearray()
    data += b"cook"
    data += struct.pack(">I", len(pages))
    for p in pages:
        data += struct.pack(">I", len(p))
    for p in pages:
        data += p
    return bytes(data)


def test_parse_binary_cookies_extracts_domain_name_value(tmp_path: Path) -> None:
    jar = tmp_path / "test.binarycookies"
    jar.write_bytes(
        build_binary_cookie_jar(
            [
                {"domain": ".chatgpt.com", "name": "session", "value": "abc123"},
                {"domain": ".openai.com", "name": "oai-did", "value": "device-1"},
            ]
        )
    )

    cookies = module.parse_binary_cookies(jar)

    assert len(cookies) == 2
    assert cookies[0].domain == ".chatgpt.com"
    assert cookies[0].name == "session"
    assert cookies[0].value == "abc123"
    assert cookies[1].name == "oai-did"
    assert cookies[1].value == "device-1"


def test_parse_binary_cookies_rejects_non_cook_magic(tmp_path: Path) -> None:
    jar = tmp_path / "bad.binarycookies"
    jar.write_bytes(b"BAAD" + b"\x00" * 100)

    with pytest.raises(module.ChatGPTLocalSessionError, match="Unsupported cookie jar"):
        module.parse_binary_cookies(jar)


def test_cookie_matches_host_handles_domain_prefix() -> None:
    cookie = module.Cookie(
        domain=".chatgpt.com", name="x", path="/", value="y", secure=False, http_only=False
    )
    assert module.cookie_matches_host(cookie, "chatgpt.com")
    assert module.cookie_matches_host(cookie, "www.chatgpt.com")
    assert not module.cookie_matches_host(cookie, "evil-chatgpt.com")


def test_build_cookie_header_selects_matching_cookies() -> None:
    cookies = [
        module.Cookie(".chatgpt.com", "a", "/", "1", False, False),
        module.Cookie(".openai.com", "b", "/", "2", False, False),
    ]

    header = module.build_cookie_header(cookies, "https://chatgpt.com/api/test")
    assert header == "a=1"


def test_find_cookie_value_returns_match_or_empty() -> None:
    cookies = [
        module.Cookie(".chatgpt.com", "oai-did", "/", "device-42", False, False),
    ]

    assert module.find_cookie_value(cookies, "chatgpt.com", "oai-did") == "device-42"
    assert module.find_cookie_value(cookies, "chatgpt.com", "missing") == ""


def test_resolve_chatgpt_cookie_jar_validates_existence_and_magic(tmp_path: Path) -> None:
    with pytest.raises(module.ChatGPTLocalSessionError, match="does not exist"):
        module.resolve_chatgpt_cookie_jar(tmp_path / "nonexistent.binarycookies")

    bad = tmp_path / "bad.binarycookies"
    bad.write_bytes(b"NOPE" + b"\x00" * 100)
    with pytest.raises(module.ChatGPTLocalSessionError, match="unexpected format"):
        module.resolve_chatgpt_cookie_jar(bad)

    good = tmp_path / "good.binarycookies"
    good.write_bytes(
        build_binary_cookie_jar([{"domain": ".chatgpt.com", "name": "x", "value": "y"}])
    )
    assert module.resolve_chatgpt_cookie_jar(good) == good.resolve()


def test_build_chatgpt_session_extracts_token_and_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    jar = tmp_path / "test.binarycookies"
    jar.write_bytes(
        build_binary_cookie_jar(
            [
                {"domain": ".chatgpt.com", "name": "session", "value": "sess"},
                {"domain": ".chatgpt.com", "name": "oai-did", "value": "device-1"},
            ]
        )
    )

    monkeypatch.setattr(
        module,
        "_fetch_session",
        lambda cookies: {
            "accessToken": "tok-abc",  # allow-secret
            "account": {"id": "acct-1", "account_id": "acct-1"},
            "user": {"email": "user@example.com"},
        },
    )

    session = module.build_chatgpt_session(jar)

    assert session.access_token == "tok-abc"  # allow-secret
    assert session.account_id == "acct-1"
    assert session.device_id == "device-1"


def test_discover_chatgpt_local_session_returns_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    jar = tmp_path / "test.binarycookies"
    jar.write_bytes(
        build_binary_cookie_jar([{"domain": ".chatgpt.com", "name": "x", "value": "y"}])
    )

    monkeypatch.setattr(
        module,
        "build_chatgpt_session",
        lambda cookie_jar: module.ChatGPTHttpSession(
            cookies=[],
            session_payload={
                "account": {"id": "acct-1"},
                "user": {"email": "user@example.com", "name": "Test"},
            },
            access_token="tok",  # allow-secret
            account_id="acct-1",
            device_id="dev-1",
        ),
    )
    monkeypatch.setattr(
        module,
        "fetch_json",
        lambda session, url: {"total": 42, "items": []},
    )

    payload = module.discover_chatgpt_local_session(jar)

    assert payload["session_state"] == "ready"
    assert payload["conversation_count"] == 42
    assert payload["account_email"] == "user@example.com"


def test_fetch_chatgpt_local_session_bundle_returns_conversations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    jar = tmp_path / "test.binarycookies"
    jar.write_bytes(
        build_binary_cookie_jar([{"domain": ".chatgpt.com", "name": "x", "value": "y"}])
    )

    monkeypatch.setattr(
        module,
        "build_chatgpt_session",
        lambda cookie_jar: module.ChatGPTHttpSession(
            cookies=[],
            session_payload={
                "account": {"id": "acct-1"},
                "user": {"email": "user@example.com"},
            },
            access_token="tok",  # allow-secret
            account_id="acct-1",
            device_id="dev-1",
        ),
    )

    def fake_fetch_json(session: module.ChatGPTHttpSession, url: str) -> object:
        if "conversations?" in url:
            return {
                "total": 1,
                "items": [{"id": "conv-1", "title": "Test Chat"}],
            }
        if "conversation/conv-1" in url:
            return {
                "conversation_id": "conv-1",
                "title": "Test Chat",
                "create_time": 1711900000,
                "update_time": 1711900100,
                "mapping": {"node-1": {"id": "node-1"}},
                "current_node": "node-1",
            }
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(module, "fetch_json", fake_fetch_json)

    bundle = module.fetch_chatgpt_local_session_bundle(jar, limit=10)

    assert bundle["fetched_count"] == 1
    assert bundle["total_count"] == 1
    assert bundle["conversations"][0]["conversation_id"] == "conv-1"
    assert bundle["conversations"][0]["mapping"]["node-1"]["id"] == "node-1"
    assert bundle["conversation_detail_failures"] == []


def test_render_discovery_text_includes_key_fields() -> None:
    text = module.render_discovery_text(
        {
            "cookie_jar": "/tmp/cookies",
            "generated_at": "2026-03-25T00:00:00+00:00",
            "session_state": "ready",
            "account_email": "user@example.com",
            "account_id": "acct-1",
            "conversation_count": 42,
            "calibration_only": True,
            "recommended_command": "cce provider import --provider chatgpt --mode local-session",
        }
    )

    assert "ChatGPT cookie jar: /tmp/cookies" in text
    assert "user@example.com" in text
    assert "Conversations: 42" in text


def test_acquisition_state_persistence_roundtrips(tmp_path: Path) -> None:
    conversations = {
        "conv-1": {"update_time": 1711900000, "fetched_at": "2026-03-25T00:00:00Z"},
        "conv-2": {"update_time": 1711900100, "fetched_at": "2026-03-25T00:01:00Z"},
    }
    report = {"fetched_count": 2, "reused_count": 0}
    module.save_acquisition_state(tmp_path, conversations, report=report)
    loaded = module.load_prior_acquisition(tmp_path)

    assert loaded["conv-1"]["update_time"] == 1711900000
    assert loaded["conv-2"]["update_time"] == 1711900100
    assert len(loaded) == 2


def test_load_prior_acquisition_returns_empty_when_missing(tmp_path: Path) -> None:
    assert module.load_prior_acquisition(tmp_path) == {}


def test_conversation_payload_cache_roundtrips(tmp_path: Path) -> None:
    payload = {"conversation_id": "conv-1", "title": "Test", "mapping": {"n1": {}}}
    path = module.cache_conversation_payload(tmp_path, "conv-1", payload)
    assert path.exists()

    loaded = module.load_cached_conversation(tmp_path, "conv-1")
    assert loaded is not None
    assert loaded["conversation_id"] == "conv-1"
    assert loaded["mapping"] == {"n1": {}}


def test_load_cached_conversation_returns_none_when_missing(tmp_path: Path) -> None:
    assert module.load_cached_conversation(tmp_path, "nonexistent") is None


# ---------------------------------------------------------------------------
# Project registry tests
# ---------------------------------------------------------------------------


def test_project_registry_roundtrips(tmp_path: Path) -> None:
    registry = module.load_project_registry(tmp_path)
    assert registry["project_count"] == 0
    assert registry["projects"] == {}

    registry["projects"]["g-p-abc"] = module._blank_project_entry("g-p-abc", "test-proj", 10, 3)
    path = module.save_project_registry(tmp_path, registry)
    assert path.exists()

    loaded = module.load_project_registry(tmp_path)
    assert loaded["project_count"] == 1
    assert loaded["projects"]["g-p-abc"]["name"] == "test-proj"
    assert loaded["projects"]["g-p-abc"]["extraction_state"] == "discovered"


def test_load_project_registry_handles_corrupt_json(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "chatgpt-project-registry.json").write_text("NOT JSON")
    registry = module.load_project_registry(tmp_path)
    assert registry["project_count"] == 0


def test_merge_project_discovery_preserves_extraction_state() -> None:
    existing = {
        "generated_at": "old",
        "account_id": "acct-1",
        "project_count": 1,
        "projects": {
            "g-p-aaa": {
                "name": "old-name",
                "interactions": 5,
                "file_count": 2,
                "discovered_at": "old",
                "extraction_state": "delivered",
                "extracted_at": "2026-01-01",
                "extraction_manifest": {"files_extracted": 2},
                "route": {"destination": "/some/path"},
            },
        },
    }
    discovered = {
        "g-p-aaa": {"name": "new-name", "interactions": 15, "file_count": 4},
        "g-p-bbb": {"name": "brand-new", "interactions": 3, "file_count": 0},
    }

    result = module.merge_project_discovery(existing, discovered)

    # Existing project: name/interactions updated, extraction_state preserved
    assert result["projects"]["g-p-aaa"]["name"] == "new-name"
    assert result["projects"]["g-p-aaa"]["interactions"] == 15
    assert result["projects"]["g-p-aaa"]["extraction_state"] == "delivered"
    assert result["projects"]["g-p-aaa"]["extraction_manifest"] == {"files_extracted": 2}

    # New project: blank entry
    assert result["projects"]["g-p-bbb"]["name"] == "brand-new"
    assert result["projects"]["g-p-bbb"]["extraction_state"] == "discovered"
    assert result["project_count"] == 2


def _fake_project_session() -> module.ChatGPTHttpSession:
    return module.ChatGPTHttpSession(
        cookies=[],
        session_payload={"account": {"id": "acct-1"}, "user": {"email": "u@example.com"}},
        access_token="tok",  # allow-secret
        account_id="acct-1",
        device_id="dev-1",
    )


def test_project_display_name_prefers_flat_projects_shape() -> None:
    assert module._project_display_name({"id": "g-p-a", "name": "My Project"}) == "My Project"
    assert module._project_display_name({"title": "Titled"}) == "Titled"


def test_project_display_name_falls_back_to_gizmo_wrapped() -> None:
    item = {"gizmo": {"display": {"name": "Gizmo Display"}}}
    assert module._project_display_name(item) == "Gizmo Display"
    assert module._project_display_name({}) == ""


def test_parse_project_item_reads_flat_projects_shape() -> None:
    item = {
        "id": "g-p-aaa",
        "name": "Alpha",
        "num_conversations": 7,
        "files": [{"id": "f1"}, {"id": "f2"}],
    }
    parsed = module._parse_project_item(item)
    assert parsed is not None
    pid, info = parsed
    assert pid == "g-p-aaa"
    assert info == {"name": "Alpha", "interactions": 7, "file_count": 2}


def test_parse_project_item_tolerates_legacy_gizmo_entry() -> None:
    item = {
        "gizmo": {"id": "g-p-bbb", "display": {"name": "Beta"}},
        "vanity_metrics": {"num_conversations": 3},
    }
    parsed = module._parse_project_item(item)
    assert parsed is not None
    pid, info = parsed
    assert pid == "g-p-bbb"
    assert info["name"] == "Beta"
    assert info["interactions"] == 3


def test_parse_project_item_returns_none_without_id() -> None:
    assert module._parse_project_item({"name": "no-id"}) is None


def test_discover_chatgpt_projects_uses_projects_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    jar = tmp_path / "test.binarycookies"
    jar.write_bytes(
        build_binary_cookie_jar([{"domain": ".chatgpt.com", "name": "x", "value": "y"}])
    )
    monkeypatch.setattr(module, "build_chatgpt_session", lambda cookie_jar: _fake_project_session())
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)

    seen_urls: list[str] = []

    def fake_fetch_json(session: object, url: str) -> object:
        seen_urls.append(url)
        return {
            "items": [
                {"id": "g-p-aaa", "name": "Alpha", "num_conversations": 2},
                {"id": "g-p-bbb", "name": "Beta", "files": [{"id": "f"}]},
            ]
        }

    monkeypatch.setattr(module, "fetch_json", fake_fetch_json)

    projects = module.discover_chatgpt_projects(jar)

    # Hits the Projects API, NOT the gizmos discovery API.
    assert seen_urls
    assert "backend-api/projects?" in seen_urls[0]
    assert all("gizmos" not in url for url in seen_urls)
    assert projects["g-p-aaa"] == {"name": "Alpha", "interactions": 2, "file_count": 0}
    assert projects["g-p-bbb"]["file_count"] == 1


def test_discover_chatgpt_projects_stops_on_empty_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    jar = tmp_path / "test.binarycookies"
    jar.write_bytes(
        build_binary_cookie_jar([{"domain": ".chatgpt.com", "name": "x", "value": "y"}])
    )
    monkeypatch.setattr(module, "build_chatgpt_session", lambda cookie_jar: _fake_project_session())
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)
    monkeypatch.setattr(module, "fetch_json", lambda session, url: {"items": []})

    assert module.discover_chatgpt_projects(jar) == {}


def test_set_project_route_updates_registry(tmp_path: Path) -> None:
    registry = module.load_project_registry(tmp_path)
    registry["projects"]["g-p-aaa"] = module._blank_project_entry("g-p-aaa", "test", 5, 2)
    module.save_project_registry(tmp_path, registry)

    entry = module.set_project_route(
        tmp_path, "g-p-aaa", "/dest/path", organ="ORGAN-III", repo="my-repo"
    )

    assert entry["route"]["destination"] == "/dest/path"
    assert entry["route"]["organ"] == "ORGAN-III"
    assert entry["extraction_state"] == "queued"

    # Verify it persisted
    loaded = module.load_project_registry(tmp_path)
    assert loaded["projects"]["g-p-aaa"]["extraction_state"] == "queued"


def test_set_project_route_raises_for_unknown_project(tmp_path: Path) -> None:
    module.save_project_registry(tmp_path, {"projects": {}, "project_count": 0})
    with pytest.raises(module.ChatGPTLocalSessionError, match="not in registry"):
        module.set_project_route(tmp_path, "g-p-missing", "/dest")


def test_render_project_status_empty() -> None:
    text = module.render_project_status({"projects": {}})
    assert "No projects" in text


def test_render_project_status_shows_breakdown() -> None:
    registry = {
        "projects": {
            "g-p-a": {"name": "alpha", "extraction_state": "discovered"},
            "g-p-b": {
                "name": "beta",
                "extraction_state": "delivered",
                "route": {"destination": "/out/beta"},
            },
            "g-p-c": {"name": "gamma", "extraction_state": "queued", "route": {}},
        },
    }
    text = module.render_project_status(registry)
    assert "Projects: 3" in text
    assert "discovered: 1" in text
    assert "delivered: 1" in text
    assert "queued: 1" in text
    assert "alpha" in text
    assert "beta" in text


def test_sync_chatgpt_projects_extracts_queued(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Set up registry with one queued project
    registry = {
        "generated_at": "old",
        "account_id": "",
        "project_count": 2,
        "projects": {
            "g-p-aaa": {
                "name": "queued-proj",
                "interactions": 5,
                "file_count": 2,
                "discovered_at": "old",
                "extraction_state": "queued",
                "extracted_at": None,
                "extraction_manifest": None,
                "route": {"destination": str(tmp_path / "output-aaa"), "organ": "", "repo": ""},
            },
            "g-p-bbb": {
                "name": "discovered-only",
                "interactions": 3,
                "file_count": 0,
                "discovered_at": "old",
                "extraction_state": "discovered",
                "extracted_at": None,
                "extraction_manifest": None,
                "route": None,
            },
        },
    }
    module.save_project_registry(tmp_path, registry)

    monkeypatch.setattr(
        module,
        "fetch_chatgpt_project",
        lambda pid, dest, cookie_jar=None: {
            "project_name": "queued-proj",
            "file_count": 5,
            "conversation_count": 3,
            "total_files": 5,
        },
    )

    result = module.sync_chatgpt_projects(tmp_path, batch_size=10)

    assert result["extracted_count"] == 1
    assert result["failed_count"] == 0
    # g-p-bbb has no route, so not a candidate
    assert result["candidates_total"] == 1

    loaded = module.load_project_registry(tmp_path)
    assert loaded["projects"]["g-p-aaa"]["extraction_state"] == "delivered"
    assert loaded["projects"]["g-p-aaa"]["extraction_manifest"]["files_extracted"] == 5


def test_delta_aware_fetch_reuses_cached_conversations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    jar = tmp_path / "test.binarycookies"
    jar.write_bytes(
        build_binary_cookie_jar([{"domain": ".chatgpt.com", "name": "x", "value": "y"}])
    )
    output_root = tmp_path / "output"

    # Cache a conversation
    module.cache_conversation_payload(
        output_root,
        "conv-1",
        {"conversation_id": "conv-1", "title": "Cached", "update_time": 100, "mapping": {}},
    )

    monkeypatch.setattr(
        module,
        "build_chatgpt_session",
        lambda cookie_jar: module.ChatGPTHttpSession(
            cookies=[],
            session_payload={"account": {"id": "acct-1"}, "user": {"email": "u@e.com"}},
            access_token="tok",  # allow-secret
            account_id="acct-1",
            device_id="dev-1",
        ),
    )

    fetch_calls: list[str] = []

    def fake_fetch_json(session: module.ChatGPTHttpSession, url: str) -> object:
        if "conversations?" in url:
            return {
                "total": 2,
                "items": [
                    {"id": "conv-1", "title": "Cached", "update_time": 100},
                    {"id": "conv-2", "title": "New", "update_time": 200},
                ],
            }
        if "conversation/conv-2" in url:
            fetch_calls.append("conv-2")
            return {
                "conversation_id": "conv-2",
                "title": "New",
                "update_time": 200,
                "mapping": {"n": {}},
            }
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(module, "fetch_json", fake_fetch_json)

    prior_state = {"conv-1": {"update_time": 100}}
    bundle = module.fetch_chatgpt_local_session_bundle(
        jar,
        limit=100,
        prior_state=prior_state,
        output_root=output_root,
    )

    assert bundle["reused_count"] == 1
    assert bundle["fetched_count"] == 1
    assert bundle["acquisition_report"]["reused_count"] == 1
    assert bundle["acquisition_report"]["fetched_count"] == 1
    assert fetch_calls == ["conv-2"]
    assert len(bundle["conversations"]) == 2
