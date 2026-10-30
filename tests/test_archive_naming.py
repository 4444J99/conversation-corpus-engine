from __future__ import annotations

import datetime
import hashlib
import json
import sys
from pathlib import Path

import pytest

from conversation_corpus_engine import archive_naming as naming
from conversation_corpus_engine.archive_ingest import PROVIDER_SPECS
from conversation_corpus_engine.archive_model import stable_uid
from conversation_corpus_engine.archive_naming import (
    KIND_TABLE,
    KINDS,
    NAMING_VERSION,
    InvalidName,
    NameCollision,
    NameLedger,
    conversation_name,
    conversation_uid,
    day_of,
    derive_name,
    hash_identity,
    parse_name,
    validate_name,
    verify_names,
)
from conversation_corpus_engine.cli import main
from conversation_corpus_engine.provider_catalog import PROVIDER_CONFIG

SPEC_KINDS = [
    *("archive", "conversation", "message", "thread", "family", "pair", "action", "question"),
    *("feed", "receipt", "corpus", "index", "pattern", "manifest", "report", "vault", "site"),
    *("graph", "database", "check", "fixture", "provider", "month", "topic", "collection"),
    *("shard", "part", "day", "bucket", "container"),
    *("home", "coverage", "guide"),
]

MESSAGE_HASH = "737b1cd5b47a52da"  # sha256 of "chatgpt-abc-123\x00node-1", first 16 hex
RECEIPT_HASH = "81a7594f4ff50691"  # sha256 of "fingerprint-one", first 16 hex
PATTERN_HASH = "28150a37a11e3d43"  # sha256 of "lineage-one", first 16 hex
ALIVE_HASH = "6574840ed2698ddf"  # sha256 of "knowledge-alive-fingerprint", first 16 hex
PART_HASH = "8cd98ff390001b88"  # sha256 of "conversation-chatgpt-abc-123", first 16 hex

