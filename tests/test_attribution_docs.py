"""The command lines in the authorship-rules recipes of docs/attributed-knowledge.md really run.

The fenced `bash` blocks of the "Deterministic authorship rules" section are parsed, their paths
are replaced by temporary ones and every `cce library attribute ...` line is executed against a
synthetic library, in order, so that a recipe that stops working (a renamed option, a changed
invocation, a step that can no longer be done in that order) fails here.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import pytest
from attribution_support import build_library

from conversation_corpus_engine.attribution_commands import handle_attribute
from conversation_corpus_engine.cli import build_parser

DOCS = Path(__file__).resolve().parents[1] / "docs" / "attributed-knowledge.md"
SECTION = "## Deterministic authorship rules"
FENCE = re.compile(r"^```bash\n(.*?)^```$", re.DOTALL | re.MULTILINE)
PLACEHOLDER = re.compile(r"/private/[\w./-]*[\w-]")
LIBRARY_PLACEHOLDER = "/private/store/library-all"


def recipe_blocks() -> list[str]:
    text = DOCS.read_text(encoding="utf-8")
    assert SECTION in text
    return [match.group(1) for match in FENCE.finditer(text[text.index(SECTION) :])]


def steps(block: str) -> list[tuple[str, str]]:
    """The block as ("command", line) and ("fill", comment) steps; a trailing backslash joins
    the next line, and any other comment is dropped."""
    result: list[tuple[str, str]] = []
    pending = ""
    for raw in block.splitlines():
        line = raw.strip()
        if pending:
            line = pending + " " + line
            pending = ""
        if line.endswith("\\"):
            pending = line[:-1].rstrip()
            continue
        if not line:
            continue
        if line.startswith("#"):
            if "fill in" in line.lower():
                result.append(("fill", line))
            continue
        result.append(("command", line))
    assert not pending
    return result


def test_the_recipe_blocks_exist_and_use_options_the_commands_have() -> None:
    blocks = recipe_blocks()
    assert len(blocks) >= 2
    joined = "\n".join(blocks)
    for fragment in (
        "review-sample",
        "review-record",
        "--sample",
        "--answers",
        "--owner-id",
        "--ledger",
        "apply",
        "--report",
    ):
        assert fragment in joined, fragment


def test_every_attribute_command_line_in_the_recipes_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "lib" / "gen", typed_records=40)
    outputs = tmp_path / "out"
    drawn: list[Path] = []
    executed: list[str] = []
    skipped: list[str] = []

    def substitute(match: re.Match[str]) -> str:
        found = match.group(0)
        if found == LIBRARY_PLACEHOLDER:
            return str(library.parent)
        return str(outputs / Path(found).name)

    def place(line: str) -> str:
        # One pass: a substituted path (which may itself start with /private/, as every
        # temporary directory does on macOS) is never matched again.
        return PLACEHOLDER.sub(substitute, line)

    for block in recipe_blocks():
        for kind, line in steps(block):
            if kind == "fill":
                # The owner opens the sample file and writes a verdict on each row.
                assert drawn, "a recipe asks for answers before it draws a sample"
                lines = drawn[-1].read_text(encoding="utf-8").splitlines()
                rows = [json.loads(row) for row in lines[1:]]
                for row in rows:
                    row["verdict"] = "yes" if row["rule"] == "typed_prefix" else "unsure"
                body = [lines[0], *(json.dumps(row, sort_keys=True) for row in rows)]
                drawn[-1].write_text("\n".join(body) + "\n", encoding="utf-8")
                continue
            words = shlex.split(place(line))
            assert words[0] == "cce", line
            if words[1:3] != ["library", "attribute"]:
                # `cce library profile ...` is another command, with its own tests.
                assert words[1:3] == ["library", "profile"], line
                skipped.append(line)
                continue
            if "review-sample" in words and "--out" in words:
                drawn.append(Path(words[words.index("--out") + 1]))
            args = build_parser().parse_args(words[1:])
            code = handle_attribute(args)
            _, err = capsys.readouterr()
            assert code == 0, (line, err)
            executed.append(line)
    actions = [word for line in executed for word in line.split() if word in ACTIONS]
    # The whole path: draw, answer, record, draw again against the ledger, report, export.
    assert actions.count("review-sample") >= 2 and actions.count("review-record") >= 2
    assert "apply" in actions and any("--report" in line for line in executed)
    assert any(line for line in executed if "--sample" in line and same_file_twice(line))
    assert len(executed) >= 6
    assert all("profile" in line for line in skipped)


ACTIONS = ("review-sample", "review-record", "apply")


def same_file_twice(line: str) -> bool:
    """`--sample X --answers X`: the sample file, filled in, is also the answers file."""
    words = shlex.split(line)
    return (
        "--sample" in words
        and "--answers" in words
        and words[words.index("--sample") + 1] == words[words.index("--answers") + 1]
    )
