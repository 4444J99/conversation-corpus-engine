"""Federation repairs: grouping, duplicate ids, contract validation, and the manifest check.

Every string is invented.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import seed_minimal_corpus
from test_federated_canon import _surface

from conversation_corpus_engine import archive_layout, cli, federation
from conversation_corpus_engine import federated_canon as canon
from conversation_corpus_engine.federation import (
    ArchiveManifestChanged,
    archive_manifest_fingerprint,
    build_federation,
    upsert_corpus,
)
from conversation_corpus_engine.sharded_collection import load_corpus_collection


def _record(corpus: str, name: str) -> dict[str, Any]:
    return {"member_id": f"{corpus}:{name}", "corpus_id": corpus, "canonical_title": name}


def _decision(review: str, subjects: list[str], subject: str, when: str) -> dict[str, Any]:
    return {
        "review_id": review,
        "subject_ids": subjects,
        "canonical_subject": subject,
        "recorded_at": when,
    }


def _decisions(*accepted: dict[str, Any]) -> dict[str, Any]:
    return {**canon.DEFAULT_FEDERATED_DECISIONS, "accepted_family_merges": list(accepted)}


def _apply(records, decisions, skipped=None):
    return canon.apply_decision_groups(
        records, decisions, "accepted_family_merges", "federated-family", skipped=skipped
    )


# --- groups of three or more keep their canonical subject ----------------------------------------


def test_a_group_of_three_built_from_pair_decisions_keeps_its_subject() -> None:
    records = [_record("alpha", "one"), _record("beta", "two"), _record("gamma", "three")]
    decisions = _decisions(
        _decision("r1", ["alpha:one", "beta:two"], "shared-idea", "2026-01-01T00:00:00+00:00"),
        _decision("r2", ["beta:two", "gamma:three"], "shared-idea", "2026-01-02T00:00:00+00:00"),
    )
    groups = _apply(records, decisions)
    assert list(groups) == ["shared-idea"]
    assert [m["member_id"] for m in groups["shared-idea"]] == [
        "alpha:one",
        "beta:two",
        "gamma:three",
    ]


def test_the_widest_then_latest_decision_names_the_group() -> None:
    records = [_record("alpha", "one"), _record("beta", "two"), _record("gamma", "three")]
    decisions = _decisions(
        _decision("r1", ["alpha:one", "beta:two"], "narrow-name", "2026-03-01T00:00:00+00:00"),
        _decision(
            "r2", ["alpha:one", "beta:two", "gamma:three"], "wide-name", "2026-01-01T00:00:00+00:00"
        ),
        _decision("r3", ["beta:two", "gamma:three"], "newer-name", "2026-04-01T00:00:00+00:00"),
    )
    assert list(_apply(records, decisions)) == ["wide-name"]


def test_long_member_ids_that_share_their_first_72_characters_get_distinct_group_ids() -> None:
    stem = "corpus-claude-archive:family-claude-" + "a-long-shared-conversation-stem-" * 2
    first, second = f"{stem}one", f"{stem}two"
    assert first[:72] == second[:72]
    records = [_record("alpha", "x"), _record("alpha", "y")]
    records[0]["member_id"], records[1]["member_id"] = first, second
    groups = _apply(records, _decisions())
    assert len(groups) == 2
    for group_id in groups:
        assert group_id.startswith("federated-family-") and len(group_id.rsplit("-", 1)[-1]) == 16
    assert set(_apply(list(reversed(records)), _decisions())) == set(groups)


def test_a_slug_that_fits_is_left_without_a_digest() -> None:
    assert canon.default_group_id("federated-family", ["alpha:one"]) == (
        "federated-family-alpha-one"
    )
    assert canon.default_group_id("federated-family", ["beta:two", "alpha:one"]) == (
        "federated-family-beta-two-alpha-one"
    )


def test_a_group_with_no_named_decision_gets_the_prefixed_slug() -> None:
    records = [_record("alpha", "one"), _record("beta", "two")]
    decisions = _decisions(_decision("r1", ["alpha:one", "beta:two"], "", "2026-01-01"))
    (group_id,) = _apply(records, decisions)
    assert group_id.startswith("federated-family-")


# --- the union keeps members from different corpora ---------------------------------------------


def test_a_union_that_would_join_two_members_of_one_corpus_is_refused_and_reported() -> None:
    records = [_record("alpha", "one"), _record("alpha", "two"), _record("beta", "three")]
    decisions = _decisions(
        _decision("r1", ["alpha:one", "beta:three"], "first-idea", "2026-01-01"),
        _decision("r2", ["alpha:two", "beta:three"], "second-idea", "2026-01-02"),
    )
    skipped: list[dict[str, Any]] = []
    groups = _apply(records, decisions, skipped)
    for members in groups.values():
        corpora = [member["corpus_id"] for member in members]
        assert len(corpora) == len(set(corpora))
    assert [m["member_id"] for m in groups["first-idea"]] == ["alpha:one", "beta:three"]
    assert [item["review_id"] for item in skipped] == ["r2"]
    assert skipped[0]["members"] == ["alpha:two", "beta:three"]


def test_a_refused_union_still_leaves_every_member_in_exactly_one_group() -> None:
    records = [_record("alpha", "one"), _record("alpha", "two"), _record("beta", "three")]
    decisions = _decisions(
        _decision("r1", ["alpha:one", "beta:three"], "first-idea", "2026-01-01"),
        _decision("r2", ["alpha:two", "beta:three"], "second-idea", "2026-01-02"),
    )
    placed = sorted(m["member_id"] for g in _apply(records, decisions).values() for m in g)
    assert placed == sorted(r["member_id"] for r in records)


# --- a duplicate federated id is an error --------------------------------------------------------


def test_two_groups_given_one_canonical_subject_is_an_error_not_an_overwrite() -> None:
    records = [
        _record("alpha", "one"),
        _record("beta", "two"),
        _record("alpha", "three"),
        _record("beta", "four"),
    ]
    decisions = _decisions(
        _decision("r1", ["alpha:one", "beta:two"], "same-name", "2026-01-01"),
        _decision("r2", ["alpha:three", "beta:four"], "same-name", "2026-01-02"),
    )
    with pytest.raises(canon.DuplicateFederatedId) as raised:
        _apply(records, decisions)
    assert "same-name" in str(raised.value)


def test_a_duplicated_record_is_an_error_not_a_silent_loss() -> None:
    records = [_record("alpha", "one"), _record("beta", "two"), _record("alpha", "one")]
    with pytest.raises(canon.FederatedGroupingError) as raised:
        _apply(records, _decisions())
    message = str(raised.value)
    assert "1 member id(s)" in message and "alpha:one" in message


# --- completeness: every member family appears exactly once -------------------------------------


def _three_corpus_surfaces(tmp_path: Path) -> list[dict[str, Any]]:
    surfaces = []
    for name in ("alpha", "beta", "gamma"):
        surfaces.append(
            _surface(
                tmp_path / name,
                corpus_id=name,
                name=f"{name} corpus",
                family_id=f"family-{name}",
                family_title="Shared Memory Fabric",
                thread_uid=f"thread-{name}",
                themes=["memory", "fabric"],
                entity_id=f"entity-{name}",
                entity_label="Memory Fabric",
                aliases=[],
                action_key=f"action-{name}",
                action_text="Stabilize the shared memory fabric",
                question_key=f"question-{name}",
                question_text="How should the shared memory fabric evolve?",
            )
        )
    return surfaces


@pytest.mark.parametrize(
    "accepted",
    [
        [],
        [("alpha", "beta")],
        [("alpha", "beta"), ("beta", "gamma")],
        [("alpha", "gamma"), ("gamma", "beta"), ("alpha", "beta")],
    ],
)
def test_every_member_family_appears_exactly_once_after_any_accepted_merge(
    tmp_path: Path, accepted: list[tuple[str, str]]
) -> None:
    surfaces = _three_corpus_surfaces(tmp_path)
    project = tmp_path / "project"
    decisions = {**canon.DEFAULT_FEDERATED_DECISIONS, "generated_at": None}
    decisions["accepted_family_merges"] = [
        _decision(
            f"r{index}",
            sorted([f"{left}:family-{left}", f"{right}:family-{right}"]),
            "shared-memory-fabric",
            f"2026-01-0{index + 1}T00:00:00+00:00",
        )
        for index, (left, right) in enumerate(accepted)
    ]
    canon.save_federated_decisions(project, decisions)
    canon.build_federated_canon(project, surfaces)

    families = load_corpus_collection(project / "federation" / "canonical-families.json")
    listed = [m["member_id"] for family in families for m in family["member_families"]]
    assert sorted(listed) == [f"{n}:family-{n}" for n in ("alpha", "beta", "gamma")]
    assert len(listed) == len(set(listed))
    assert sum(family["member_count"] for family in families) == 3
    ids = [family["federated_family_id"] for family in families]
    assert len(ids) == len(set(ids))


def test_a_three_way_merge_through_the_build_keeps_the_canonical_subject(tmp_path: Path) -> None:
    surfaces = _three_corpus_surfaces(tmp_path)
    project = tmp_path / "project"
    decisions = {**canon.DEFAULT_FEDERATED_DECISIONS, "generated_at": None}
    decisions["accepted_family_merges"] = [
        _decision("r1", ["alpha:family-alpha", "beta:family-beta"], "shared-fabric", "2026-01-01"),
        _decision("r2", ["beta:family-beta", "gamma:family-gamma"], "shared-fabric", "2026-01-02"),
    ]
    canon.save_federated_decisions(project, decisions)
    canon.build_federated_canon(project, surfaces)
    families = load_corpus_collection(project / "federation" / "canonical-families.json")
    assert [f["federated_family_id"] for f in families] == ["shared-fabric"]
    assert families[0]["corpus_ids"] == ["alpha", "beta", "gamma"]
    report = json.loads((project / "federation" / "conflict-report.json").read_text())
    assert report["skipped_merges"] == []


# --- contract.json is validated against its schema -----------------------------------------------


def _registered(tmp_path: Path, contract: dict[str, Any] | None) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    corpus = seed_minimal_corpus(tmp_path / "corpus-one", corpus_id="corpus-one")
    upsert_corpus(project, corpus, corpus_id="corpus-one")
    if contract is not None:
        (corpus / "corpus" / "contract.json").write_text(json.dumps(contract), encoding="utf-8")
    return project


def test_build_federation_accepts_a_contract_that_matches_the_schema(tmp_path: Path) -> None:
    project = _registered(tmp_path, None)
    build_federation(project)
    assert (project / "federation" / "registry.json").is_file()


def test_build_federation_refuses_a_contract_that_breaks_the_schema(tmp_path: Path) -> None:
    broken = {
        "contract_name": "conversation-corpus-engine.v2",
        "contract_version": "two",
        "adapter_type": "test-adapter",
        "corpus_id": "corpus-one",
        "name": "Corpus One",
    }
    project = _registered(tmp_path, broken)
    with pytest.raises(canon.ContractInvalid) as raised:
        build_federation(project)
    assert "corpus-one" in str(raised.value)
    assert not (project / "federation" / "registry.json").exists()


# --- the archive manifest is unchanged by a federation build --------------------------------------


def _archive(tmp_path: Path) -> Path:
    root = tmp_path / "archive-all"
    root.mkdir()
    (root / archive_layout.MANIFEST_FILENAME).write_text(
        json.dumps({"schema": "archive-manifest.v2", "source_fingerprint": "ab" * 32}),
        encoding="utf-8",
    )
    return root


def test_the_manifest_fingerprint_is_recorded_and_unchanged(tmp_path: Path) -> None:
    project = _registered(tmp_path, None)
    archive = _archive(tmp_path)
    before = archive_manifest_fingerprint(archive)
    result = build_federation(project, archive_root=archive)
    assert result["archive_manifest_fingerprint"] == before
    assert before["source_fingerprint"] == "ab" * 32
    assert archive_manifest_fingerprint(archive) == before


def test_a_build_that_changes_the_manifest_fails(tmp_path: Path, monkeypatch) -> None:
    project = _registered(tmp_path, None)
    archive = _archive(tmp_path)
    original = federation._build_federation

    def meddling(project_root: Path) -> dict[str, Any]:
        result = original(project_root)
        manifest = archive / "manifest-all.json"
        manifest.write_text(json.dumps({"source_fingerprint": "cd" * 32}), encoding="utf-8")
        return result

    monkeypatch.setattr(federation, "_build_federation", meddling)
    with pytest.raises(ArchiveManifestChanged):
        build_federation(project, archive_root=archive)


def test_the_command_accepts_an_archive_root(tmp_path: Path, monkeypatch, capsys) -> None:
    project = _registered(tmp_path, None)
    archive = _archive(tmp_path)
    argv = [
        "cce",
        "federation",
        "build",
        "--project-root",
        str(project),
        "--archive-root",
        str(archive),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    cli.main()
    printed = json.loads(capsys.readouterr().out)
    assert printed["archive_manifest_fingerprint"]["source_fingerprint"] == "ab" * 32


def test_a_missing_manifest_is_reported_before_the_build(tmp_path: Path) -> None:
    project = _registered(tmp_path, None)
    with pytest.raises(FileNotFoundError):
        build_federation(project, archive_root=tmp_path / "nowhere")
    assert not (project / "federation").exists()