# (expected name, derive_name arguments). Every vector in the build spec is here.
GOLDEN: list[tuple[str, dict]] = [
    ("archive-all", {"kind": "archive", "scope": "all"}),
    (
        "conversation-chatgpt-abc-123",
        {"kind": "conversation", "scope": "chatgpt", "identity": "abc-123"},
    ),
    (
        f"message-chatgpt-{MESSAGE_HASH}",
        {"kind": "message", "scope": "chatgpt", "identity": "chatgpt-abc-123\x00node-1"},
    ),
    ("feed-chatgpt-prompts", {"kind": "feed", "scope": "chatgpt", "subject": "prompts"}),
    (
        f"receipt-chatgpt-prompts-{RECEIPT_HASH}-20261017",
        {
            "kind": "receipt",
            "scope": "chatgpt",
            "subject": "prompts",
            "identity": "fingerprint-one",
            "date": "20261017",
        },
    ),
    ("corpus-chatgpt-archive", {"kind": "corpus", "scope": "chatgpt", "subject": "archive"}),
    ("index-all-patterns", {"kind": "index", "scope": "all", "subject": "patterns"}),
    ("database-all-search", {"kind": "database", "scope": "all", "subject": "search"}),
    ("vault-all", {"kind": "vault", "scope": "all"}),
    ("site-all", {"kind": "site", "scope": "all"}),
    ("graph-all", {"kind": "graph", "scope": "all"}),
    ("manifest-all", {"kind": "manifest", "scope": "all"}),
    ("manifest-all-vault", {"kind": "manifest", "scope": "all", "subject": "vault"}),
    ("provider-chatgpt", {"kind": "provider", "scope": "chatgpt"}),
    ("month-all-202506", {"kind": "month", "scope": "all", "subject": "202506"}),
    ("topic-all-kubernetes", {"kind": "topic", "scope": "all", "subject": "kubernetes"}),
    (
        "collection-claude-notes",
        {"kind": "collection", "scope": "claude", "subject": "notes"},
    ),
    ("home-all", {"kind": "home", "scope": "all"}),
    ("coverage-all", {"kind": "coverage", "scope": "all"}),
    ("guide-all", {"kind": "guide", "scope": "all"}),
    ("pattern-all-" + PATTERN_HASH, {"kind": "pattern", "scope": "all", "identity": "lineage-one"}),
    ("report-all-patterns", {"kind": "report", "scope": "all", "subject": "patterns"}),
    # Kinds the spec names without a literal example: identity is a hash over the owning
    # conversation uid plus a NUL-joined label, as the corpus projector will mint them.
    (
        "thread-chatgpt-619671c2e3e5af06",
        {"kind": "thread", "scope": "chatgpt", "identity": "chatgpt-abc-123\x00thread"},
    ),
    (
        "family-chatgpt-ba224a4026a0db5e",
        {"kind": "family", "scope": "chatgpt", "identity": "chatgpt-abc-123\x00family"},
    ),
    (
        "pair-chatgpt-ece267f45a800ad9",
        {"kind": "pair", "scope": "chatgpt", "identity": "chatgpt-abc-123\x00pair\x001"},
    ),
    (
        "action-chatgpt-8acc8f466e8a7fd3",
        {"kind": "action", "scope": "chatgpt", "identity": "chatgpt-abc-123\x00action\x00digest"},
    ),
    (
        "question-chatgpt-e77b1275288669fa",
        {
            "kind": "question",
            "scope": "chatgpt",
            "identity": "chatgpt-abc-123\x00question\x00digest",
        },
    ),
    ("fixture-chatgpt-exports", {"kind": "fixture", "scope": "chatgpt", "subject": "exports"}),
    # The bounded layout (naming version 3): shards, split parts, spill folders, containers.
    ("shard-chatgpt-202506", {"kind": "shard", "scope": "chatgpt", "subject": "202506"}),
    ("shard-claude-202506-002", {"kind": "shard", "scope": "claude", "subject": "202506-002"}),
    ("shard-grok-undated", {"kind": "shard", "scope": "grok", "subject": "undated"}),
    (
        f"part-chatgpt-p002-{PART_HASH}",
        {
            "kind": "part",
            "scope": "chatgpt",
            "subject": "p002",
            "identity": "conversation-chatgpt-abc-123",
        },
    ),
    ("day-all-20250614", {"kind": "day", "scope": "all", "date": "20250614"}),
    ("bucket-all-b007", {"kind": "bucket", "scope": "all", "subject": "b007"}),
    ("container-all-authored", {"kind": "container", "scope": "all", "subject": "authored"}),
    # The two names Limen mints, which it checks by shape; they are valid under the grammar too.
    ("check-knowledge-alive", {"kind": "check", "subject": "knowledge-alive"}),
    (
        f"receipt-knowledge-alive-{ALIVE_HASH}-20261017",
        {
            "kind": "receipt",
            "subject": "knowledge-alive",
            "identity": "knowledge-alive-fingerprint",
            "date": "20261017",
        },
    ),
]


def _cce(monkeypatch, capsys, *argv: str) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["cce", *argv])
    code = 0
    try:
        main()
    except SystemExit as exit_:
        code = int(exit_.code or 0)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- golden vectors ---------------------------------------------------------------------


@pytest.mark.parametrize(("expected", "kwargs"), GOLDEN, ids=[item[0][:40] for item in GOLDEN])
def test_golden_vector_is_derived_parsed_and_valid(expected: str, kwargs: dict) -> None:
    assert derive_name(**kwargs) == expected
    assert validate_name(expected) == []
    parts = parse_name(expected)
    assert parts.render() == expected
    assert parts.kind == kwargs["kind"]
    assert parts.scope == kwargs.get("scope")
    assert parts.subject == kwargs.get("subject")
    assert parts.date == kwargs.get("date")


def test_golden_hash_literals_match_their_inputs() -> None:
    assert hash_identity("chatgpt-abc-123\x00node-1") == MESSAGE_HASH
    assert hash_identity("fingerprint-one") == RECEIPT_HASH
    assert hash_identity("lineage-one") == PATTERN_HASH
    assert hash_identity("knowledge-alive-fingerprint") == ALIVE_HASH
    assert hash_identity("conversation-chatgpt-abc-123") == PART_HASH
    expected = hashlib.sha256(b"fingerprint-one").hexdigest()[:16]
    assert hash_identity("fingerprint-one") == expected


def test_golden_vectors_cover_every_kind() -> None:
    assert {parse_name(name).kind for name, _ in GOLDEN} == set(KINDS)


