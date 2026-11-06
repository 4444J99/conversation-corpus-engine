"""The import modules' own command lines write a provider's conversations as plaintext into
`--output-root`, which defaults to the working directory. They are the one place text could land in a
folder Git can see without a registered store in the way, so each refuses such a folder before it
reads or writes anything. Every repository and string here is synthetic.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from test_git_private_guard import hermetic_git, make_repo  # noqa: F401  (hermetic_git is autouse)

from conversation_corpus_engine import corpus_store

# module, the function its main() calls, and the arguments before --output-root
MAINS = {
    "chatgpt-export": ("import_chatgpt_export_corpus", "import_chatgpt_export_corpus", ["bundle"]),
    "claude-export": ("import_claude_export_corpus", "import_claude_export_corpus", ["bundle"]),
    "document-export": (
        "import_document_export_corpus",
        "import_document_export_corpus",
        ["gemini", "bundle"],
    ),
    "markdown-document": (
        "import_markdown_document_corpus",
        "import_markdown_document_corpus",
        ["bundle"],
    ),
    "claude-local-session": (
        "import_claude_local_session_corpus",
        "import_claude_local_session_corpus",
        [],
    ),
}


class Entry:
    """One import module whose function is replaced by a recorder, so no export is needed."""

    def __init__(self, monkeypatch, capsys, name: str) -> None:
        module_name, function, self.arguments = MAINS[name]
        self.module = importlib.import_module(f"conversation_corpus_engine.{module_name}")
        self.monkeypatch, self.capsys = monkeypatch, capsys
        self.calls: list[Path] = []

        def recorder(source: Path, output_root: Path, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(output_root)
            return {"corpus": "synthetic"}

        monkeypatch.setattr(self.module, function, recorder)

    def run(self, *extra: str) -> tuple[int, str, str]:
        self.monkeypatch.setattr(sys, "argv", [self.module.__name__, *self.arguments, *extra])
        code = self.module.main()
        captured = self.capsys.readouterr()
        return code, captured.out, captured.err


@pytest.fixture(params=sorted(MAINS))
def entry(request, monkeypatch, capsys) -> Entry:
    return Entry(monkeypatch, capsys, request.param)


def test_a_git_visible_output_root_is_refused_before_anything_is_read(entry, tmp_path) -> None:
    repo = make_repo(tmp_path, gitignore=".*\n")  # only dotfiles ignored: a corpus would show
    destination = repo / "corpus"
    code, out, err = entry.run("--output-root", str(destination))
    assert code == 2 and out == ""
    assert "Git-visible" in err
    assert entry.calls == [] and not destination.exists()


def test_the_default_output_root_is_judged_like_an_explicit_one(entry, tmp_path) -> None:
    """With no --output-root the corpus lands in the working directory."""
    repo = make_repo(tmp_path, gitignore="")
    entry.monkeypatch.setattr(entry.module, "DEFAULT_OUTPUT_ROOT", repo / "default-corpus")
    code, _, err = entry.run()
    assert code == 2 and "Git-visible" in err
    assert entry.calls == [] and not (repo / "default-corpus").exists()


def test_the_working_directory_itself_is_not_a_private_default(entry, tmp_path) -> None:
    repo = make_repo(tmp_path, gitignore="")
    code, _, err = entry.run("--output-root", str(repo))
    assert code == 2 and "Git-visible" in err and entry.calls == []


def test_an_output_root_outside_every_work_tree_is_used(entry, tmp_path) -> None:
    destination = tmp_path / "plain" / "corpus"
    code, out, _ = entry.run("--output-root", str(destination))
    assert code == 0 and json.loads(out) == {"corpus": "synthetic"}
    assert entry.calls == [destination]


def test_an_ignored_directory_inside_a_work_tree_is_used(entry, tmp_path) -> None:
    repo = make_repo(tmp_path, gitignore="private/\n")
    destination = repo / "private" / "corpus"
    code, _, _ = entry.run("--output-root", str(destination))
    assert code == 0 and entry.calls == [destination]


def test_a_git_that_cannot_be_asked_is_a_refusal_not_a_pass(entry, tmp_path) -> None:
    repo = make_repo(tmp_path, gitignore="private/\n")

    def broken(directory: Path) -> Path:
        raise corpus_store.CorpusStoreError("git could not inspect the directory")

    entry.monkeypatch.setattr(corpus_store, "require_git_private", broken)
    code, _, err = entry.run("--output-root", str(repo / "private" / "corpus"))
    assert code == 2 and "git could not inspect" in err and entry.calls == []
