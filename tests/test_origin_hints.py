"""Git-history origin hints: the loader, the matching rule and their effect on attribution.

Synthetic repository names and paths only. Hints only SUBTRACT owner credit: `agent` makes a
document's text the agent's, `mixed` and `ambiguous` make it unknown, `human` and no hint change
nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_git_origin_hints import build_alpha, run_tool

from conversation_corpus_engine import attribution_rules
from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.attribution_commands import handle_attribute
from conversation_corpus_engine.attribution_rules import (
    DOCUMENT_KINDS,
    RULES_VERSION,
    attribute_message,
    record_info,
)
from conversation_corpus_engine.cli import build_parser
from conversation_corpus_engine.library_store import Library
from conversation_corpus_engine.origin_hints import (
    MAX_LINE_BYTES,
    OriginHints,
    OriginHintsError,
    load_origin_hints,
)

BODY = "A plain paragraph of prose about the week."


def row(repo: str, path: str, origin: str = "human") -> dict:
    agent = {"agent": 2, "mixed": 1, "human": 0}[origin]
    total = 2 if origin != "mixed" else 2
    return {
        "schema": "cce.origin-hints.v1",
        "repo": repo,
        "path": path,
        "origin": origin,
        "commits": {"total": total, "agent": agent, "human": total - agent},
        "signals": [] if origin == "human" else ["trailer:co-authored-by:claude"],
        "basis": "git-log",
    }


def hints_of(*rows: dict) -> OriginHints:
    hints = OriginHints.from_rows(rows)
    assert not hints.findings, hints.findings
    return hints


def write_hints(path: Path, *rows: object, raw: str = "") -> Path:
    path.write_text(
        "".join(json.dumps(item) + "\n" for item in rows) + raw,
        encoding="utf-8",
    )
    return path


def match(hints: OriginHints, source: str, key: str = "source_file") -> str | None:
    return hints.match({key: source})


# --- the version --------------------------------------------------------------------------------


def test_the_rules_version_names_the_hint_semantics() -> None:
    assert RULES_VERSION == "cce.attribution-rules@11"


# --- loading and validation ---------------------------------------------------------------------


def test_a_valid_file_loads_without_findings(tmp_path: Path) -> None:
    path = write_hints(tmp_path / "h.jsonl", row("r", "a.md", "agent"), row("r", "b.md", "mixed"))
    hints = load_origin_hints(path)
    assert (hints.rows, hints.finding_count, hints.findings) == (2, 0, [])
    hints.refuse_findings()


def test_blank_lines_are_ignored(tmp_path: Path) -> None:
    path = write_hints(tmp_path / "h.jsonl", row("r", "a.md"), raw="\n   \n")
    assert load_origin_hints(path).rows == 1


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"schema": "cce.origin-hints.v0"}, "wrong or missing schema"),
        ({"schema": None}, "wrong or missing schema"),
        ({"repo": ""}, "repo must be a non-empty string"),
        ({"repo": 3}, "repo must be a non-empty string"),
        ({"path": "/"}, "path must be a non-empty string"),
        ({"path": "a/../b.md"}, "a path must not hold .."),
        ({"origin": "robot"}, "origin must be agent, mixed or human"),
        ({"commits": []}, "commits must be an object"),
        ({"commits": {"total": 2, "agent": True, "human": 1}}, "non-negative integers"),
        ({"commits": {"total": 2, "agent": -1, "human": 3}}, "non-negative integers"),
        ({"commits": {"total": 0, "agent": 0, "human": 0}}, "do not add up"),
        ({"commits": {"total": 3, "agent": 1, "human": 1}}, "do not add up"),
        ({"origin": "agent", "commits": {"total": 2, "agent": 1, "human": 1}}, "does not match"),
        ({"origin": "human", "commits": {"total": 2, "agent": 1, "human": 1}}, "does not match"),
        ({"signals": "x"}, "signals must be a list of strings"),
        ({"signals": [1]}, "signals must be a list of strings"),
        ({"basis": "guess"}, "wrong or missing basis"),
    ],
)
def test_a_row_that_breaks_the_schema_is_a_finding(change: dict, reason: str) -> None:
    hints = OriginHints.from_rows([{**row("r", "a.md", "human"), **change}])
    assert hints.rows == 0 and hints.finding_count == 1
    assert reason in hints.findings[0] and hints.findings[0].startswith("line 1: ")


def test_a_missing_field_is_a_finding_and_a_non_object_row_too() -> None:
    incomplete = row("r", "a.md")
    del incomplete["origin"]
    hints = OriginHints.from_rows([incomplete, [1], "text", None])
    assert hints.finding_count == 4 and hints.rows == 0


def test_findings_name_lines_and_reasons_but_never_content(tmp_path: Path) -> None:
    secret = "Secret Person <secret@example.test>"
    bad = {**row("r", "a.md"), "origin": secret}
    path = write_hints(tmp_path / "h.jsonl", row("r", "ok.md"), bad, raw="{broken " + secret + "\n")
    hints = load_origin_hints(path)
    assert hints.rows == 1 and hints.finding_count == 2
    text = " ".join(hints.findings)
    assert "line 2" in text and "line 3" in text and "not valid JSON" in text
    assert secret not in text
    with pytest.raises(OriginHintsError) as refused:
        hints.refuse_findings()
    assert "2 malformed origin-hint row(s)" in str(refused.value)
    assert secret not in str(refused.value)


def test_findings_are_listed_in_bounded_number() -> None:
    hints = OriginHints.from_rows([{}] * 500)
    assert hints.finding_count == 500 and len(hints.findings) <= 20
    with pytest.raises(OriginHintsError, match="and 480 more"):
        hints.refuse_findings()


def test_invalid_utf8_and_overlong_lines_are_findings(tmp_path: Path) -> None:
    path = tmp_path / "h.jsonl"
    long_line = b'{"x": "' + b"a" * (MAX_LINE_BYTES + 10) + b'"}\n'
    path.write_bytes(
        json.dumps(row("r", "a.md")).encode()
        + b"\n\xff\xfe\n"
        + long_line
        + json.dumps(row("r", "b.md")).encode()
        + b"\n"
    )
    hints = load_origin_hints(path)
    assert hints.rows == 2 and hints.finding_count == 2
    assert "line 2: not valid JSON" in hints.findings
    assert "line 3: line is too long" in hints.findings


def test_deeply_nested_json_is_a_finding_not_a_crash(tmp_path: Path) -> None:
    path = tmp_path / "h.jsonl"
    path.write_text("[" * 20000 + "]" * 20000 + "\n", encoding="utf-8")
    hints = load_origin_hints(path)
    assert hints.finding_count == 1


def test_an_oversize_file_is_refused_before_it_is_read(tmp_path: Path) -> None:
    path = write_hints(tmp_path / "h.jsonl", row("r", "a.md"))
    with pytest.raises(OriginHintsError, match="byte limit"):
        load_origin_hints(path, max_bytes=10)


def test_a_file_with_too_many_rows_is_refused(tmp_path: Path) -> None:
    path = write_hints(tmp_path / "h.jsonl", *[row("r", f"{n}.md") for n in range(5)])
    with pytest.raises(OriginHintsError, match="more than 3 rows"):
        load_origin_hints(path, max_rows=3)


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(OriginHintsError, match="cannot read"):
        load_origin_hints(tmp_path / "absent.jsonl")


def test_the_real_tool_output_loads_and_matches(tmp_path: Path) -> None:
    clones = tmp_path / "clones"
    build_alpha(clones)
    out = tmp_path / "hints.jsonl"
    assert run_tool("--repos-dir", clones, "--all", "--out", out).returncode == 0
    hints = load_origin_hints(out)
    assert hints.finding_count == 0 and hints.rows == 11
    assert match(hints, "/backup/alpha/docs/both.md") == "mixed"
    assert match(hints, "backup/alpha/feat.md") == "agent"
    assert match(hints, "backup/alpha/docs/pat.md") == "human"
    assert match(hints, "backup/alpha/docs/other.md") is None


# --- the matching rule --------------------------------------------------------------------------


def test_the_repo_and_path_must_be_the_last_components() -> None:
    hints = hints_of(row("proj", "docs/a.md", "agent"))
    assert match(hints, "proj/docs/a.md") == "agent"
    assert match(hints, "/mnt/backup/2026/proj/docs/a.md") == "agent"
    assert match(hints, "docs/a.md") is None
    assert match(hints, "a.md") is None
    assert match(hints, "xproj/docs/a.md") is None
    assert match(hints, "proj/docs/a.md/extra") is None
    assert match(hints, "proj/docs/xa.md") is None
    assert match(hints, "proj/other/docs/a.md") is None


def test_paths_are_compared_by_whole_components_case_sensitively() -> None:
    hints = hints_of(row("proj", "Docs/A.md", "agent"))
    assert match(hints, "proj/docs/a.md") is None
    assert match(hints, "proj/Docs/A.md") == "agent"


def test_separators_dots_prefixes_and_normalisation_are_read_alike() -> None:
    hints = hints_of(row("proj", "dir/ü.md", "mixed"))
    assert match(hints, "x\\proj\\dir\\ü.md") == "mixed"
    assert match(hints, "./x//proj/./dir/ü.md") == "mixed"
    assert match(hints, "file:///x/proj/dir/ü.md") == "mixed"
    assert match(hints, "  x/proj/dir/ü.md  ") == "mixed"


def test_a_reference_loses_its_fragment_and_query_but_a_file_name_keeps_them() -> None:
    hints = hints_of(row("proj", "a.md", "agent"), row("proj", "c#1.md", "mixed"))
    assert match(hints, "x/proj/a.md#section-2", "source_ref") == "agent"
    assert match(hints, "x/proj/a.md?raw=1", "source_ref") == "agent"
    assert match(hints, "x/proj/a.md#section-2", "source_file") is None
    assert match(hints, "x/proj/c#1.md", "source_file") == "mixed"


def test_the_longest_match_wins() -> None:
    hints = hints_of(row("r", "a/b.md", "human"), row("org/r", "a/b.md", "agent"))
    assert match(hints, "/z/org/r/a/b.md") == "agent"
    assert match(hints, "/z/other/r/a/b.md") == "human"
    assert match(hints, "/z/r/a/b.md") == "human"


def test_a_repo_name_may_span_components_and_equals_the_split_path() -> None:
    split = hints_of(row("r/a", "b.md", "agent"))
    whole = hints_of(row("r", "a/b.md", "agent"))
    assert match(split, "x/r/a/b.md") == match(whole, "x/r/a/b.md") == "agent"


def test_two_rows_for_one_file_that_disagree_are_ambiguous_and_that_agree_are_not() -> None:
    assert match(hints_of(row("r", "a.md", "agent"), row("r", "a.md", "human")), "r/a.md") == (
        "ambiguous"
    )
    assert match(hints_of(row("r", "a.md", "agent"), row("r", "a.md", "agent")), "r/a.md") == (
        "agent"
    )
    split = hints_of(row("r", "a/b.md", "agent"), row("r/a", "b.md", "human"))
    assert match(split, "r/a/b.md") == "ambiguous"


def test_equal_length_matches_of_two_locators_are_ambiguous_when_they_disagree() -> None:
    hints = hints_of(row("r", "a.md", "agent"), row("s", "a.md", "human"))
    both = {"source_file": "x/r/a.md", "source_path": "x/s/a.md"}
    assert hints.match(both) == "ambiguous"
    same = hints_of(row("r", "a.md", "agent"), row("s", "a.md", "agent"))
    assert same.match(both) == "agent"


def test_a_longer_match_of_another_locator_beats_a_shorter_one() -> None:
    hints = hints_of(row("r", "a.md", "human"), row("r", "d/a.md", "agent"))
    record = {"source_file": "x/r/a.md", "source_ref": "x/r/d/a.md#top"}
    assert hints.match(record) == "agent"


def test_records_without_a_locator_or_a_matching_hint_have_no_match() -> None:
    hints = hints_of(row("r", "a.md", "agent"))
    for provenance in ({}, {"source_file": ""}, {"source_file": 3}, None, {"source_file": "r"}):
        assert hints.match(provenance) is None


# --- the effect on attribution ------------------------------------------------------------------


def payload(source: str | None = None, **changes) -> dict:
    base = {
        "provider": "documents",
        "kind": "document",
        "extra": {"wing": "authored", "lane": "writing"},
        "provenance": {"source_file": source} if source is not None else {},
    }
    base.update(changes)
    return base


def outcome(
    source: str | None, hints: OriginHints | None = None, **changes
) -> tuple[str, str, str]:
    record = record_info(payload(source, **changes), hints)
    spans = attribute_message(
        BODY, role="user", channel="written", typed_chars=len(BODY), meta={}, record=record
    )
    assert len(spans) == 1, spans
    return spans[0].rule, spans[0].author, spans[0].detail


ALL_HINTS = hints_of(
    row("proj", "docs/agent.md", "agent"),
    row("proj", "docs/mixed.md", "mixed"),
    row("proj", "docs/human.md", "human"),
    row("proj", "docs/both.md", "agent"),
    row("proj", "docs/both.md", "human"),
)
OWNER = ("note_owner", "owner", "family=documents")


def test_an_agent_hint_makes_a_document_the_agents() -> None:
    assert outcome("b/proj/docs/agent.md", ALL_HINTS) == (
        "document_origin",
        "agent",
        "origin=git:agent-commits",
    )
    assert record_info(payload("b/proj/docs/agent.md"), ALL_HINTS).agent_origin == (
        "git:agent-commits"
    )


def test_a_mixed_hint_makes_a_document_unknown_never_owner() -> None:
    assert outcome("b/proj/docs/mixed.md", ALL_HINTS) == (
        "ambiguous",
        "unknown",
        "origin=git:mixed",
    )
    assert record_info(payload("b/proj/docs/mixed.md"), ALL_HINTS).unknown_origin == "git:mixed"


def test_an_ambiguous_match_is_unknown_never_owner() -> None:
    assert outcome("b/proj/docs/both.md", ALL_HINTS) == (
        "ambiguous",
        "unknown",
        "origin=git:ambiguous",
    )


def test_a_human_hint_changes_nothing_and_never_grants_owner_credit() -> None:
    assert outcome("b/proj/docs/human.md", ALL_HINTS) == OWNER
    assert outcome("b/proj/docs/human.md", ALL_HINTS) == outcome("b/proj/docs/human.md")
    assert record_info(payload("b/proj/docs/human.md"), ALL_HINTS) == record_info(
        payload("b/proj/docs/human.md")
    )
    # Where the older rules say agent or unknown, a human hint does not undo them.
    human = hints_of(
        row("proj", "skills/x.md", "human"),
        row("proj", ".github/y.md", "human"),
        row("proj", "p.png", "human"),
    )
    assert outcome("proj/skills/x.md", human)[1] == "agent"
    assert outcome("proj/.github/y.md", human)[:2] == ("ambiguous", "unknown")
    assert outcome("proj/p.png", human)[:2] == ("ambiguous", "unknown")


def test_no_hint_and_no_hints_change_nothing() -> None:
    assert outcome("b/proj/docs/none.md", ALL_HINTS) == OWNER
    assert outcome("b/proj/docs/agent.md", None) == OWNER
    assert outcome(None, ALL_HINTS) == OWNER
    assert record_info(payload("b/proj/docs/none.md"), ALL_HINTS) == record_info(
        payload("b/proj/docs/none.md")
    )


def test_hints_only_subtract_owner_credit_from_the_older_reasons() -> None:
    hints = hints_of(row("proj", "p.png", "agent"), row("proj", "skills/x.md", "mixed"))
    assert outcome("proj/p.png", hints) == ("document_origin", "agent", "origin=git:agent-commits")
    # An agent convention already holds: its own reason stays.
    assert outcome("proj/skills/x.md", hints) == (
        "document_origin",
        "agent",
        "origin=path:skills",
    )
    # A mixed hint on a path the older rules already call unknown keeps the older reason.
    gh = hints_of(row("proj", ".github/y.md", "mixed"))
    assert outcome("proj/.github/y.md", gh)[2] == "origin=path:.github"


@pytest.mark.parametrize("kind", sorted(DOCUMENT_KINDS))
def test_every_document_kind_takes_the_hints(kind: str) -> None:
    assert outcome("b/proj/docs/agent.md", ALL_HINTS, kind=kind)[1] == "agent"
    assert outcome("b/proj/docs/mixed.md", ALL_HINTS, kind=kind)[1] == "unknown"


@pytest.mark.parametrize("kind", ["session", "chat", "search", "conversation", ""])
def test_chats_are_never_touched(kind: str) -> None:
    for source in ("b/proj/docs/agent.md", "b/proj/docs/mixed.md", "b/proj/docs/both.md"):
        with_hints = record_info(payload(source, kind=kind, provider="chatgpt"), ALL_HINTS)
        without = record_info(payload(source, kind=kind, provider="chatgpt"))
        assert with_hints == without
        assert with_hints.agent_origin == with_hints.unknown_origin == ""


def test_a_third_party_family_stays_third_party() -> None:
    record = record_info(
        payload("b/proj/docs/agent.md", provider="webcapture", kind="page"), ALL_HINTS
    )
    spans = attribute_message(
        BODY, role="user", channel="written", typed_chars=len(BODY), meta={}, record=record
    )
    assert [(s.rule, s.author) for s in spans] == [("third_party_origin", "third_party")]


def test_the_rule_set_is_unchanged_apart_from_the_version() -> None:
    assert attribution_rules.RULES_VERSION.endswith("@11")
    assert "git:agent-commits" in attribution_rules.GIT_AGENT_ORIGIN


# --- the commands -------------------------------------------------------------------------------

TEXTS = {
    "agent": "The agent wrote this paragraph about the parser, in plain words, for the notes.",
    "mixed": "Several hands touched this paragraph about the schedule, and it has no clear author.",
    "human": "This paragraph about the garden was written by a person and a human hint stays quiet.",
    "plain": "This paragraph about the kitchen has no hint at all and is credited as before.",
}


def make_library(directory: Path) -> Path:
    directory.mkdir(parents=True)
    with Library.create(directory / "library.sqlite") as library:
        for name, text in TEXTS.items():
            library.put(
                Conversation(
                    provider="documents",
                    source_id=f"doc-{name}",
                    title=name,
                    kind="document",
                    provenance={"source_file": f"backup/proj/docs/{name}.md"},
                    messages=[
                        Message(
                            role="user",
                            channel="project",
                            id=f"m-{name}",
                            text=text,
                            typed_chars=0,
                            meta={"kind": "document"},
                        )
                    ],
                    extra={"wing": "authored", "lane": "writing", "body_status": "full"},
                )
            )
        library.put(
            Conversation(
                provider="chatgpt",
                source_id="chat-agent",
                title="chat",
                kind="session",
                provenance={"source_file": "backup/proj/docs/agent.md"},
                messages=[Message(role="user", channel="typed", id="c0", text=TEXTS["plain"])],
                extra={"wing": "ai", "lane": "conversations"},
            )
        )
    return directory / "library.sqlite"


@pytest.fixture
def library_path(tmp_path: Path) -> Path:
    return make_library(tmp_path / "store" / "generation")


@pytest.fixture
def hints_path(tmp_path: Path) -> Path:
    return write_hints(
        tmp_path / "hints.jsonl",
        row("proj", "docs/agent.md", "agent"),
        row("proj", "docs/mixed.md", "mixed"),
        row("proj", "docs/human.md", "human"),
    )


def run_attribute(capsys: pytest.CaptureFixture[str], *argv: str | Path) -> tuple[int, str, str]:
    args = build_parser().parse_args(["library", "attribute", *map(str, argv)])
    code = handle_attribute(args)
    out, err = capsys.readouterr()
    return code, out, err


def authors_by_title(library: Path, entries: list[dict]) -> dict[str, set[str]]:
    with Library(library) as opened:
        titles = {row_["uid"]: opened.record(row_["uid"])["title"] for row_ in opened.rows()}
    found: dict[str, set[str]] = {}
    for entry in entries:
        title = titles[entry["source"]["uid"]]
        found.setdefault(title, set()).update(
            span["author"] for span in entry["attribution"]["spans"]
        )
    return found


def apply_authors(
    capsys: pytest.CaptureFixture[str], library: Path, out: Path, *extra: str | Path
) -> dict[str, set[str]]:
    code, _, err = run_attribute(
        capsys, "apply", "--library", library, "--out", out, "--json", *extra
    )
    assert code == 0, err
    return authors_by_title(library, json.loads(out.read_text(encoding="utf-8")))


def test_apply_without_hints_credits_every_document_to_the_owner(
    library_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    authors = apply_authors(capsys, library_path, tmp_path / "plain.json")
    assert authors["agent"] == authors["mixed"] == authors["human"] == authors["plain"]
    assert "owner" in authors["agent"]


def test_apply_with_hints_moves_documents_off_the_owner(
    library_path: Path, hints_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plain = apply_authors(capsys, library_path, tmp_path / "plain.json")
    authors = apply_authors(
        capsys, library_path, tmp_path / "hinted.json", "--origin-hints", hints_path
    )
    assert authors["agent"] == {"agent"}
    assert "mixed" not in authors  # all unknown: no annotation at all
    assert authors["human"] == authors["plain"] and "owner" in authors["plain"]
    # The chat record names the same file as the agent document and is untouched.
    assert authors.get("chat") == plain.get("chat")


def test_the_flag_works_before_the_subcommand_too(
    library_path: Path, hints_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "hinted.json"
    code, _, err = run_attribute(
        capsys,
        "--origin-hints",
        hints_path,
        "apply",
        "--library",
        library_path,
        "--out",
        out,
        "--json",
    )
    assert code == 0, err
    assert authors_by_title(library_path, json.loads(out.read_text()))["agent"] == {"agent"}


def report_for(
    capsys: pytest.CaptureFixture[str], library: Path, *extra: str | Path
) -> tuple[dict, str]:
    code, out, err = run_attribute(capsys, "--report", "--json", "--library", library, *extra)
    assert code == 0, err
    return json.loads(out), err


def owner_characters(report: dict) -> int:
    return report["authors"].get("owner", {}).get("characters", 0)


def test_report_with_hints_has_fewer_owner_characters(
    library_path: Path, hints_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plain, _ = report_for(capsys, library_path)
    hinted, _ = report_for(capsys, library_path, "--origin-hints", hints_path)
    removed = len(TEXTS["agent"]) + len(TEXTS["mixed"])
    assert owner_characters(plain) - owner_characters(hinted) == removed
    assert hinted["partition"]["ok"] and "origin_hints" not in plain
    assert hinted["origin_hints"] == {"rows": 3}


def test_review_sample_accepts_the_flag(
    library_path: Path, hints_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = run_attribute(
        capsys,
        "review-sample",
        "--library",
        library_path,
        "--owner-id",
        "rob",
        "--origin-hints",
        hints_path,
        "--out",
        tmp_path / "sample.jsonl",
    )
    assert code == 0, err
    assert (tmp_path / "sample.jsonl").is_file()


def test_a_sample_drawn_with_hints_is_recorded_only_with_the_same_hints(
    library_path: Path, hints_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sample = tmp_path / "sample.jsonl"
    code, _, err = run_attribute(
        capsys,
        "review-sample",
        "--library",
        library_path,
        "--owner-id",
        "rob",
        "--origin-hints",
        hints_path,
        "--out",
        sample,
    )
    assert code == 0, err

    def record(ledger: Path, *extra: str | Path) -> tuple[int, str]:
        code, _, err = run_attribute(
            capsys,
            "review-record",
            "--library",
            library_path,
            "--ledger",
            ledger,
            "--sample",
            sample,
            "--answers",
            sample,
            "--reviewer",
            "rob",
            "--reviewed-at",
            "2026-01-02T03:04:05Z",
            *extra,
        )
        return code, err

    refused_code, refused = record(tmp_path / "without.jsonl")
    assert refused_code == 2 and "not the draw the rules give" in refused
    assert "--origin-hints" in refused
    assert not (tmp_path / "without.jsonl").exists()
    accepted_code, accepted = record(tmp_path / "with.jsonl", "--origin-hints", hints_path)
    assert accepted_code == 0, accepted
    assert (tmp_path / "with.jsonl").is_file()


def test_a_missing_hints_file_exits_two_and_writes_nothing(
    library_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out.json"
    code, _, err = run_attribute(
        capsys,
        "apply",
        "--library",
        library_path,
        "--out",
        out,
        "--origin-hints",
        tmp_path / "absent.jsonl",
    )
    assert code == 2 and "cannot read the origin hints" in err
    assert not out.exists()


def test_a_malformed_hint_row_exits_two_naming_the_line(
    library_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = write_hints(tmp_path / "bad.jsonl", row("proj", "docs/agent.md", "agent"), raw="oops\n")
    code, _, err = run_attribute(
        capsys, "--report", "--library", library_path, "--origin-hints", bad
    )
    assert code == 2
    assert "1 malformed origin-hint row(s)" in err and "line 2" in err


def test_the_help_explains_the_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["library", "attribute", "--help"])
    text = capsys.readouterr().out
    assert "--origin-hints" in text and "never grants owner" in " ".join(text.split())