def test_derive_is_pure_and_repeatable() -> None:
    first = derive_name("feed", scope="grok", subject="prompts")
    assert all(derive_name("feed", scope="grok", subject="prompts") == first for _ in range(5))


# --- the closed kind table --------------------------------------------------------------


def test_kind_table_is_the_closed_spec_list_in_order() -> None:
    assert list(KINDS) == SPEC_KINDS
    assert NAMING_VERSION == 4


def test_every_kind_is_one_lowercase_word_and_not_a_scope() -> None:
    for kind in KINDS:
        assert kind.isalpha() and kind == kind.lower()
        assert kind not in naming.SCOPES


def test_table_makes_the_grammar_unambiguous() -> None:
    for kind, rule in KIND_TABLE.items():
        assert rule.date != naming.OPTIONAL, kind
        assert rule.identity != naming.OPTIONAL, kind
        assert rule.scope != naming.FORBIDDEN, kind
        if rule.has_subject_and_identity:
            assert rule.identity_form == naming.IDENTITY_HASH, kind


def test_unknown_or_missing_kinds_are_refused() -> None:
    with pytest.raises(InvalidName):
        derive_name("notakind", scope="all")
    with pytest.raises(InvalidName):
        derive_name(None)  # type: ignore[arg-type]
    assert validate_name("notakind-all")
    assert validate_name("kb-all")  # the old abbreviation is not a kind
    assert validate_name("_archive")


def test_required_and_forbidden_segments_are_enforced_per_kind() -> None:
    with pytest.raises(InvalidName, match="scope is required"):
        derive_name("vault")
    with pytest.raises(InvalidName, match="subject is required"):
        derive_name("feed", scope="chatgpt")
    with pytest.raises(InvalidName, match="identity is required"):
        derive_name("conversation", scope="chatgpt")
    with pytest.raises(InvalidName, match="date is required"):
        derive_name("receipt", scope="chatgpt", subject="prompts", identity="x")
    with pytest.raises(InvalidName, match="subject is not allowed"):
        derive_name("conversation", scope="chatgpt", subject="x", identity="abc")
    with pytest.raises(InvalidName, match="identity is not allowed"):
        derive_name("feed", scope="chatgpt", subject="prompts", identity="abc")
    with pytest.raises(InvalidName, match="date is not allowed"):
        derive_name("vault", scope="all", date="20261017")
    with pytest.raises(InvalidName, match="date is not allowed"):
        derive_name("month", scope="all", subject="202506", date="20250601")


def test_scope_values_follow_the_kind() -> None:
    with pytest.raises(InvalidName, match="not 'all'"):
        derive_name("conversation", scope="all", identity="abc")
    with pytest.raises(InvalidName, match="must be 'all'"):
        derive_name("vault", scope="chatgpt")
    with pytest.raises(InvalidName, match="not a known provider"):
        derive_name("feed", scope="Chatgpt", subject="prompts")
    with pytest.raises(InvalidName, match="not a known provider"):
        derive_name("feed", scope="unknown", subject="prompts")


def test_scope_list_covers_the_provider_catalog_and_the_archive_readers() -> None:
    assert set(PROVIDER_CONFIG) <= set(naming.PROVIDER_SCOPES)
    assert set(PROVIDER_SPECS) <= set(naming.PROVIDER_SCOPES)
    assert naming.SCOPES[-1] == "all"
    assert len(set(naming.SCOPES)) == len(naming.SCOPES)


# --- subject, identity, date rules ------------------------------------------------------


def test_subject_is_validated_never_transformed() -> None:
    assert derive_name("feed", scope="claude", subject="a-b-c") == "feed-claude-a-b-c"
    for bad in ("Prompts", "my prompts", "a--b", "-a", "a-", "a_b", "über", "", "A Title: Here"):
        with pytest.raises(InvalidName):
            derive_name("feed", scope="claude", subject=bad)


def test_subject_is_limited_to_thirty_two_characters() -> None:
    thirty_two = "a" * 32
    assert derive_name("feed", scope="claude", subject=thirty_two).endswith(thirty_two)
    with pytest.raises(InvalidName, match="longer than 32"):
        derive_name("feed", scope="claude", subject="a" * 33)


