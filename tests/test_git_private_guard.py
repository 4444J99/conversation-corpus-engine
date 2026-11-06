"""`corpus_store.require_git_private`: what makes a directory fit to hold private plaintext.

Inside a Git work tree a directory is private only when Git ignores the directory itself and
ignores what will be written beneath it. Every repository here is synthetic and lives in a pytest
temporary directory outside any other work tree.
"""

from __future__ import annotations

import subprocess
import sys
import unicodedata
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


def test_library_export_cards_refuses_a_git_visible_output_before_touching_the_store(
    tmp_path, monkeypatch, capsys
) -> None:
    """export-cards writes record text like export does, so it needs the same guard, first."""
    repo = make_repo(tmp_path, gitignore=".*\n")  # only dotfiles ignored: the old probe passed it
    missing_store = tmp_path / "no-such-store"
    code, _, err = run_cli(
        monkeypatch,
        capsys,
        "library",
        "export-cards",
        "--store-root",
        str(missing_store),
        "--out",
        str(repo / "cards"),
    )
    assert code == 2 and "Git-visible" in err
    assert not (repo / "cards").exists() and not missing_store.exists()


# --- the environment the answer depends on (documented in "Where private text may be written") ---


def test_git_config_global_devnull_does_not_hide_the_default_global_excludes_file(
    tmp_path, monkeypatch
) -> None:
    """GIT_CONFIG_GLOBAL=/dev/null hides the config file, not `$XDG_CONFIG_HOME/git/ignore`, so a
    developer's own excludes file still decides unless HOME and XDG_CONFIG_HOME are redirected."""
    excludes = tmp_path / "xdg" / "git" / "ignore"
    excludes.parent.mkdir(parents=True)
    excludes.write_text("ignored/\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    repo = make_repo(tmp_path)
    assert require_git_private(repo / "ignored") == (repo / "ignored")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "elsewhere"))
    with pytest.raises(CorpusStoreError, match="Git-visible"):
        require_git_private(repo / "ignored")


@pytest.mark.parametrize("how", ["count", "parameters"])
def test_process_scoped_git_config_is_passed_on_and_counts(tmp_path, monkeypatch, how) -> None:
    """An excludes file set through GIT_CONFIG_COUNT/KEY/VALUE or GIT_CONFIG_PARAMETERS reaches
    the git processes the guard starts, on purpose: it is how a caller's own rules are honoured."""
    excludes = tmp_path / "scoped-ignore"
    excludes.write_text("ignored/\n", encoding="utf-8")
    repo = make_repo(tmp_path)
    with pytest.raises(CorpusStoreError, match="Git-visible"):
        require_git_private(repo / "ignored")
    if how == "count":
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.excludesFile")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(excludes))
    else:
        monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'core.excludesfile'='{excludes}'")
    assert require_git_private(repo / "ignored") == (repo / "ignored")


# --- spelling: on a case-insensitive volume the caller's case is not the disk's case -------------


def fake_volume(listings: dict[str, list[str]], *, case_insensitive: bool = True):
    """A `list_directory` / `exists` pair over a made-up tree, so the canonicaliser runs anywhere."""
    by_path = {Path(key): names for key, names in listings.items()}

    def list_directory(directory: Path) -> list[str]:
        if directory not in by_path:
            raise FileNotFoundError(str(directory))
        return list(by_path[directory])

    def fold(name: str) -> str:
        return unicodedata.normalize("NFC", name).casefold()

    def exists(path: Path) -> bool:
        names = by_path.get(path.parent, [])
        if case_insensitive:
            return any(fold(name) == fold(path.name) for name in names)
        return path.name in names

    return list_directory, exists


def canonical(path: str, listings, **kwargs) -> Path:
    list_directory, exists = fake_volume(listings, **kwargs)
    return corpus_store.canonical_spelling(Path(path), list_directory=list_directory, exists=exists)


TREE = {
    "/": ["work", "other"],
    "/work": ["Repo", "notes"],
    "/work/Repo": ["ignored", "Docs"],
    "/work/Repo/ignored": ["Sub"],
}


def test_the_canonical_spelling_is_the_one_on_disk() -> None:
    assert canonical("/WORK/repo/IGNORED/sub", TREE) == Path("/work/Repo/ignored/Sub")
    assert canonical("/work/Repo/ignored/Sub", TREE) == Path("/work/Repo/ignored/Sub")


def test_the_canonical_spelling_keeps_what_does_not_exist_yet_as_given() -> None:
    assert canonical("/work/repo/IGNORED/NewDir/Deeper", TREE) == Path(
        "/work/Repo/ignored/NewDir/Deeper"
    )
    # beneath the first missing component nothing is listed, so nothing is rewritten
    assert canonical("/work/repo/Fresh/IGNORED", TREE) == Path("/work/Repo/Fresh/IGNORED")


