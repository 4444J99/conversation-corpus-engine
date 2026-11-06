"""`corpus_store.require_git_private`: what makes a directory fit to hold private plaintext.

Inside a Git work tree a directory is private only when Git ignores the directory itself and
ignores what will be written beneath it. Every repository here is synthetic and lives in a pytest
temporary directory outside any other work tree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.corpus_store import CorpusStoreError, require_git_private
from conversation_corpus_engine.library_export import export_library
from conversation_corpus_engine.library_store import Library


@pytest.fixture(autouse=True)
def hermetic_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No developer configuration leaks into a case: not the global or system config, and not the
    default global excludes file (`$XDG_CONFIG_HOME/git/ignore`), which a developer may well have
    filled with dotfile patterns."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


def git(repo: Path, *argv: str) -> None:
    subprocess.run(["git", "-C", str(repo), *argv], check=True, capture_output=True)


def make_repo(
    root: Path,
    name: str = "repo",
    *,
    gitignore: str | None = None,
    exclude: str | None = None,
    nested: dict[str, str] | None = None,
) -> Path:
    repo = root / name
    repo.mkdir()
    git(repo, "init", "-q")
    if gitignore is not None:
        (repo / ".gitignore").write_text(gitignore, encoding="utf-8")
    if exclude is not None:
        (repo / ".git" / "info" / "exclude").write_text(exclude, encoding="utf-8")
    for relative, text in (nested or {}).items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return repo


# (id, .gitignore, .git/info/exclude, nested files, target below the repo, private?)
CASES = [
    pytest.param(".*\n", None, {}, "out", False, id="dotstar-ignores-only-dotfiles"),
    pytest.param(".private-probe\n", None, {}, "out", False, id="the-literal-probe-name"),
    pytest.param("ignored/*\n!ignored/vault/\n", None, {}, "ignored", False, id="negation-parent"),
    pytest.param(
        "ignored/*\n!ignored/vault/\n", None, {}, "ignored/vault", False, id="negation-reincluded"
    ),
    pytest.param("ignored/\n", None, {}, "ignored", True, id="directory-pattern"),
    pytest.param("ignored/\n", None, {}, "ignored/deeper/still", True, id="below-an-ignored-dir"),
    pytest.param("/ignored/\n", None, {}, "ignored", True, id="anchored-directory-pattern"),
    pytest.param("*\n!.gitignore\n", None, {}, "anything", True, id="ignore-everything"),
    pytest.param("a/*\n", None, {}, "a/b/c", True, id="leading-component-ignored"),
    pytest.param("a/*\n", None, {}, "a", False, id="children-ignored-not-the-dir"),
    pytest.param("*.md\n", None, {}, "out", False, id="markdown-only"),
    pytest.param("*.json\n*.jsonl\n", None, {}, "out", False, id="json-only"),
    pytest.param("", None, {"sub/.gitignore": "out/\n"}, "sub/out", True, id="nested-ignore"),
    pytest.param("", None, {"sub/.gitignore": "out/\n"}, "sub/other", False, id="nested-elsewhere"),
    pytest.param("", "ignored/\n", {}, "ignored", True, id="info-exclude-directory"),
    pytest.param("", ".*\n", {}, "out", False, id="info-exclude-dotstar"),
    pytest.param("", None, {}, "out", False, id="no-ignore-rule"),
    pytest.param("ignored/\n", None, {}, ".", False, id="the-work-tree-itself"),
]


@pytest.mark.parametrize("exists", [False, True], ids=["new-directory", "existing-directory"])
@pytest.mark.parametrize(("gitignore", "exclude", "nested", "target", "private"), CASES)
def test_a_directory_is_private_only_when_it_and_what_goes_in_it_are_ignored(
    tmp_path, gitignore, exclude, nested, target, private, exists
) -> None:
    repo = make_repo(tmp_path, gitignore=gitignore, exclude=exclude, nested=nested)
    directory = repo / target
    if exists:
        directory.mkdir(parents=True, exist_ok=True)
    before = sorted(path.relative_to(repo).as_posix() for path in repo.rglob("*"))
    if private:
        assert require_git_private(directory) == directory.resolve()
    else:
        with pytest.raises(CorpusStoreError, match="Git-visible"):
            require_git_private(directory)
    after = sorted(path.relative_to(repo).as_posix() for path in repo.rglob("*"))
    assert after == before, "the check creates and removes nothing"


@pytest.mark.parametrize(
    ("ignored", "private"),
    [("ignored/\n", True), (".*\n", False), ("*.md\n", False)],
    ids=["directory", "dotstar", "markdown"],
)
def test_the_global_excludes_file_counts_like_a_gitignore(
    tmp_path, monkeypatch, ignored, private
) -> None:
    excludes = tmp_path / "global-ignore"
    excludes.write_text(ignored, encoding="utf-8")
    config = tmp_path / "gitconfig"
    config.write_text(f"[core]\n\texcludesFile = {excludes}\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    repo = make_repo(tmp_path)
    target = repo / "ignored"
    if private:
        assert require_git_private(target) == target.resolve()
    else:
        with pytest.raises(CorpusStoreError, match="Git-visible"):
            require_git_private(target)


def test_a_directory_outside_every_work_tree_is_private_and_none_is_created(tmp_path) -> None:
    target = tmp_path / "plain" / "deeper"
    assert require_git_private(target) == target.resolve()
    assert not (tmp_path / "plain").exists()


def test_tracked_files_in_an_ignored_directory_make_it_visible(tmp_path) -> None:
    repo = make_repo(tmp_path, gitignore="ignored/\n")
    (repo / "ignored").mkdir()
    (repo / "ignored" / "kept.txt").write_text("tracked on purpose\n", encoding="utf-8")
    git(repo, "add", "-f", "ignored/kept.txt")
    with pytest.raises(CorpusStoreError, match="Git-visible|tracked"):
        require_git_private(repo / "ignored")


def test_a_symlink_into_a_work_tree_is_judged_by_where_it_points(tmp_path) -> None:
    repo = make_repo(tmp_path, gitignore="ignored/\n")
    (repo / "visible").mkdir()
    (repo / "ignored").mkdir()
    (tmp_path / "to-visible").symlink_to(repo / "visible")
    (tmp_path / "to-ignored").symlink_to(repo / "ignored")
    with pytest.raises(CorpusStoreError, match="Git-visible"):
        require_git_private(tmp_path / "to-visible" / "out")
    assert require_git_private(tmp_path / "to-ignored" / "out") == (repo / "ignored" / "out")


# --- the bypass the verifiers found: a Git-visible directory under a dotfile or probe rule -----


def private_library(tmp_path: Path) -> Path:
    database = tmp_path / "syn.sqlite"
    with Library.create(database) as library:
        library.put(
            Conversation(
                provider="chatgpt",
                source_id="s1",
                title="T",
                messages=[Message(role="user", text="SYNTHETIC-SECRET-TEXT")],
            )
        )
    return database


@pytest.mark.parametrize(
    "gitignore",
    [".*\n", ".private-probe\n", "ignored/*\n!ignored/vault/\n"],
    ids=["dotstar", "probe-name", "negated-vault"],
)
def test_a_library_export_is_refused_where_only_dotfiles_or_the_probe_are_ignored(
    tmp_path, gitignore
) -> None:
    database = private_library(tmp_path)
    repo = make_repo(tmp_path, gitignore=gitignore)
    target = repo / ("ignored" if "ignored" in gitignore else "visible")
    with Library(database) as library, pytest.raises(CorpusStoreError, match="Git-visible"):
        export_library(library, target)
    assert not target.exists(), "nothing was written"
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "SYNTHETIC" not in status and status.strip() in ("", "?? .gitignore")