def test_month_and_topic_subjects_have_their_own_shape() -> None:
    assert derive_name("month", scope="all", subject="202512") == "month-all-202512"
    for bad in ("202513", "202500", "20251", "2025061", "jan"):
        with pytest.raises(InvalidName):
            derive_name("month", scope="all", subject=bad)
    with pytest.raises(InvalidName, match="at most 1 word"):
        derive_name("topic", scope="all", subject="two-words")
    assert validate_name("month-all-202513")


def test_without_a_scope_a_subject_may_not_start_with_a_scope_word() -> None:
    with pytest.raises(InvalidName, match="may not start with the scope word"):
        derive_name("check", subject="claude-code")
    with pytest.raises(InvalidName, match="may not start with the scope word"):
        derive_name("check", subject="all-things")
    assert derive_name("check", scope="claude", subject="code") == "check-claude-code"


def test_plain_vendor_identities_are_used_as_is() -> None:
    assert derive_name("message", scope="claude", identity="msg-01") == "message-claude-msg-01"
    uuid_like = "0f8fad5b-d9cb-469f-a165-9d0d2bba6b21"
    assert (
        derive_name("message", scope="chatgpt", identity=uuid_like)
        == f"message-chatgpt-{uuid_like}"
    )
    sixty_four = "a" * 64
    assert derive_name("message", scope="grok", identity=sixty_four).endswith(sixty_four)


def test_other_identities_are_hashed_without_normalization() -> None:
    for raw in ("Has Capitals", "a" * 65, "a--b", "-edge", "x/y", "", "naïve", "t\x00u"):
        name = derive_name("thread", scope="claude", identity=raw)
        assert name == f"thread-claude-{hash_identity(raw)}"
    assert hash_identity("Abc") != hash_identity("abc")
    assert hash_identity(" abc") != hash_identity("abc")


def test_hash_kinds_always_hash_even_a_plain_identity() -> None:
    assert derive_name("pattern", scope="all", identity="abc") == (
        f"pattern-all-{hash_identity('abc')}"
    )
    name = derive_name("pattern", scope="all", identity=PATTERN_HASH)
    assert name == f"pattern-all-{hash_identity(PATTERN_HASH)}"
    assert name != f"pattern-all-{PATTERN_HASH}"


def test_conversation_identity_is_carried_exactly_and_never_hashed() -> None:
    name = derive_name("conversation", scope="gemini", identity="session-2025-06-0123456789")
    assert name == "conversation-gemini-session-2025-06-0123456789"
    with pytest.raises(InvalidName, match="identity"):
        derive_name("conversation", scope="gemini", identity="Session 2025/06")
    with pytest.raises(InvalidName, match="identity"):
        derive_name("conversation", scope="gemini", identity="a--b")


def test_dates_are_real_calendar_days_from_the_data() -> None:
    kwargs = {"kind": "receipt", "scope": "claude", "subject": "prompts", "identity": "f"}
    assert derive_name(**kwargs, date="20240229").endswith("-20240229")
    assert derive_name(**kwargs, date=datetime.date(2025, 6, 1)).endswith("-20250601")
    assert derive_name(**kwargs, date=datetime.datetime(2025, 6, 1, 23, 59)).endswith("-20250601")
    for bad in ("20230229", "20261301", "20260001", "2026-10-17", "2026101", "202610170", "x"):
        with pytest.raises(InvalidName):
            derive_name(**kwargs, date=bad)
    with pytest.raises(InvalidName):
        derive_name(**kwargs, date=20261017)  # type: ignore[arg-type]
    with pytest.raises(InvalidName, match="date is required"):
        derive_name(**kwargs)  # there is no clock fallback


def test_day_of_reads_the_day_from_a_timestamp() -> None:
    assert day_of("2025-06-01T10:00:00Z") == "20250601"
    assert day_of("2025-06-01") == "20250601"
    for bad in ("", "not a date", "2025-13-01T00:00:00Z", "20250601"):
        with pytest.raises(InvalidName):
            day_of(bad)


# --- length and shape -------------------------------------------------------------------


