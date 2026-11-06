"""`corpus_store.require_git_private`: what makes a directory fit to hold private plaintext.

Inside a Git work tree a directory is private only when Git ignores the directory itself and
ignores what will be written beneath it. Every repository here is synthetic and lives in a pytest
temporary directory outside any other work tree.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from conversation_corpus_engine import cli, corpus_store
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


# --- what Git is told, and what it may not be taken to mean --------------------------------------

LOCATION_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_NAMESPACE",
)


@pytest.mark.parametrize("variable", LOCATION_VARIABLES)
def test_a_poisoned_git_environment_changes_no_answer(tmp_path, monkeypatch, variable) -> None:
    """GIT_DIR=/nonexistent makes git say 'not a git repository' for every directory, which would
    read as 'outside every work tree'. None of the variables that relocate or hide a repository
    reaches the git processes the guard starts."""
    repo = make_repo(tmp_path, gitignore="ignored/\n")
    (repo / "visible").mkdir()
    (repo / "ignored").mkdir()
    values = {
        "GIT_DIR": tmp_path / "nonexistent",
        "GIT_WORK_TREE": tmp_path / "elsewhere",
        "GIT_COMMON_DIR": tmp_path / "nonexistent",
        "GIT_INDEX_FILE": tmp_path / "nonexistent-index",
        "GIT_CEILING_DIRECTORIES": repo,  # git would not look in or above the work tree
        "GIT_DISCOVERY_ACROSS_FILESYSTEM": "0",
        "GIT_NAMESPACE": "other",
    }
    monkeypatch.setenv(variable, str(values[variable]))
    for target in (repo / "visible" / "x", repo / "visible"):
        with pytest.raises(CorpusStoreError, match="Git-visible"):
            require_git_private(target)
    for target in (repo / "ignored", repo / "ignored" / "x"):
        assert require_git_private(target) == target.resolve()
    assert require_git_private(tmp_path / "outside" / "x") == (tmp_path / "outside" / "x")


def test_the_environment_given_to_git_drops_every_location_variable(monkeypatch) -> None:
    for name in LOCATION_VARIABLES:
        monkeypatch.setenv(name, "poison")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")  # configuration is kept
    environment = corpus_store._git_environment()
    assert not set(LOCATION_VARIABLES) & set(environment)
    assert environment["LC_ALL"] == "C" and environment["GIT_CONFIG_GLOBAL"] == "/dev/null"


@pytest.mark.parametrize(
    "content",
    [None, "gitdir: /nonexistent/elsewhere\n"],
    ids=["empty-dot-git-directory", "dot-git-file-to-nowhere"],
)
def test_git_saying_not_a_repository_beside_a_dot_git_is_not_trusted(tmp_path, content) -> None:
    """Git reports 'not a git repository' for a damaged .git too. That is not the same as the
    directory sitting outside every work tree, so it is refused rather than called private."""
    broken = tmp_path / "broken"
    broken.mkdir()
    if content is None:
        (broken / ".git").mkdir()
    else:
        (broken / ".git").write_text(content, encoding="utf-8")
    for target in (broken / "out", broken / "deeper" / "out"):
        with pytest.raises(CorpusStoreError, match="git could not inspect.*\\.git"):
            require_git_private(target)


def test_a_fake_not_a_repository_answer_cannot_clear_a_real_work_tree(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, gitignore="ignored/\n")
    (repo / "visible").mkdir()
    liar = tmp_path / "bin" / "git"
    liar.parent.mkdir()
    liar.write_text(
        "#!/bin/sh\necho 'fatal: not a git repository (or any of the parent directories): .git'"
        " >&2\nexit 128\n",
        encoding="utf-8",
    )
    liar.chmod(0o755)
    monkeypatch.setenv("PATH", str(liar.parent))
    with pytest.raises(CorpusStoreError, match="git could not inspect"):
        require_git_private(repo / "visible" / "x")


def test_a_directory_whose_ancestors_hold_no_dot_git_is_private_when_git_says_so(tmp_path) -> None:
    deep = tmp_path / "a" / "b" / "c"
    assert require_git_private(deep) == deep.resolve()


# --- the other command that writes a provider's text to a directory the caller names ------------


def run_cli(monkeypatch, capsys, *argv: str) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["cce", *argv])
    code = 0
    try:
        cli.main()
    except SystemExit as exit_:
        code = int(exit_.code or 0)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def stub_fetch(monkeypatch) -> list[Path]:
    calls: list[Path] = []

    def fetch(project_id, output_root, **kwargs):
        calls.append(output_root)
        return {
            "project_name": "Synthetic project",
            "output_root": str(output_root),
            "file_count": 0,
            "total_files": 0,
            "conversation_count": 0,
        }

    monkeypatch.setattr(cli, "fetch_chatgpt_project", fetch)
    return calls


def test_project_extract_refuses_a_git_visible_output_before_fetching_anything(
    tmp_path, monkeypatch, capsys
) -> None:
    calls = stub_fetch(monkeypatch)
    repo = make_repo(tmp_path, gitignore=".*\n")  # only dotfiles ignored: the old probe passed it
    code, _, err = run_cli(
        monkeypatch,
        capsys,
        "project",
        "extract",
        "--project-id",
        "g-p-x",
        "--output",
        str(repo / "out"),
    )
    assert code == 2 and "Git-visible" in err
    assert calls == [] and not (repo / "out").exists()


def test_project_extract_still_writes_where_git_cannot_see(tmp_path, monkeypatch, capsys) -> None:
    calls = stub_fetch(monkeypatch)
    repo = make_repo(tmp_path, gitignore="ignored/\n")
    for output in (tmp_path / "outside" / "out", repo / "ignored" / "out"):
        code, out, _ = run_cli(
            monkeypatch,
            capsys,
            "project",
            "extract",
            "--project-id",
            "g-p-x",
            "--output",
            str(output),
        )
        assert code == 0 and "Synthetic project" in out
    assert calls == [tmp_path / "outside" / "out", repo / "ignored" / "out"]
