"""Web captures: fetched pages become third-party library records with their source.

Every URL, title and sentence here is invented (example.com style); no real page is involved.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conversation_corpus_engine import archive_read_library as bridge
from conversation_corpus_engine import web_capture
from conversation_corpus_engine.archive_author import HUMAN, MACHINE
from conversation_corpus_engine.archive_ingest import ingest
from conversation_corpus_engine.archive_model import (
    Conversation,
    Message,
    SourceTracker,
    finalize_conversation,
)
from conversation_corpus_engine.attribution_rules import (
    OWNER,
    THIRD_PARTY,
    attribute_message,
    record_info,
)
from conversation_corpus_engine.cli import build_parser
from conversation_corpus_engine.corpus_store import register_corpus_store
from conversation_corpus_engine.library_build import (
    build_library,
    current_generation,
    publish_library,
)
from conversation_corpus_engine.library_commands import handle_library
from conversation_corpus_engine.library_resume import prepare_resume
from conversation_corpus_engine.library_store import Library

PAGES = [
    (
        "https://example.com/articles/orchard",
        "Orchard notes",
        "The orchard lattice irrigation plan waters each row twice a week.",
        "2026-03-01T08:00:00Z",
    ),
    (
        "https://example.org/guides/kettle",
        "Kettle guide",
        "A copper kettle thermodynamics primer for the curious reader.",
        "2026-03-02T09:30:00Z",
    ),
    (
        "https://example.net/reports/glacier",
        "Glacier report",
        "Glacier survey methodology is described in the second appendix.",
        "2026-03-03T11:45:00Z",
    ),
]


def make_row(
    url: str,
    text: str,
    captured_at: str,
    *,
    title: str | None = None,
    final_url: str | None = None,
    key: str | None = None,
) -> dict[str, Any]:
    key = key or url.lower()
    row: dict[str, Any] = {
        "schema": "capture-shard.v1",
        "id": web_capture.page_id(key),
        "key": key,
        "requested_url": url,
        "final_url": final_url or url,
        "status": "captured",
        "http_status": 200,
        "content_digest": web_capture.text_digest(text),
        "captured_at": captured_at,
        "extractor": {"name": "synthetic-extractor", "version": "1.0"},
        "text": text,
        "failure_class": None,
        "license_note": "synthetic test page",
    }
    if title is not None:
        row["title"] = title
    return row


def failed_row(url: str, failure: str, captured_at: str) -> dict[str, Any]:
    row = make_row(url, "placeholder", captured_at)
    row.update(
        status="failed",
        http_status=None,
        content_digest=None,
        text="",
        failure_class=failure,
        final_url=None,
    )
    return row


def write_shard(path: Path, rows: list[Any], *, raw: list[str] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(row) for row in rows] + (raw or [])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def page_rows() -> list[dict[str, Any]]:
    return [make_row(url, text, at, title=title) for url, title, text, at in PAGES]


def setup(tmp_path: Path, name: str = "work") -> tuple[Path, Path, Path]:
    base = tmp_path / name
    project, store, shards = base / "project", base / "store", base / "shards"
    for path in (project, store, shards):
        path.mkdir(parents=True)
    register_corpus_store(project, store)
    return project, store, shards


def manifest_for(shards: Path, **extra: Any) -> dict[str, Any]:
    return {"schema": "cce.library-sources.v1", "web_captures": [str(shards)], **extra}


def records(generation: Path) -> dict[str, dict[str, Any]]:
    with Library(generation / "library.sqlite") as library:
        return {row["uid"]: row for row in library.rows()}


# --- the reader ---------------------------------------------------------------------------------


def test_three_pages_build_search_and_show_their_source(tmp_path, capsys):
    project, store, shards = setup(tmp_path)
    write_shard(shards / "captures-001.jsonl", page_rows())
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(manifest_for(shards)), encoding="utf-8")
    args = build_parser().parse_args(
        [
            "library",
            "build",
            "--manifest",
            str(manifest),
            "--project-root",
            str(project),
            "--store-root",
            str(store),
            "--publish",
        ]
    )
    assert handle_library(args) == 0
    built = json.loads(capsys.readouterr().out)
    assert built["status"]["records"] == 3
    assert built["status"]["by_provider"] == {"webcapture": 3}
    assert built["status"]["by_wing"] == {"ai": 3}
    assert built["findings_count"] == 0
    assert built["coverage_complete"] is True
    assert built["publication"]["changed"] is True
    for url, title, text, captured in PAGES:
        phrase = " ".join(text.split()[1:5])
        search = build_parser().parse_args(
            ["library", "search", f'"{phrase}"', "--store-root", str(store)]
        )
        assert handle_library(search) == 0
        found = json.loads(capsys.readouterr().out)
        assert found["total"] == 1, phrase
        hit = found["hits"][0]
        assert hit["provider"] == "webcapture" and hit["kind"] == "page"
        assert hit["title"] == title
        assert hit["source_ref"] == url
        assert hit["capture"]["url"] == url
        assert hit["capture"]["captured_at"] == captured
        assert hit["capture"]["content_digest"] == web_capture.text_digest(text)


def test_a_search_hit_never_reads_the_stored_page(tmp_path, monkeypatch):
    project, store, shards = setup(tmp_path)
    write_shard(shards / "captures-001.jsonl", page_rows())
    report = build_library(manifest_for(shards), project_root=project, store_root=store)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a search read a whole stored payload")

    monkeypatch.setattr(Library, "record", forbidden)
    with Library(Path(report["generation"]) / "library.sqlite") as library:
        found = library.search("orchard lattice", max_seconds=5)
    (hit,) = found["hits"]
    assert hit["capture"] == {
        "url": PAGES[0][0],
        "final_url": PAGES[0][0],
        "captured_at": PAGES[0][3],
        "content_digest": web_capture.text_digest(PAGES[0][2]),
        "http_status": 200,
    }


def test_a_record_keeps_its_provenance_and_no_host_path(tmp_path):
    project, store, shards = setup(tmp_path)
    rows = page_rows()
    rows[0]["final_url"] = "https://example.com/articles/orchard?page=1"
    write_shard(shards / "captures-001.jsonl", rows)
    report = build_library(manifest_for(shards), project_root=project, store_root=store)
    generation = Path(report["generation"])
    (uid,) = [
        uid for uid in records(generation) if records(generation)[uid]["title"] == "Orchard notes"
    ]
    with Library(generation / "library.sqlite") as library:
        payload = library.record(uid)
    provenance = payload["provenance"]
    assert provenance["source"] == "web_capture" and provenance["source_origin"] == "web_capture"
    assert provenance["requested_url"] == "https://example.com/articles/orchard"
    assert provenance["final_url"] == "https://example.com/articles/orchard?page=1"
    assert provenance["source_path"] == provenance["final_url"]
    assert provenance["captured_at"] == "2026-03-01T08:00:00Z"
    assert provenance["content_digest"] == web_capture.text_digest(PAGES[0][2])
    assert (
        provenance["http_status"] == 200
        and provenance["extractor"]["name"] == "synthetic-extractor"
    )
    assert provenance["source_file"] == "captures-001.jsonl"
    assert str(shards) not in json.dumps(payload) and str(tmp_path) not in json.dumps(payload)
    assert payload["extra"]["wing"] == "ai" and payload["extra"]["lane"] == "web"
    assert payload["created"] == payload["updated"] == "2026-03-01T08:00:00Z"
    (message,) = payload["messages"]
    assert (message["role"], message["channel"], message["typed_chars"]) == ("tool", "tool", 0)
    assert uid == "webcapture-pg-" + web_capture.page_id(PAGES[0][0].lower())[len("pg_") :]


def test_rebuilding_the_same_input_changes_nothing(tmp_path):
    project, store, shards = setup(tmp_path)
    write_shard(shards / "captures-001.jsonl", page_rows())
    first = build_library(manifest_for(shards), project_root=project, store_root=store)
    assert publish_library(Path(first["generation"]), project_root=project, store_root=store)[
        "changed"
    ]
    second = build_library(manifest_for(shards), project_root=project, store_root=store)
    assert second["changed_records"] == 0
    assert second["record_fingerprint"] == first["record_fingerprint"]
    published = publish_library(Path(second["generation"]), project_root=project, store_root=store)
    assert published["changed"] is False
    assert current_generation(store) == Path(first["generation"])
    # A separate store given the same bytes builds the same records.
    project2, store2, shards2 = setup(tmp_path, "other")
    write_shard(shards2 / "captures-001.jsonl", page_rows())
    other = build_library(manifest_for(shards2), project_root=project2, store_root=store2)
    assert other["record_fingerprint"] == first["record_fingerprint"]


def test_changed_text_for_the_same_url_is_a_new_revision(tmp_path):
    project, store, shards = setup(tmp_path)
    write_shard(shards / "captures-001.jsonl", page_rows())
    first = build_library(manifest_for(shards), project_root=project, store_root=store)
    publish_library(Path(first["generation"]), project_root=project, store_root=store)
    url, title, _, _ = PAGES[0]
    newer = "The orchard lattice irrigation plan now waters each row three times a week."
    write_shard(
        shards / "captures-002.jsonl",
        [make_row(url, newer, "2026-04-01T08:00:00Z", title=title)],
    )
    second = build_library(manifest_for(shards), project_root=project, store_root=store)
    assert second["changed_records"] == 1
    generation = Path(second["generation"])
    uid = "webcapture-pg-" + web_capture.page_id(url.lower())[len("pg_") :]
    with Library(generation / "library.sqlite") as library:
        current = library.record(uid)
        assert current["messages"][0]["text"] == newer
        assert [item["version"] for item in library.history(uid)] == ["1.0.0", "1.1.0"]
        assert library.record(uid, version="1.0.0")["messages"][0]["text"] == PAGES[0][2]
        assert library.search("three times a week")["total"] == 1
    assert records(generation)[uid]["current_version"] == "1.1.0"


def test_duplicate_urls_keep_one_winner_whatever_the_shard_order(tmp_path):
    url, title, text, _ = PAGES[0]
    older = make_row(url, text, "2026-03-01T08:00:00Z", title=title)
    newer = make_row(url, text + " It was revised.", "2026-03-09T08:00:00Z", title=title)
    prints = []
    for index, order in enumerate((("a", "b"), ("b", "a"))):
        project, store, shards = setup(tmp_path, f"order{index}")
        other = make_row(PAGES[1][0], PAGES[1][2], PAGES[1][3], title=PAGES[1][1])
        write_shard(shards / f"{order[0]}.jsonl", [older, other])
        write_shard(shards / f"{order[1]}.jsonl", [newer])
        report = build_library(manifest_for(shards), project_root=project, store_root=store)
        with Library(Path(report["generation"]) / "library.sqlite") as library:
            winner = library.record("webcapture-pg-" + web_capture.page_id(url.lower())[3:])
        # The record names the shard it came from, so the whole record differs by file name; the
        # winner's text, time and digest do not.
        prints.append(
            (
                winner["messages"][0]["text"],
                winner["provenance"]["captured_at"],
                winner["provenance"]["content_digest"],
            )
        )
        counts = report["sources"][0]["report"]["counts"]
        assert (counts["records"], counts["duplicates"], counts["refused"]) == (2, 1, 0)
        assert report["status"]["records"] == 2
        with Library(Path(report["generation"]) / "library.sqlite") as library:
            assert library.search("It was revised")["total"] == 1
    assert prints[0] == prints[1]


def test_a_capture_beats_a_failure_for_the_same_page(tmp_path):
    project, store, shards = setup(tmp_path)
    url, title, text, _ = PAGES[0]
    write_shard(
        shards / "one.jsonl",
        [make_row(url, text, "2026-03-01T08:00:00Z", title=title)],
    )
    write_shard(shards / "two.jsonl", [failed_row(url, "timeout", "2026-03-20T08:00:00Z")])
    report = build_library(manifest_for(shards), project_root=project, store_root=store)
    assert report["status"]["by_body_status"] == {"full": 1}


def test_a_failed_fetch_is_a_visible_record_without_a_body(tmp_path):
    project, store, shards = setup(tmp_path)
    write_shard(
        shards / "captures-001.jsonl",
        [*page_rows(), failed_row("https://example.com/gone", "dead_link", "2026-03-05T00:00:00Z")],
    )
    report = build_library(manifest_for(shards), project_root=project, store_root=store)
    status = report["status"]
    assert status["by_body_status"] == {"full": 3, "metadata_only": 1}
    assert status["body_reasons"] == {"metadata_only": {"dead_link": 1}}
    assert status["unexplained_non_full"] == 0
    assert report["coverage_complete"] is False
    counts = report["sources"][0]["report"]["counts"]
    assert (counts["captured"], counts["failed"], counts["records"]) == (3, 1, 4)


def test_a_failed_build_with_web_captures_can_be_qualified_for_resume(tmp_path):
    project, store, shards = setup(tmp_path)
    write_shard(shards / "captures-001.jsonl", page_rows())
    report = build_library(manifest_for(shards), project_root=project, store_root=store)
    generation = Path(report["generation"])
    assert [source["kind"] for source in report["sources"]] == ["web_captures", "documents"]
    seed = {**report, "complete": False, "phase": "ingested"}
    state = {"state": "failed", "error": "stopped while indexing", "report": seed}
    (generation / "build-state.json").write_text(json.dumps(state), encoding="utf-8")
    qualified = prepare_resume(generation, store, None, 1024**3)
    assert qualified["receipt"]["accepted_observations"] == 3
    seed["accepted_observations"] = 4
    (generation / "build-state.json").write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError, match="accepted observations differ"):
        prepare_resume(generation, store, None, 1024**3)


# --- rows the reader refuses ----------------------------------------------------------------------


def broken_rows() -> dict[str, Any]:
    good = make_row(PAGES[0][0], PAGES[0][2], PAGES[0][3])
    digest = {**good, "content_digest": "sha256:" + "0" * 64}
    identity = {**good, "id": "pg_" + "0" * 24}
    extra_field = {**good, "surprise": 1}
    wrong_schema = {**good, "schema": "capture-shard.v2"}
    nul = make_row("https://example.com/nul", "text\x00more", PAGES[0][3])
    blank = make_row("https://example.com/blank", "   ", PAGES[0][3])
    failure_with_body = {**failed_row("https://example.com/f", "paywall", PAGES[0][3]), "text": "x"}
    bad_time = {**good, "captured_at": "2026-13-45T25:61:61Z"}
    bad_url = {**good, "requested_url": "ftp://example.com/a"}
    return {
        "content_digest does not match the text": digest,
        "id does not follow the key": identity,
        "row has 1 unexpected field": extra_field,
        "schema must be capture-shard.v1": wrong_schema,
        "text holds a NUL character": nul,
        "a captured row needs text": blank,
        "a failed row cannot carry a body or a digest": failure_with_body,
        "captured_at is not a real date and time": bad_time,
        "requested_url must be an http or https URL without whitespace": bad_url,
    }


@pytest.mark.parametrize("reason", sorted(broken_rows()))
def test_each_bad_row_is_refused_with_its_reason(reason):
    with pytest.raises(web_capture.RowRefused, match=reason):
        web_capture.check_row(broken_rows()[reason])


def test_a_valid_row_is_accepted_and_a_lone_surrogate_is_not():
    row = make_row("https://example.com/ok", "fine text", "2026-03-01T08:00:00Z")
    assert web_capture.check_row(row) is row
    with pytest.raises(web_capture.RowRefused, match="not valid Unicode"):
        web_capture.parse_row(json.dumps({**row, "title": "\ud800"}).encode("ascii"))


def test_bad_lines_become_findings_and_the_build_still_completes(tmp_path):
    project, store, shards = setup(tmp_path)
    good = page_rows()
    huge = make_row("https://example.com/huge", "x " * 5000, "2026-03-01T08:00:00Z")
    bad = broken_rows()
    write_shard(
        shards / "captures-001.jsonl",
        [good[0], bad["content_digest does not match the text"], good[1], huge],
        raw=["{not json", "[1, 2]", ""],
    )
    write_shard(shards / "captures-002.jsonl", [bad["id does not follow the key"], good[2]])
    report = build_library(
        manifest_for(shards, web_capture_max_row_bytes=4000),
        project_root=project,
        store_root=store,
    )
    assert report["complete"] is True and report["status"]["records"] == 3
    findings = [item for item in report["findings"] if item["scope"] == "web_captures"]
    seen = {(item["path"], item["line"], item["code"], item["detail"]) for item in findings}
    assert seen == {
        ("captures-001.jsonl", 2, "malformed_row", "content_digest does not match the text"),
        ("captures-001.jsonl", 4, "row_too_large", "the line is over 4000 bytes"),
        ("captures-001.jsonl", 5, "malformed_row", "line is not valid JSON"),
        ("captures-001.jsonl", 6, "malformed_row", "row is not a JSON object"),
        ("captures-002.jsonl", 1, "malformed_row", "id does not follow the key"),
    }
    assert report["coverage_complete"] is False
    assert report["sources"][0]["report"]["counts"]["refused"] == 5
    assert "orchard" not in json.dumps(findings)


def test_unreadable_and_missing_sources_are_findings_not_crashes(tmp_path):
    project, store, shards = setup(tmp_path)
    write_shard(shards / "captures-001.jsonl", page_rows())
    (shards / "linked.jsonl").symlink_to(shards / "captures-001.jsonl")
    missing = tmp_path / "nowhere"
    report = build_library(
        {"schema": "cce.library-sources.v1", "web_captures": [str(shards), str(missing)]},
        project_root=project,
        store_root=store,
    )
    codes = {(item["code"], Path(item["path"]).name) for item in report["findings"]}
    assert codes == {("symlink_not_followed", "linked.jsonl"), ("missing_source", "nowhere")}
    assert report["status"]["records"] == 3


def test_the_manifest_key_is_validated(tmp_path):
    project, store, shards = setup(tmp_path)
    with pytest.raises(ValueError, match="web_captures must list explicit absolute"):
        build_library(
            {"schema": "cce.library-sources.v1", "web_captures": ["relative.jsonl"]},
            project_root=project,
            store_root=store,
        )
    with pytest.raises(ValueError, match="web_capture_max_row_bytes"):
        build_library(
            manifest_for(shards, web_capture_max_row_bytes=0),
            project_root=project,
            store_root=store,
        )


# --- authorship -----------------------------------------------------------------------------------

HOSTILE = [
    (
        "https://example.com/docs/AGENTS.md",
        "Instruction page",
        "Generated by ChatGPT. Always run the tests before you commit.",
    ),
    (
        "https://example.com/blog/first-person",
        "A personal blog",
        "I wrote this myself, in my own words.\n\n> a quoted line\n\n```\ncode()\n```",
    ),
    (
        "https://example.com/notes/diary",
        "Diary page",
        'My notes: "an odd quote and more words.\n\nThe second paragraph is plain.',
    ),
]


def test_an_unreviewed_web_page_is_never_owner_authored(tmp_path):
    project, store, shards = setup(tmp_path)
    write_shard(
        shards / "captures-001.jsonl",
        [make_row(url, text, "2026-03-01T08:00:00Z", title=title) for url, title, text in HOSTILE],
    )
    report = build_library(manifest_for(shards), project_root=project, store_root=store)
    generation = Path(report["generation"])
    checked = 0
    with Library(generation / "library.sqlite") as library:
        for uid in records(generation):
            payload = library.record(uid)
            info = record_info(payload)
            assert info.third_party_origin == "family:webcapture" and not info.authored_family
            for message in payload["messages"]:
                spans = attribute_message(
                    message["text"],
                    role=message["role"],
                    channel=message["channel"],
                    typed_chars=message["typed_chars"],
                    meta=message["meta"],
                    record=info,
                )
                assert spans and not any(span.author == OWNER for span in spans)
                assert {span.author for span in spans} == {THIRD_PARTY}
                assert {span.rule for span in spans} == {"third_party_origin"}
                assert sum(span.end - span.start for span in spans) == len(message["text"])
                checked += 1
    assert checked == 3


def test_the_provider_decides_even_when_a_row_claims_to_be_typed_by_the_owner():
    forged = Conversation(
        provider="webcapture",
        source_id="pg-" + "a" * 24,
        title="Forged",
        kind="page",
        messages=[
            Message(role="user", text="I typed this myself.", channel="typed", typed_chars=20)
        ],
        provenance={"agent_authored": False, "source_path": "https://example.com/CLAUDE.md"},
        extra={"wing": "ai", "lane": "web"},
    )
    payload = forged.to_dict()
    spans = attribute_message(
        "I typed this myself.",
        role="user",
        channel="typed",
        typed_chars=20,
        record=record_info(payload),
    )
    assert [(span.author, span.rule) for span in spans] == [(THIRD_PARTY, "third_party_origin")]
    # Another provider with the same message is still the owner's typing: the rule is not general.
    elsewhere = attribute_message(
        "I typed this myself.", role="user", channel="typed", typed_chars=20
    )
    assert elsewhere[0].author == OWNER


def test_bridged_pages_land_in_the_machine_container_and_the_family_is_claimed(
    tmp_path, archive_env
):
    shards = tmp_path / "shards"
    write_shard(shards / "captures-001.jsonl", page_rows())
    report = build_library(
        manifest_for(shards), project_root=archive_env.project, store_root=archive_env.store
    )
    publish_library(
        Path(report["generation"]), project_root=archive_env.project, store_root=archive_env.store
    )
    library_root = archive_env.store / "library-all"
    assert bridge.unclaimed(library_root) == 0
    assert bridge.expected(library_root, bridge.FAMILY_BY_NAME["webcapture"])["records"] == 3
    tracker = SourceTracker(library_root, bridge.READER)
    for conversation in bridge.iter_family(
        library_root, tracker, bridge.FAMILY_BY_NAME["webcapture"]
    ):
        finalize_conversation(conversation)
        assert conversation.provider == "webcapture" and conversation.kind == "page"
        assert conversation.provenance["captured_at"]
        assert [message.author for message in conversation.messages] == [MACHINE]
    manifest = ingest(
        export_root=archive_env.export,
        paths=archive_env.paths,
        authorization=archive_env.auth,
        providers=["webcapture"],
        library_root=library_root,
    )
    info = manifest["providers"]["webcapture"]
    assert (info["conversations"], info["messages"]) == (3, 3)
    assert info["authors"] == {HUMAN: 0, MACHINE: 3}