def test_names_over_eighty_characters_are_an_error_never_truncated() -> None:
    longest_subject = "a" * 32
    ok = derive_name(
        "receipt", scope="notebooklm", subject=longest_subject, identity="x", date="20261017"
    )
    assert len(ok) <= 80
    long_tail = "a" * 64
    with pytest.raises(InvalidName, match="limit is 80"):
        derive_name("conversation", scope="chatgpt", identity=long_tail)
    fits = "a" * (80 - len("conversation-chatgpt-"))
    assert len(derive_name("conversation", scope="chatgpt", identity=fits)) == 80
    assert validate_name("feed-claude-" + "a" * 70)


def test_shape_rules_reject_case_underscores_spaces_and_edge_hyphens() -> None:
    for bad in (
        "Vault-all",
        "vault_all",
        "vault all",
        "-vault-all",
        "vault-all-",
        "vault--all",
        "",
    ):
        assert validate_name(bad), bad
    with pytest.raises(InvalidName):
        parse_name("vault--all")
    assert validate_name(None)  # type: ignore[arg-type]


def test_parse_reports_every_problem_it_finds() -> None:
    problems = validate_name("receipt-claude-prompts-nothex")
    assert any("eight digit date" in item for item in problems)
    assert any("hex" in item for item in problems)


def test_derive_and_parse_round_trip_for_every_kind_and_scope() -> None:
    for kind, rule in KIND_TABLE.items():
        scopes: list[str | None] = [None] if rule.scope == naming.OPTIONAL else []
        scopes += list(naming.SCOPES)
        for scope in scopes:
            kwargs: dict = {"kind": kind, "scope": scope}
            if rule.subject != naming.FORBIDDEN:
                kwargs["subject"] = "202506" if rule.subject_month else "term"
            if rule.identity != naming.FORBIDDEN:
                tail = rule.identity_form == naming.IDENTITY_TAIL
                kwargs["identity"] = "some-identity" if tail else "some identity"
            if rule.date == naming.REQUIRED:
                kwargs["date"] = "20260102"
            try:
                name = derive_name(**kwargs)
            except InvalidName as exc:
                # only a scope the kind rejects may fail
                assert "scope" in str(exc), (kind, scope, exc)
                continue
            parts = parse_name(name)
            assert parts.render() == name
            assert (parts.kind, parts.scope, parts.date) == (kind, scope, kwargs.get("date"))
            assert validate_name(name) == []


def test_parse_returns_the_hashed_segment_not_the_identity_that_was_hashed() -> None:
    """The docstring's round-trip claim holds over resolved segments only."""
    fields = {"scope": "chatgpt", "subject": "prompts", "date": "20261017"}
    name = derive_name("receipt", identity="a fingerprint", **fields)
    parts = parse_name(name)
    assert parts.identity == hash_identity("a fingerprint") != "a fingerprint"
    assert parts.render() == name  # the parse is faithful to the name it was given
    # the original identity is not recoverable, and the segment hashes again if fed back
    again = derive_name("receipt", identity=parts.identity, **fields)
    assert again != name
    assert parse_name(again).identity == hash_identity(parts.identity)
    # where nothing is hashed (a plain identity on a plain kind) the segment is the input
    assert parse_name(derive_name("thread", scope="claude", identity="abc-1")).identity == "abc-1"


def test_a_subject_made_of_hex_does_not_confuse_the_receipt_parse() -> None:
    name = derive_name(
        "receipt", scope="claude", subject="deadbeefdeadbeef", identity="f", date="20260102"
    )
    parts = parse_name(name)
    assert parts.subject == "deadbeefdeadbeef"
    assert parts.identity == hash_identity("f")


# --- stable_uid replay ------------------------------------------------------------------

PLAIN_IDS = [
    "abc-123",
    "a",
    "0f8fad5b-d9cb-469f-a165-9d0d2bba6b21",
    "12345678",
    "node-9",
    "x" * 40,
]
NON_PLAIN_IDS = [
    "Session 2025/06",
    "ABC",
    "",
    "Ünïcode ∑ title",
    "with_underscore",
    "x" * 70,
    "trailing space ",
    "UPPER-lower-123",
]


