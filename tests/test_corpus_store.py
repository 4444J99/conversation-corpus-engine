from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conversation_corpus_engine import corpus_store
from conversation_corpus_engine.archive_ingest import ArchivePaths, require_inside
from conversation_corpus_engine.archive_turns import build_turn_store
from conversation_corpus_engine.corpus_store import (
    CORPUS_STORE_ROOT_ENV,
    CorpusReadAuthorization,
    CorpusStoreError,
    CorpusStoreUnavailableError,
    CorpusWriteAuthorization,
    authorize_additional_corpus_path,
    authorize_corpus_read,
    authorize_corpus_write,
    register_corpus_store,
    require_git_private,
    resolve_configured_corpus_store_root,
)
from conversation_corpus_engine.federation import REGISTRY_VERSION, registry_path


def _make_project_and_store(tmp_path: Path) -> tuple[Path, Path]:
    project_root = tmp_path / "project"
    store_root = tmp_path / "private-corpus-store"
    project_root.mkdir(parents=True)
    store_root.mkdir()
    register_corpus_store(project_root, store_root)
    return project_root, store_root


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_git_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init")


def _tree_snapshot(root: Path) -> dict[str, bytes | str]:
    snapshot: dict[str, bytes | str] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            snapshot[relative] = f"symlink:{path.readlink()}"
        elif path.is_file():
            snapshot[relative] = path.read_bytes()
        else:
            snapshot[relative] = "directory"
    return snapshot


def test_registration_writes_v2_and_same_root_is_byte_idempotent(tmp_path: Path) -> None:
    project_root, store_root = _make_project_and_store(tmp_path)
    path = registry_path(project_root)
    before = path.read_bytes()

    registration = register_corpus_store(project_root, store_root)

    assert path.read_bytes() == before
    registry = json.loads(before)
    assert registry["registry_version"] == REGISTRY_VERSION == 2
    assert registry["corpus_store"] == registration
    assert registry["corpora"] == []


def test_different_registration_replaces_singular_store_without_history(tmp_path: Path) -> None:
    project_root, first_store = _make_project_and_store(tmp_path)
    second_store = tmp_path / "second-store"
    second_store.mkdir()
    first_registration = register_corpus_store(project_root, first_store)

    second_registration = register_corpus_store(project_root, second_store)

    registry = json.loads(registry_path(project_root).read_text())
    assert registry["corpus_store"] == second_registration
    assert registry["corpus_store"]["root"] == str(second_store.resolve())
    assert first_registration["root"] not in json.dumps(registry["corpus_store"])
    assert "corpus_stores" not in registry


def test_explicit_configuration_precedes_environment(tmp_path: Path) -> None:
    project_root, store_root = _make_project_and_store(tmp_path)
    other_store = tmp_path / "other-store"
    other_store.mkdir()

    resolved = resolve_configured_corpus_store_root(
        store_root,
        environ={CORPUS_STORE_ROOT_ENV: str(other_store)},
    )
    authorization = authorize_corpus_write(
        project_root=project_root,
        corpus_store_root=resolved,
        destination=store_root / "corpus-a",
    )

    assert authorization.store_root == store_root.resolve()
    assert authorization.destination == (store_root / "corpus-a").resolve()


def test_environment_configuration_is_used_when_cli_value_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root, store_root = _make_project_and_store(tmp_path)
    monkeypatch.setenv(CORPUS_STORE_ROOT_ENV, str(store_root))

    authorization = authorize_corpus_write(
        project_root=project_root,
        destination=store_root / "corpus-a",
    )

    assert authorization.store_root == store_root.resolve()


def test_missing_configuration_and_unregistered_root_create_nothing(tmp_path: Path) -> None:
    project_root, store_root = _make_project_and_store(tmp_path)
    other_store = tmp_path / "other-store"
    other_store.mkdir()
    missing_destination = store_root / "missing" / "corpus"
    before = _tree_snapshot(tmp_path)

    with pytest.raises(CorpusStoreError, match="not configured"):
        authorize_corpus_write(
            project_root=project_root,
            destination=missing_destination,
            corpus_store_root=None,
        )
    with pytest.raises(CorpusStoreError, match="does not match"):
        authorize_corpus_write(
            project_root=project_root,
            corpus_store_root=other_store,
            destination=other_store / "corpus",
        )

    assert _tree_snapshot(tmp_path) == before
    assert not missing_destination.exists()


