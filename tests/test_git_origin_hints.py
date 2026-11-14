"""The standalone git-history tool `scripts/git_origin_hints.py`, on synthetic repositories.

Every repository is built in a temp directory with fixed identities and dates. No real names,
repositories or paths appear here.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import unicodedata
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "git_origin_hints.py"
SENTINEL_TEXT = "SENTINEL-COMMIT-TEXT-7731"
SENTINEL_NAME = "Zed Sentinelname"
SENTINEL_EMAIL = "zed.sentinel@example.test"
HUMAN = ("Alice Example", "alice@example.test")
BOT = ("dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com")
CLAUDE_TRAILER = "Co-Authored-By: Claude <noreply@anthropic.com>"
FOOTER = "\N{ROBOT FACE} Generated with [Claude Code](https://claude.com/claude-code)"
CLAUDE_SIGNALS = [
    "trailer:co-authored-by:anthropic",
    "trailer:co-authored-by:claude",
    "trailer:co-authored-by:noreply@anthropic.com",
]
ISOLATION = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
}


def load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("git_origin_hints", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["git_origin_hints"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in ISOLATION.items():
        monkeypatch.setenv(key, value)


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"]
        + list(args),
        check=True,
        capture_output=True,
        env={**os.environ, **ISOLATION, **(env or {})},
    )


class Clock:
    def __init__(self) -> None:
        self.tick = 0

    def next(self) -> str:
        self.tick += 1
        return f"2026-01-{self.tick:02d}T12:00:00+0000"


def commit(
    repo: Path,
    clock: Clock,
    message: str,
    files: dict[str, str | None],
    *,
    author: tuple[str, str] = HUMAN,
    committer: tuple[str, str] | None = None,
) -> None:
    for name, content in files.items():
        target = repo / name
        if content is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    stamp = clock.next()
    who = committer or author
    env = {
        "GIT_AUTHOR_NAME": author[0],
        "GIT_AUTHOR_EMAIL": author[1],
        "GIT_AUTHOR_DATE": stamp,
        "GIT_COMMITTER_NAME": who[0],
        "GIT_COMMITTER_EMAIL": who[1],
        "GIT_COMMITTER_DATE": stamp,
    }
    holder = repo.parent / f"{repo.name}.message"
    holder.write_text(message, encoding="utf-8")
    git(repo, "commit", "--allow-empty", "-F", str(holder), env=env)


def build_alpha(root: Path) -> Path:
    """One repository holding every kind of history the tool must tell apart."""
    repo = root / "alpha"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    clock = Clock()
    commit(
        repo,
        clock,
        f"start the notes\n\n{SENTINEL_TEXT}",
        {
            "docs/human.md": "one",
            "docs/both.md": "one",
            "dir with space/ünï.md": "one",
            'quote"name.md': "one",
        },
        author=(SENTINEL_NAME, SENTINEL_EMAIL),
    )
    commit(
        repo,
        clock,
        f"write the agent file\n\n{CLAUDE_TRAILER}\n",
        {"docs/agent.md": "one", "docs/both.md": "two"},
    )
    commit(repo, clock, "bump a pin", {"docs/bot.md": "one"}, author=BOT)
    commit(repo, clock, f"another agent file\n\n{FOOTER}\n", {"docs/footer.md": "one"})
    commit(
        repo,
        clock,
        "copilot file\n\nco-authored-by: Copilot <copilot@example.test>",
        {"docs/copilot.md": "one"},
    )
    commit(
        repo,
        clock,
        "a person helped\n\nCo-Authored-By: Pat Example <pat@example.test>",
        {"docs/pat.md": "one"},
    )
    commit(
        repo,
        clock,
        f"squash of two commits\n\n{CLAUDE_TRAILER}\n\nthe second squashed message follows",
        {"docs/human.md": "two"},
    )
    git(repo, "mv", "docs/agent.md", "docs/agent-renamed.md")
    commit(repo, clock, "move the file", {})
    git(repo, "checkout", "-q", "-b", "feature")
    commit(repo, clock, f"feature work\n\n{CLAUDE_TRAILER}\n", {"feat.md": "one"})
    git(repo, "checkout", "-q", "main")
    stamp = clock.next()
    env = {
        "GIT_AUTHOR_NAME": HUMAN[0],
        "GIT_AUTHOR_EMAIL": HUMAN[1],
        "GIT_AUTHOR_DATE": stamp,
        "GIT_COMMITTER_NAME": HUMAN[0],
        "GIT_COMMITTER_EMAIL": HUMAN[1],
        "GIT_COMMITTER_DATE": stamp,
    }
    git(repo, "merge", "-q", "--no-ff", "-m", "merge the feature", "feature", env=env)
    return repo


def expected_rows(repo: str = "alpha") -> list[dict]:
    def row(path: str, origin: str, total: int, agent: int, signals: list[str]) -> dict:
        return {
            "schema": "cce.origin-hints.v1",
            "repo": repo,
            "path": path,
            "origin": origin,
            "commits": {"total": total, "agent": agent, "human": total - agent},
            "signals": sorted(signals),
            "basis": "git-log",
        }

    bot = [
        f"{kind}:{label}"
        for kind in ("author", "author-email", "committer", "committer-email")
        for label in ("bot", "dependabot")
    ]
    rows = [
        row("dir with space/ünï.md", "human", 1, 0, []),
        row("docs/agent-renamed.md", "human", 1, 0, []),
        row("docs/agent.md", "mixed", 2, 1, CLAUDE_SIGNALS),
        row("docs/bot.md", "agent", 1, 1, bot),
        row("docs/both.md", "mixed", 2, 1, CLAUDE_SIGNALS),
        row("docs/copilot.md", "agent", 1, 1, ["trailer:co-authored-by:copilot"]),
        row("docs/footer.md", "agent", 1, 1, ["footer:generated-with"]),
        row("docs/human.md", "mixed", 2, 1, CLAUDE_SIGNALS),
        row("docs/pat.md", "human", 1, 0, []),
        row("feat.md", "agent", 1, 1, CLAUDE_SIGNALS),
        row('quote"name.md', "human", 1, 0, []),
    ]
    return sorted(rows, key=lambda item: (item["repo"], item["path"]))


def lines_of(rows: list[dict]) -> str:
    return "".join(json.dumps(row, ensure_ascii=True) + "\n" for row in rows)


def run_tool(*argv: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, argv)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **ISOLATION},
    )


@pytest.fixture(scope="module")
def clones(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("clones")
    build_alpha(root)
    return root


# --- the output ---------------------------------------------------------------------------------


def test_all_files_are_emitted_exactly_once_and_sorted(clones: Path, tmp_path: Path) -> None:
    out = tmp_path / "hints.jsonl"
    done = run_tool("--repos-dir", clones, "--all", "--out", out)
    assert done.returncode == 0, done.stderr
    assert out.read_text(encoding="utf-8") == lines_of(expected_rows())


def test_the_output_is_deterministic(clones: Path, tmp_path: Path) -> None:
    first, second = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    assert run_tool("--repos-dir", clones, "--all", "--out", first).returncode == 0
    assert run_tool("--repos-dir", clones, "--all", "--out", second).returncode == 0
    assert first.read_bytes() == second.read_bytes()


def test_the_default_destination_is_standard_output(clones: Path) -> None:
    done = run_tool("--repos-dir", clones, "--all")
    assert done.returncode == 0
    assert done.stdout == lines_of(expected_rows())


def test_merges_are_excluded_and_a_rename_is_not_followed(clones: Path, tmp_path: Path) -> None:
    out = tmp_path / "hints.jsonl"
    run_tool("--repos-dir", clones, "--all", "--out", out)
    rows = {json.loads(line)["path"]: json.loads(line) for line in out.read_text().splitlines()}
    assert rows["feat.md"]["commits"] == {"total": 1, "agent": 1, "human": 0}
    # The agent's file keeps its agent commit and gains the human move; the new name starts there.
    assert rows["docs/agent.md"]["origin"] == "mixed"
    assert rows["docs/agent-renamed.md"]["origin"] == "human"
    assert rows["docs/agent-renamed.md"]["commits"]["total"] == 1


def test_a_trailer_in_the_middle_of_a_squash_message_still_counts(
    clones: Path, tmp_path: Path
) -> None:
    out = tmp_path / "hints.jsonl"
    run_tool("--repos-dir", clones, "--all", "--out", out)
    rows = {json.loads(line)["path"]: json.loads(line) for line in out.read_text().splitlines()}
    assert rows["docs/human.md"]["origin"] == "mixed"
    assert rows["docs/human.md"]["commits"] == {"total": 2, "agent": 1, "human": 1}


def test_a_bare_clone_gives_the_same_rows(clones: Path, tmp_path: Path) -> None:
    store = tmp_path / "bare"
    store.mkdir()
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(clones / "alpha"), str(store / "alpha.git")],
        check=True,
        capture_output=True,
        env={**os.environ, **ISOLATION},
    )
    out = tmp_path / "hints.jsonl"
    done = run_tool("--repos-dir", store, "--all", "--out", out)
    assert done.returncode == 0, done.stderr
    assert out.read_text(encoding="utf-8") == lines_of(expected_rows())


def test_a_blobless_clone_gives_the_same_rows(clones: Path, tmp_path: Path) -> None:
    source = clones / "alpha"
    subprocess.run(
        ["git", "-C", str(source), "config", "uploadpack.allowFilter", "true"],
        check=True,
        env={**os.environ, **ISOLATION},
    )
    store = tmp_path / "blobless"
    store.mkdir()
    cloned = subprocess.run(
        ["git", "clone", "-q", "--bare", "--filter=blob:none", f"file://{source}", "alpha.git"],
        cwd=store,
        check=False,
        capture_output=True,
        env={**os.environ, **ISOLATION},
    )
    if cloned.returncode != 0:
        pytest.skip("this git cannot make a blobless clone")
    out = tmp_path / "hints.jsonl"
    done = run_tool("--repos-dir", store, "--all", "--out", out)
    assert done.returncode == 0, done.stderr
    assert out.read_text(encoding="utf-8") == lines_of(expected_rows())


# --- the wanted list ----------------------------------------------------------------------------


def test_only_wanted_files_are_emitted_in_their_own_spelling(clones: Path, tmp_path: Path) -> None:
    decomposed = unicodedata.normalize("NFD", "dir with space/ünï.md")
    wanted = tmp_path / "wanted.jsonl"
    wanted.write_text(
        "".join(
            json.dumps({"repo": "alpha", "path": path}, ensure_ascii=False) + "\n"
            for path in ("docs/both.md", decomposed, "docs/never-existed.md", "docs/both.md")
        ),
        encoding="utf-8",
    )
    out = tmp_path / "hints.jsonl"
    done = run_tool("--repos-dir", clones, "--wanted", wanted, "--out", out)
    assert done.returncode == 0, done.stderr
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert [row["path"] for row in rows] == sorted([decomposed, "docs/both.md"])
    assert {row["origin"] for row in rows} == {"human", "mixed"}
    assert "1 wanted files without history" in done.stderr


@pytest.mark.parametrize(
    "line",
    ["not json", "[1]", '{"repo": "alpha"}', '{"repo": "alpha", "path": "../x"}', '{"path": "x"}'],
)
def test_a_malformed_wanted_row_is_refused(clones: Path, tmp_path: Path, line: str) -> None:
    wanted = tmp_path / "wanted.jsonl"
    wanted.write_text(line + "\n", encoding="utf-8")
    out = tmp_path / "hints.jsonl"
    done = run_tool("--repos-dir", clones, "--wanted", wanted, "--out", out)
    assert done.returncode == 2
    assert "wanted line 1" in done.stderr
    assert not out.exists()


def test_a_missing_repository_fails_with_its_name_only_and_writes_nothing(
    clones: Path, tmp_path: Path
) -> None:
    wanted = tmp_path / "wanted.jsonl"
    wanted.write_text(
        '{"repo": "alpha", "path": "feat.md"}\n{"repo": "ghost", "path": "x.md"}\n'
        '{"repo": "phantom", "path": "y.md"}\n',
        encoding="utf-8",
    )
    out = tmp_path / "hints.jsonl"
    done = run_tool("--repos-dir", clones, "--wanted", wanted, "--out", out)
    assert done.returncode == 2
    assert "ghost" in done.stderr and "phantom" in done.stderr
    assert "alpha" not in done.stderr
    assert not out.exists()


def test_a_directory_that_is_not_a_repository_fails_with_exit_three(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    wanted = tmp_path / "wanted.jsonl"
    wanted.write_text('{"repo": "plain", "path": "x.md"}\n', encoding="utf-8")
    out = tmp_path / "hints.jsonl"
    done = run_tool("--repos-dir", tmp_path, "--wanted", wanted, "--out", out)
    assert done.returncode == 3
    assert "plain" in done.stderr
    assert not out.exists()


def test_a_git_failure_leaves_standard_output_empty_even_after_good_repositories(
    clones: Path, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(clones / "alpha"), str(store / "alpha.git")],
        check=True,
        capture_output=True,
        env={**os.environ, **ISOLATION},
    )
    (store / "zeta").mkdir()
    (store / "zeta" / ".git").mkdir()
    done = run_tool("--repos-dir", store, "--all")
    assert done.returncode == 3
    assert done.stdout == ""
    assert "zeta" in done.stderr


def test_a_repository_without_commits_gives_no_rows(tmp_path: Path) -> None:
    store = tmp_path / "store"
    (store / "empty").mkdir(parents=True)
    git(store / "empty", "init", "-q", "-b", "main")
    done = run_tool("--repos-dir", store, "--all")
    assert done.returncode == 0, done.stderr
    assert done.stdout == ""


def test_wanted_and_all_are_exclusive_and_one_is_required(clones: Path) -> None:
    assert run_tool("--repos-dir", clones).returncode == 2
    assert run_tool("--repos-dir", clones, "--all", "--wanted", "x.jsonl").returncode == 2


# --- limits and privacy -------------------------------------------------------------------------


def test_the_commit_limit_reads_the_newest_commits_and_says_so(
    clones: Path, tmp_path: Path
) -> None:
    out = tmp_path / "hints.jsonl"
    done = run_tool("--repos-dir", clones, "--all", "--max-commits-per-repo", "1", "--out", out)
    assert done.returncode == 0, done.stderr
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert [row["path"] for row in rows] == ["feat.md"]
    assert rows[0]["signals"] == sorted([*CLAUDE_SIGNALS, "window:max-commits"])


def test_a_limit_is_a_positive_integer(clones: Path) -> None:
    assert run_tool("--repos-dir", clones, "--all", "--max-commits-per-repo", "0").returncode == 2


def test_standard_error_holds_counts_never_commit_text_or_identities(
    clones: Path, tmp_path: Path
) -> None:
    done = run_tool("--repos-dir", clones, "--all", "--out", tmp_path / "hints.jsonl")
    assert done.returncode == 0
    assert "9 commits, 11 files" in done.stderr
    for secret in (SENTINEL_TEXT, SENTINEL_NAME, SENTINEL_EMAIL, "Alice", "alice@", "Claude"):
        assert secret not in done.stderr
    assert SENTINEL_TEXT not in (tmp_path / "hints.jsonl").read_text(encoding="utf-8")


def test_the_output_never_holds_an_address_or_a_name(clones: Path) -> None:
    done = run_tool("--repos-dir", clones, "--all")
    for secret in (SENTINEL_NAME, SENTINEL_EMAIL, "alice@", "pat@", "49699333"):
        assert secret not in done.stdout


# --- the classification ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "labels"),
    [
        ("Claude", {"claude"}),
        ("claude-code", {"claude"}),
        ("Claudette", set()),
        ("copilots", set()),
        ("Anthropic Support", {"anthropic"}),
        ("noreply@anthropic.com", {"anthropic", "noreply@anthropic.com"}),
        ("github-actions[bot]", {"bot", "github-actions"}),
        ("41898282+github-actions[bot]@users.noreply.github.com", {"bot", "github-actions"}),
        ("GitHub Actions", {"github-actions"}),
        ("github", set()),
        ("renovate[bot]", {"bot", "renovate"}),
        ("ChatGPT", {"chatgpt"}),
        ("OpenAI Codex", {"openai", "codex"}),
        ("Gemini CLI", {"gemini"}),
        ("opencode", {"opencode"}),
        ("Cursor Agent", {"cursor"}),
        ("antigravity", {"antigravity"}),
        ("aider (gpt-4)", {"aider"}),
        ("Devin AI", {"devin"}),
        ("Jules", {"jules"}),
        ("Alice Example", set()),
        ("", set()),
    ],
)
def test_ai_names_match_as_whole_words(text: str, labels: set[str]) -> None:
    assert load_tool().ai_labels(text) == frozenset(labels)


@pytest.mark.parametrize(
    ("message", "signals"),
    [
        ("plain\n", set()),
        (f"x\n\n{CLAUDE_TRAILER}\n", set(CLAUDE_SIGNALS)),
        ("x\n\nco-authored-by: GEMINI <g@example.test>", {"trailer:co-authored-by:gemini"}),
        ("x\n\nCO-AUTHORED-BY: Codex", {"trailer:co-authored-by:codex"}),
        ("x\n\nCo-authored-by: Pat Example <pat@example.test>", set()),
        (f"x\n\n{CLAUDE_TRAILER}\n\nmore text after", set(CLAUDE_SIGNALS)),
        (
            f"first\n\n{CLAUDE_TRAILER}\n\nsecond squashed message\n\nSigned-off-by: A",
            set(CLAUDE_SIGNALS),
        ),
        ("> Co-authored-by: Claude <noreply@anthropic.com>", set()),
        (f"x\n\n{FOOTER}", {"footer:generated-with"}),
        ("x\n\nGenerated with [Claude Code]", {"footer:generated-with"}),
        (
            f"{FOOTER}\n\nSigned-off-by: Alice Example <alice@example.test>",
            {"footer:generated-with"},
        ),
        ("Generated with a template\n", set()),
        ("", set()),
    ],
)
def test_trailers_and_footers(message: str, signals: set[str]) -> None:
    assert load_tool().message_signals(message) == frozenset(signals)


def test_an_empty_message_and_empty_identities_do_not_break_the_stream(tmp_path: Path) -> None:
    repo = tmp_path / "store" / "quiet"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.md").write_text("x", encoding="utf-8")
    git(repo, "add", "-A")
    env = {
        "GIT_AUTHOR_NAME": "A",
        "GIT_AUTHOR_EMAIL": "",
        "GIT_COMMITTER_NAME": "A",
        "GIT_COMMITTER_EMAIL": "",
    }
    git(repo, "commit", "-q", "--allow-empty-message", "-m", "", env=env)
    (repo / "b.md").write_text("x", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty-message", "-m", "", env=env)
    done = run_tool("--repos-dir", tmp_path / "store", "--all")
    assert done.returncode == 0, done.stderr
    assert [json.loads(line)["path"] for line in done.stdout.splitlines()] == ["a.md", "b.md"]