@pytest.mark.parametrize("provider", sorted(PROVIDER_SPECS))
def test_conversation_names_reproduce_stable_uid_byte_for_byte(provider: str) -> None:
    for source_id in PLAIN_IDS + NON_PLAIN_IDS:
        uid = stable_uid(provider, source_id)
        name = conversation_name(provider, uid)
        assert name == f"conversation-{uid}"
        assert conversation_uid(name) == uid
        parts = parse_name(name)
        assert (parts.kind, parts.scope) == ("conversation", provider)
        assert f"{parts.scope}-{parts.identity}" == uid
        assert validate_name(name) == []


def test_replay_covers_both_branches_of_stable_uid() -> None:
    plain = stable_uid("chatgpt", "abc-123")
    odd = stable_uid("gemini", "Session 2025/06")
    assert plain == "chatgpt-abc-123"
    assert odd.startswith("gemini-session-2025-06-")
    assert conversation_name("gemini", odd) == f"conversation-{odd}"


def test_a_uid_that_cannot_be_a_name_is_an_error_not_a_rewrite() -> None:
    # stable_uid keeps these plain ids exactly, but they cannot satisfy the name shape.
    for source_id in ("-leading", "trailing-", "dou--ble"):
        uid = stable_uid("chatgpt", source_id)
        with pytest.raises(InvalidName):
            conversation_name("chatgpt", uid)
    longest_plain = stable_uid("chatgpt", "x" * 64)
    with pytest.raises(InvalidName, match="limit is 80"):
        conversation_name("chatgpt", longest_plain)
    with pytest.raises(InvalidName, match="does not start with"):
        conversation_name("claude", stable_uid("chatgpt", "abc"))


def test_conversation_uid_rejects_other_kinds() -> None:
    with pytest.raises(InvalidName):
        conversation_uid("vault-all")
    with pytest.raises(InvalidName):
        conversation_uid("not a name")


# --- ledger -----------------------------------------------------------------------------


def test_ledger_treats_the_same_identity_as_a_harmless_rerun() -> None:
    ledger = NameLedger()
    assert ledger.register("feed-claude-prompts", "claude/prompts") is True
    assert ledger.register("feed-claude-prompts", "claude/prompts") is False
    assert len(ledger) == 1 and "feed-claude-prompts" in ledger
    assert ledger.identity_of("feed-claude-prompts") == "claude/prompts"
    assert ledger.names() == ["feed-claude-prompts"]


def test_ledger_refuses_a_different_identity_for_a_taken_name() -> None:
    ledger = NameLedger()
    ledger.register("thread-claude-abc", "one")
    with pytest.raises(NameCollision) as caught:
        ledger.register("thread-claude-abc", "two")
    assert caught.value.name == "thread-claude-abc"
    assert ledger.identity_of("thread-claude-abc") == "one"


def test_ledger_folds_case_so_a_case_insensitive_clash_is_caught() -> None:
    ledger = NameLedger()
    ledger.register("home-all", "one")
    with pytest.raises(NameCollision):
        ledger.register("HOME-ALL", "two")
    assert ledger.register("Home-All", "one") is False
    assert "HOME-all" in ledger


def test_ledger_rejects_bad_arguments() -> None:
    ledger = NameLedger()
    with pytest.raises(InvalidName):
        ledger.register("", "x")
    with pytest.raises(InvalidName):
        ledger.register("home-all", None)  # type: ignore[arg-type]
    assert ledger.identity_of("missing") is None
    assert 3 not in ledger


# --- payload and commands ---------------------------------------------------------------


def test_names_payload_lists_every_kind_with_its_rule() -> None:
    payload = naming.names_payload()
    assert payload["naming_version"] == NAMING_VERSION
    assert [item["kind"] for item in payload["kinds"]] == SPEC_KINDS
    assert payload["scopes"] == list(naming.SCOPES)
    assert payload["max_name_length"] == 80 and payload["max_subject_length"] == 32
    receipt = next(item for item in payload["kinds"] if item["kind"] == "receipt")
    assert (receipt["scope"], receipt["date"], receipt["identity_form"]) == (
        "optional",
        "required",
        "hash",
    )
    json.dumps(payload)  # serializable
    assert (
        "kind" in naming.render_names_text()
        and f"version {NAMING_VERSION}" in naming.render_names_text()
    )