def test_outside_git_and_git_ignored_roots_are_accepted(tmp_path: Path) -> None:
    project_root, store_root = _make_project_and_store(tmp_path / "outside")
    assert authorize_corpus_write(
        project_root=project_root,
        corpus_store_root=store_root,
        destination=store_root / "corpus",
    )

    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / ".gitignore").write_text("private/\nstate/\n", encoding="utf-8")
    ignored_store = repo / "private"
    ignored_store.mkdir()
    register_corpus_store(repo, ignored_store)

    assert authorize_corpus_write(
        project_root=repo,
        corpus_store_root=ignored_store,
        destination=ignored_store / "corpus",
    )


def _fake_git(directory: Path, *, exit_code: int, message: str) -> None:
    """A `git` that fails the way a real one can: nothing on stdout, a message on stderr."""
    directory.mkdir()
    script = directory / "git"
    script.write_text(f"#!/bin/sh\necho '{message}' >&2\nexit {exit_code}\n", encoding="utf-8")
    script.chmod(0o755)


def test_git_saying_not_a_repository_means_outside_every_work_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_git(
        tmp_path / "bin",
        exit_code=128,
        message="fatal: not a git repository (or any of the parent directories): .git",
    )
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    target = tmp_path / "private-output"
    target.mkdir()
    assert require_git_private(target) == target.resolve()


@pytest.mark.parametrize(
    ("exit_code", "message"),
    [
        (128, "fatal: detected dubious ownership in repository"),
        (1, "fatal: not a git repository"),
        (2, "error: permission denied"),
    ],
)
def test_any_other_git_failure_is_an_error_not_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int, message: str
) -> None:
    _fake_git(tmp_path / "bin", exit_code=exit_code, message=message)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    target = tmp_path / "private-output"
    target.mkdir()
    with pytest.raises(CorpusStoreError, match=rf"git could not inspect .*exit {exit_code}"):
        require_git_private(target)


def test_a_missing_git_binary_is_an_error_not_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "empty-bin").mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    target = tmp_path / "private-output"
    target.mkdir()
    with pytest.raises(CorpusStoreError, match="Could not run git"):
        require_git_private(target)


def test_tracked_content_and_ignore_negation_are_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / ".gitignore").write_text("private/\nstate/\n", encoding="utf-8")
    store_root = repo / "private"
    store_root.mkdir()
    tracked = store_root / "tracked.txt"
    tracked.write_text("tracked\n", encoding="utf-8")
    _git(repo, "add", "-f", "private/tracked.txt")

    with pytest.raises(CorpusStoreError, match="tracked"):
        register_corpus_store(repo, store_root)

    _git(repo, "rm", "--cached", "private/tracked.txt")
    register_corpus_store(repo, store_root)
    visible = store_root / "visible.txt"
    visible.write_text("visible\n", encoding="utf-8")
    (repo / ".gitignore").write_text(
        "private/*\n!private/visible.txt\nstate/\n",
        encoding="utf-8",
    )

    with pytest.raises(CorpusStoreError, match="Git-visible|negation"):
        authorize_corpus_write(
            project_root=repo,
            corpus_store_root=store_root,
            destination=store_root / "corpus",
        )


def test_registered_root_that_becomes_git_visible_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    ignore_path = repo / ".gitignore"
    ignore_path.write_text("private/\nstate/\n", encoding="utf-8")
    store_root = repo / "private"
    store_root.mkdir()
    register_corpus_store(repo, store_root)
    ignore_path.write_text("state/\n", encoding="utf-8")

    with pytest.raises(CorpusStoreError, match="Git-visible"):
        authorize_corpus_write(
            project_root=repo,
            corpus_store_root=store_root,
            destination=store_root / "corpus",
        )