def test_a_case_sensitive_volume_never_has_its_names_rewritten() -> None:
    """`Docs` is not `docs` there: a name that does not exist is new, not a respelling."""
    sensitive = {"/": ["work"], "/work": ["docs"], "/work/docs": []}
    assert canonical("/work/Docs/x", sensitive, case_insensitive=False) == Path("/work/Docs/x")
    assert canonical("/work/docs/x", sensitive, case_insensitive=False) == Path("/work/docs/x")


def test_two_entries_for_one_name_are_refused_not_guessed() -> None:
    clash = {"/": ["work"], "/work": ["Out", "OUT"]}
    with pytest.raises(CorpusStoreError, match="more than one"):
        canonical("/work/out", clash)
    # an exact spelling needs no guess
    assert canonical("/work/Out", clash) == Path("/work/Out")


def test_a_name_that_exists_but_cannot_be_matched_is_refused() -> None:
    """exists() says yes and no listed entry folds to it: the spelling cannot be established."""
    tree = {"/": ["work"], "/work": ["something-else"]}
    list_directory, _ = fake_volume(tree)
    with pytest.raises(CorpusStoreError, match="spelling"):
        corpus_store.canonical_spelling(
            Path("/work/odd"), list_directory=list_directory, exists=lambda path: True
        )


def test_an_unreadable_directory_is_refused_not_skipped() -> None:
    def refuse(directory: Path) -> list[str]:
        raise PermissionError(13, "denied", str(directory))

    with pytest.raises(CorpusStoreError, match="Could not list"):
        corpus_store.canonical_spelling(
            Path("/work/x"), list_directory=refuse, exists=lambda path: True
        )


def test_unicode_normalisation_does_not_hide_an_entry() -> None:
    """APFS treats a precomposed and a decomposed name as one; the listing keeps one of them."""
    on_disk = "café"
    asked = "café"
    tree = {"/": ["work"], "/work": [on_disk]}
    assert canonical(f"/work/{asked.upper()}", tree) == Path(f"/work/{on_disk}")


def case_insensitive_volume(directory: Path) -> bool:
    probe = directory / "CasE-Probe"
    probe.mkdir()
    try:
        return (directory / "case-probe").exists()
    finally:
        probe.rmdir()


@pytest.fixture
def needs_case_insensitive_volume(tmp_path: Path) -> None:
    if not case_insensitive_volume(tmp_path):
        pytest.skip("the temporary directory is on a case-sensitive file system")


def case_sensitive_repo(root: Path, *, gitignore: str) -> Path:
    repo = make_repo(root, gitignore=gitignore)
    git(repo, "config", "core.ignorecase", "false")
    return repo


def test_a_respelled_directory_is_judged_where_the_files_really_land(
    tmp_path, needs_case_insensitive_volume
) -> None:
    """A rule for `IGNORED/` does not ignore the real `ignored/` when git matches by exact case,
    and a path spelled `IGNORED/x` writes into `ignored/`. The old check asked git about the
    spelling alone, so it said private about a directory it would then fill."""
    repo = case_sensitive_repo(tmp_path, gitignore="IGNORED/\n")
    (repo / "ignored").mkdir()
    for spelling in ("IGNORED", "ignored", "Ignored"):
        with pytest.raises(CorpusStoreError, match="Git-visible"):
            require_git_private(repo / spelling / "x")
    assert not (repo / "ignored" / "x").exists(), "the check creates nothing"


def test_both_spellings_must_be_private(tmp_path, needs_case_insensitive_volume) -> None:
    """The rule names the real case. The accurate spelling passes; any other spelling is judged
    by name as well and git, matching exact case, does not ignore it, so the strictest wins."""
    repo = case_sensitive_repo(tmp_path, gitignore="ignored/\n")
    (repo / "ignored").mkdir()
    assert require_git_private(repo / "ignored" / "x") == (repo / "ignored" / "x")
    with pytest.raises(CorpusStoreError, match="Git-visible"):
        require_git_private(repo / "IGNORED" / "x")


def test_a_respelled_path_to_the_repository_itself_is_still_judged(
    tmp_path, needs_case_insensitive_volume
) -> None:
    (tmp_path / "Outer").mkdir()
    repo = case_sensitive_repo(tmp_path / "Outer", gitignore="ignored/\n")
    (repo / "ignored").mkdir()
    (repo / "visible").mkdir()
    respelled = tmp_path / "OUTER" / "repo"
    assert require_git_private(respelled / "ignored" / "x") == (respelled / "ignored" / "x")
    with pytest.raises(CorpusStoreError, match="Git-visible"):
        require_git_private(respelled / "visible" / "x")


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