def test_names_command_prints_the_contract_as_json(monkeypatch, capsys) -> None:
    code, out, _ = _cce(monkeypatch, capsys, "archive", "names", "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["naming_version"] == NAMING_VERSION
    assert [item["kind"] for item in payload["kinds"]] == SPEC_KINDS
    assert "chatgpt" in payload["scopes"] and "all" in payload["scopes"]


def test_names_command_needs_no_store_and_prints_text_without_json(monkeypatch, capsys) -> None:
    monkeypatch.delenv("CCE_CORPUS_STORE_ROOT", raising=False)
    code, out, err = _cce(monkeypatch, capsys, "archive", "names")
    assert code == 0 and "conversation" in out and "custody" not in err


def _tree(root: Path, spec: dict) -> None:
    for name, value in spec.items():
        target = root / name
        if isinstance(value, dict):
            target.mkdir()
            _tree(target, value)
        else:
            target.write_text(value, encoding="utf-8")


def test_verify_names_accepts_a_conforming_tree(tmp_path: Path) -> None:
    _tree(
        tmp_path,
        {
            "manifest-all.json": "{}",
            "database-all-search.sqlite": "",
            "database-all-search.sqlite-wal": "",
            "feed-chatgpt-prompts.jsonl": "",
            f"receipt-chatgpt-prompts-{RECEIPT_HASH}-20261017.json": "{}",
            "vault-all": {
                "conversations": {"conversation-chatgpt-abc-123.md": ""},
                "hubs": {"provider-chatgpt.md": "", "month-all-202506.md": ""},
                "meta": {},
                "home-all.md": "",
                ".home-all.md.tmp": "",
            },
            "site-all": {"index-all-patterns.html": ""},
        },
    )
    report = verify_names(tmp_path)
    assert report["problems"] == [] and report["ok"] is True
    assert report["checked"] == 15
    assert report["naming_version"] == NAMING_VERSION


def test_verify_names_names_every_offender_with_its_path(tmp_path: Path) -> None:
    _tree(
        tmp_path,
        {
            "_archive": {"manifest.json": "{}"},
            "vault-all": {"Home-all.md": "", "stray note.md": "", "hubs": {"Month-all.md": ""}},
            "vault-all.tmp.d": {},
        },
    )
    report = verify_names(tmp_path)
    assert report["ok"] is False
    joined = "\n".join(report["problems"])
    assert "_archive:" in joined and "_archive/manifest.json:" in joined
    assert "vault-all/Home-all.md: not lowercase" in joined
    assert "vault-all/stray note.md:" in joined
    assert "vault-all/hubs/Month-all.md: not lowercase" in joined
    assert "vault-all.tmp.d" not in joined  # a valid stem with an odd extension is fine


def test_verify_names_is_deterministic_and_does_not_follow_links(tmp_path: Path) -> None:
    (tmp_path / "vault-all").mkdir()
    (tmp_path / "bad name").write_text("", encoding="utf-8")
    (tmp_path / "loop").symlink_to(tmp_path)
    first = verify_names(tmp_path)
    assert verify_names(tmp_path) == first
    assert any(item.startswith("bad name:") for item in first["problems"])
    assert any(item.startswith("loop:") for item in first["problems"])


def test_verify_names_accepts_a_custom_structural_list(tmp_path: Path) -> None:
    _tree(tmp_path, {"normalized": {}})
    assert verify_names(tmp_path)["ok"] is False
    assert verify_names(tmp_path, structural={"normalized"})["ok"] is True


def test_verify_names_rejects_a_missing_root(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        verify_names(tmp_path / "missing")


def test_verify_names_command_exit_codes(tmp_path: Path, monkeypatch, capsys) -> None:
    good = tmp_path / "archive-all"
    good.mkdir()
    (good / "manifest-all.json").write_text("{}", encoding="utf-8")
    code, out, _ = _cce(monkeypatch, capsys, "archive", "verify-names", "--root", str(good))
    assert code == 0 and json.loads(out)["ok"] is True

    (good / "Kb").mkdir()
    code, out, _ = _cce(monkeypatch, capsys, "archive", "verify-names", "--root", str(good))
    payload = json.loads(out)
    assert code == 1 and payload["ok"] is False and payload["problems"]

    code, out, err = _cce(
        monkeypatch, capsys, "archive", "verify-names", "--root", str(tmp_path / "nowhere")
    )
    assert code == 2 and out == "" and "not found" in err