@pytest.mark.parametrize(
    ("destination_kind", "message"),
    [
        ("equal", "strict descendant"),
        ("outside", "strict descendant"),
        ("source_parent", "overlap"),
        ("source_child", "overlap"),
    ],
)
def test_containment_and_bidirectional_overlap_are_rejected(
    tmp_path: Path, destination_kind: str, message: str
) -> None:
    project_root, store_root = _make_project_and_store(tmp_path)
    corpus_root = store_root / "corpus"
    source_child = corpus_root / "source"
    source_child.mkdir(parents=True)
    if destination_kind == "equal":
        destination = store_root
        source_roots: tuple[Path, ...] = ()
    elif destination_kind == "outside":
        destination = tmp_path / "outside"
        source_roots = ()
    elif destination_kind == "source_parent":
        destination = corpus_root / "candidate"
        source_roots = (corpus_root,)
    else:
        destination = corpus_root
        source_roots = (source_child,)

    with pytest.raises(CorpusStoreError, match=message):
        authorize_corpus_write(
            project_root=project_root,
            corpus_store_root=store_root,
            destination=destination,
            source_roots=source_roots,
        )


def test_root_and_descendant_symlinks_are_rejected(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    actual_store = tmp_path / "actual-store"
    actual_store.mkdir()
    linked_store = tmp_path / "linked-store"
    linked_store.symlink_to(actual_store, target_is_directory=True)

    with pytest.raises(CorpusStoreError, match="symlink"):
        register_corpus_store(project_root, linked_store)

    register_corpus_store(project_root, actual_store)
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_child = actual_store / "linked-child"
    linked_child.symlink_to(outside, target_is_directory=True)

    with pytest.raises(CorpusStoreError, match="symlink"):
        authorize_corpus_write(
            project_root=project_root,
            corpus_store_root=actual_store,
            destination=linked_child / "corpus",
        )


def test_nested_clone_incident_replay_is_rejected_without_writes(tmp_path: Path) -> None:
    project_root, store_root = _make_project_and_store(tmp_path)
    nested = store_root / "candidate"
    (nested / ".git").mkdir(parents=True)
    destination = nested / "corpus"
    before = _tree_snapshot(tmp_path)

    with pytest.raises(CorpusStoreError, match="nested Git"):
        authorize_corpus_write(
            project_root=project_root,
            corpus_store_root=store_root,
            destination=destination,
        )

    assert _tree_snapshot(tmp_path) == before
    assert not destination.exists()


def test_malformed_registry_denial_does_not_bootstrap_or_rewrite(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    store_root = tmp_path / "store"
    project_root.mkdir()
    store_root.mkdir()
    path = registry_path(project_root)
    path.parent.mkdir()
    path.write_text("{malformed", encoding="utf-8")
    before = _tree_snapshot(tmp_path)

    with pytest.raises(CorpusStoreError, match="malformed"):
        authorize_corpus_write(
            project_root=project_root,
            corpus_store_root=store_root,
            destination=store_root / "corpus",
        )

    assert _tree_snapshot(tmp_path) == before


# --- read-only custody: the cheap properties, no whole-store walks --------------------------------


def _forbid_walks(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a read-only attachment walked the store")

    monkeypatch.setattr(corpus_store, "_find_nested_git_metadata", refuse)
    monkeypatch.setattr(corpus_store, "_find_symlink", refuse)


def _record_walks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Let both walks run as they do, and list which of them were called."""
    called: list[str] = []
    for name in ("_find_nested_git_metadata", "_find_symlink"):
        original = getattr(corpus_store, name)

        def spy(*args: object, _name: str = name, _original=original, **kwargs: object):
            called.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(corpus_store, name, spy)
    return called


def _archive_in(store_root: Path) -> Path:
    archive_root = store_root / "archive-all"
    archive_root.mkdir()
    return archive_root


def test_read_authorization_attaches_without_walking_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root, store_root = _make_project_and_store(tmp_path)
    archive_root = _archive_in(store_root)
    _forbid_walks(monkeypatch)

    authorization = authorize_corpus_read(project_root, archive_root, store_root)

    assert isinstance(authorization, CorpusReadAuthorization)
    assert authorization.project_root == project_root.resolve()
    assert authorization.store_root == store_root.resolve()
    assert authorization.destination == archive_root.resolve()


def test_read_authorization_takes_the_store_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root, store_root = _make_project_and_store(tmp_path)
    archive_root = _archive_in(store_root)
    monkeypatch.setenv(CORPUS_STORE_ROOT_ENV, str(store_root))
    _forbid_walks(monkeypatch)

    authorization = authorize_corpus_read(project_root, archive_root)

    assert authorization.store_root == store_root.resolve()


def test_read_authorization_accepts_a_git_ignored_store_inside_a_work_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / ".gitignore").write_text("private/\n", encoding="utf-8")
    store_root = repo / "private"
    store_root.mkdir()
    register_corpus_store(repo, store_root)
    archive_root = _archive_in(store_root)
    _forbid_walks(monkeypatch)

    assert authorize_corpus_read(repo, archive_root, store_root).destination == archive_root


def test_write_authorization_still_walks_the_store_for_nested_git_and_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root, store_root = _make_project_and_store(tmp_path)
    archive_root = _archive_in(store_root)
    called = _record_walks(monkeypatch)

    authorization = authorize_corpus_write(
        project_root=project_root, corpus_store_root=store_root, destination=archive_root
    )

    assert isinstance(authorization, CorpusWriteAuthorization)
    assert sorted(called) == ["_find_nested_git_metadata", "_find_symlink"]


def test_a_read_authorization_is_not_write_custody(tmp_path: Path) -> None:
    project_root, store_root = _make_project_and_store(tmp_path)
    archive_root = _archive_in(store_root)
    read = authorize_corpus_read(project_root, archive_root, store_root)

    assert not issubclass(CorpusReadAuthorization, CorpusWriteAuthorization)
    assert not isinstance(read, CorpusWriteAuthorization)
    with pytest.raises(CorpusStoreError, match="write custody"):
        require_inside(read, archive_root / "turns")  # type: ignore[arg-type]
    with pytest.raises(CorpusStoreError, match="write custody"):
        authorize_additional_corpus_path(read, archive_root / "extra")  # type: ignore[arg-type]
    with pytest.raises(CorpusStoreError, match="write custody"):
        build_turn_store(ArchivePaths(read.destination), read)  # type: ignore[arg-type]
    assert not (archive_root / "turns").exists()
    assert not (archive_root / "extra").exists()


def test_read_authorization_refuses_a_symlinked_store_root(tmp_path: Path) -> None:
    project_root, store_root = _make_project_and_store(tmp_path)
    archive_root = _archive_in(store_root)
    linked_store = tmp_path / "linked-store"
    linked_store.symlink_to(store_root, target_is_directory=True)

    with pytest.raises(CorpusStoreError, match="may not be a symlink"):
        authorize_corpus_read(project_root, linked_store / archive_root.name, linked_store)


def test_read_authorization_refuses_an_unregistered_store(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    store_root = tmp_path / "store"
    project_root.mkdir()
    store_root.mkdir()
    archive_root = _archive_in(store_root)

    with pytest.raises(CorpusStoreError, match="registry does not exist"):
        authorize_corpus_read(project_root, archive_root, store_root)


def test_read_authorization_refuses_a_registration_that_is_not_active(tmp_path: Path) -> None:
    project_root, store_root = _make_project_and_store(tmp_path)
    archive_root = _archive_in(store_root)
    path = registry_path(project_root)
    registry = json.loads(path.read_text(encoding="utf-8"))
    registry["corpus_store"]["status"] = "retired"
    path.write_text(json.dumps(registry), encoding="utf-8")

    with pytest.raises(CorpusStoreError, match="not active"):
        authorize_corpus_read(project_root, archive_root, store_root)


def test_read_authorization_refuses_a_store_the_registration_does_not_name(
    tmp_path: Path,
) -> None:
    project_root, _ = _make_project_and_store(tmp_path)
    other_store = tmp_path / "other-store"
    other_store.mkdir()
    archive_root = _archive_in(other_store)

    with pytest.raises(CorpusStoreError, match="does not match the active registration"):
        authorize_corpus_read(project_root, archive_root, other_store)


@pytest.mark.parametrize("destination_kind", ["equal", "outside"])
def test_read_authorization_refuses_an_archive_that_is_not_beneath_the_store(
    tmp_path: Path, destination_kind: str
) -> None:
    project_root, store_root = _make_project_and_store(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    archive_root = store_root if destination_kind == "equal" else outside

    with pytest.raises(CorpusStoreError, match="strict descendant"):
        authorize_corpus_read(project_root, archive_root, store_root)


def test_read_authorization_refuses_a_symlink_on_the_way_to_the_archive(tmp_path: Path) -> None:
    project_root, store_root = _make_project_and_store(tmp_path)
    archive_root = _archive_in(store_root)
    linked_archive = store_root / "linked-archive"
    linked_archive.symlink_to(archive_root, target_is_directory=True)

    with pytest.raises(CorpusStoreError, match="traverses a symlink"):
        authorize_corpus_read(project_root, linked_archive, store_root)


def test_read_authorization_refuses_a_store_that_became_git_visible(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    ignore_path = repo / ".gitignore"
    ignore_path.write_text("private/\n", encoding="utf-8")
    store_root = repo / "private"
    store_root.mkdir()
    register_corpus_store(repo, store_root)
    archive_root = _archive_in(store_root)
    ignore_path.write_text("other/\n", encoding="utf-8")

    with pytest.raises(CorpusStoreError, match="Git-visible"):
        authorize_corpus_read(repo, archive_root, store_root)


def test_read_authorization_refuses_tracked_content_and_ignore_negation(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / ".gitignore").write_text("private/\n", encoding="utf-8")
    store_root = repo / "private"
    store_root.mkdir()
    register_corpus_store(repo, store_root)
    archive_root = _archive_in(store_root)
    tracked = store_root / "tracked.txt"
    tracked.write_text("tracked\n", encoding="utf-8")
    _git(repo, "add", "-f", "private/tracked.txt")

    with pytest.raises(CorpusStoreError, match="tracked"):
        authorize_corpus_read(repo, archive_root, store_root)

    _git(repo, "rm", "--cached", "private/tracked.txt")
    (store_root / "visible.txt").write_text("visible\n", encoding="utf-8")
    (repo / ".gitignore").write_text("private/*\n!private/visible.txt\n", encoding="utf-8")

    with pytest.raises(CorpusStoreError, match="Git-visible|negation"):
        authorize_corpus_read(repo, archive_root, store_root)


def test_read_authorization_names_a_missing_archive(tmp_path: Path) -> None:
    project_root, store_root = _make_project_and_store(tmp_path)

    with pytest.raises(CorpusStoreUnavailableError, match="Archive root does not exist"):
        authorize_corpus_read(project_root, store_root / "archive-all", store_root)


def test_read_authorization_refuses_an_archive_that_is_a_file(tmp_path: Path) -> None:
    project_root, store_root = _make_project_and_store(tmp_path)
    (store_root / "archive-all").write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(CorpusStoreUnavailableError, match="not a directory"):
        authorize_corpus_read(project_root, store_root / "archive-all", store_root)


def test_an_unmounted_store_says_the_root_does_not_exist(tmp_path: Path) -> None:
    project_root, store_root = _make_project_and_store(tmp_path)
    archive_root = _archive_in(store_root)
    shutil.rmtree(store_root)  # the volume is gone

    with pytest.raises(CorpusStoreUnavailableError, match="does not exist.*unmounted") as read:
        authorize_corpus_read(project_root, archive_root, store_root)
    with pytest.raises(CorpusStoreUnavailableError, match="does not exist.*unmounted") as write:
        authorize_corpus_write(
            project_root=project_root, corpus_store_root=store_root, destination=archive_root
        )
    assert str(store_root) in str(read.value)
    assert str(store_root) in str(write.value)
