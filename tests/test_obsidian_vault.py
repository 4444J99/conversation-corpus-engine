"""Behavioral checks for a bounded, reversible Obsidian reading derivative."""

import importlib.util
import json
import re
import sqlite3
from pathlib import Path

import pytest
from markdown_it import MarkdownIt


@pytest.fixture
def packer():
    path = Path(__file__).parents[1] / "scripts/package-obsidian-vault.py"
    assert path.exists(), "the bounded Obsidian packager has not been implemented"
    spec = importlib.util.spec_from_file_location("obsidian_packer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build(packer, tmp_path, files, **kwargs):
    source = tmp_path / "source"
    source.mkdir()
    for relative, content in files.items():
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    output = tmp_path / "derived"
    result = packer.package(
        source,
        output,
        max_note_bytes=4096,
        max_output_bytes=10_000_000,
        max_seconds=30,
        **kwargs,
    )
    return source, output, result


def test_splits_every_kind_and_recovers_exact_bytes(packer, tmp_path):
    files = {
        "Home.md": b"# Home\r\n\r\n" + b"- entry\r\n" * 1800,
        "records/large.md": ("# Record\n\n" + "naive \U0001f30d text\n" * 1900).encode(),
        "blocks/shared.md": b"# Passage\n\n" + b"word " * 7000,
        "hubs/all.md": b"# All\n\n" + b"[Large](../records/large.md)\n" * 1500,
        "small.md": b"# Unchanged\r\n\xff",
        "data.csv": b"a,b\r\n1,2\r\n",
    }
    # Invalid UTF-8 is rejected for Markdown; binary evidence remains copyable.
    files["small.md"] = b"# Unchanged\r\n"
    files["data.bin"] = b"\xff\x00"
    source, output, result = build(packer, tmp_path, files)
    assert result["status"] == "verified"
    assert result["split_notes"] == 4
    for relative, original in files.items():
        assert source.joinpath(relative).read_bytes() == original
        assert b"".join(packer.reconstruct(output, relative)) == original
    assert all(path.stat().st_size <= 4096 for path in output.rglob("*.md"))
    assert packer.verify_package(output)["source_files"] == len(files)
    assert all(path.stat().st_mode & 0o077 == 0 for path in output.rglob("*"))


def test_message_links_follow_the_correct_part(packer, tmp_path):
    large = b"# Record\n\n## Message 1\n\n" + b"body\n" * 1400
    large += b"\n## Message 2\n\nLast message.\n[First](#message-1)\n"
    files = {
        "records/a.md": large,
        "blocks/b.md": b"[Second](../records/a.md#message-2)\n",
        "Home.md": b"[Record](records/a.md)\n",
    }
    _, output, _ = build(packer, tmp_path, files)
    backlink = output.joinpath("blocks/b.md").read_text()
    assert "a.__part" in backlink and "#message-2" in backlink
    target = backlink.split("](", 1)[1].split(")", 1)[0].split("#", 1)[0]
    part = (output / "blocks" / target).resolve()
    assert "## Message 2" in part.read_text()
    assert "[First](a.__part000001.md#message-1)" in part.read_text()
    assert "[Record](records/a.md)" in output.joinpath("Home.md").read_text()
    assert b"".join(packer.reconstruct(output, "blocks/b.md")) == files["blocks/b.md"]


def test_fences_are_balanced_and_literal_links_unchanged(packer, tmp_path):
    text = b"# Code\n\n```python\n" + b"[literal](other.md#message-2)\n" * 500
    text += b"```\n\n~~~text\n" + b"line\n" * 800 + b"~~~\n"
    other = b"# Other\n" + b"x\n" * 5000 + b"\n## Message 2\n"
    _, output, _ = build(packer, tmp_path, {"code.md": text, "other.md": other})
    parts = sorted(output.glob("code.__part*.md"))
    assert len(parts) > 3
    for path in parts:
        rendered = path.read_text()
        assert packer.fence_state(rendered, None, True) is None
        assert "other.__part" not in rendered
    assert b"".join(packer.reconstruct(output, "code.md")) == text


def test_agent_entry_links_are_relocated_and_reversible(packer, tmp_path):
    agent = tmp_path / "agent-entry.md"
    agent.write_text("# Agent entry\n")
    content = b"[Agent](../agent-entry.md)\n"
    _, output, _ = build(packer, tmp_path, {"Home.md": content}, agent_entry=agent)
    assert output.joinpath("Home.md").read_text() == "[Agent](agent-entry.md)\n"
    assert output.joinpath("agent-entry.md").read_text() == "# Agent entry\n"
    assert b"".join(packer.reconstruct(output, "Home.md")) == content


def test_refuses_existing_or_nested_output_and_symlink(packer, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    source.joinpath("Home.md").write_text("# Home\n")
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(ValueError, match="exist"):
        packer.package(source, existing)
    with pytest.raises(ValueError, match="inside|overlap"):
        packer.package(source, source / "nested")
    source.joinpath("link.md").symlink_to(source / "Home.md")
    with pytest.raises(ValueError, match="symlink"):
        packer.package(source, tmp_path / "new")


def test_invalid_utf8_and_output_budget_do_not_claim_success(packer, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    source.joinpath("bad.md").write_bytes(b"\xff")
    with pytest.raises(UnicodeError):
        packer.package(source, tmp_path / "invalid")
    source.joinpath("bad.md").write_bytes(b"x" * 10000)
    with pytest.raises(ValueError, match="budget"):
        packer.package(source, tmp_path / "tiny", max_output_bytes=1024)
    assert not (tmp_path / "tiny/.cce-package/manifest.json").exists()


def test_verifier_detects_part_tampering(packer, tmp_path):
    _, output, _ = build(packer, tmp_path, {"large.md": b"long text\n" * 2000})
    part = next(output.glob("large.__part*.md"))
    part.write_bytes(part.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="hash|digest|size"):
        packer.verify_package(output)


def test_runtime_state_is_explicitly_excluded_from_source_evidence(packer, tmp_path):
    _, output, result = build(
        packer, tmp_path, {"Home.md": b"# Home\n", ".obsidian/workspace.json": b"{}"}
    )
    assert not output.joinpath(".obsidian").exists()
    assert result["excluded_runtime_directories"] == [".obsidian"]
    receipt = json.loads(output.joinpath(".cce-package/manifest.json").read_text())
    assert receipt["source_unchanged"] is True


def test_dense_fragment_links_fit_and_all_reach_the_anchor(packer, tmp_path):
    name = "a" * 180 + ".md"
    raw = b"# A\n\n" + b"[A](#a)\n" * 1000
    _, output, _ = build(packer, tmp_path, {name: raw})
    for path in output.glob("*.__part*.md"):
        content = path.read_text()
        assert path.stat().st_size <= 4096
        assert "[A](#a)" not in content
    assert b"".join(packer.reconstruct(output, name)) == raw


def test_failed_tiny_budget_never_writes_more_than_the_cap(packer, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    source.joinpath("Home.md").write_text("# Home\n")
    output = tmp_path / "tiny"
    with pytest.raises(ValueError, match="budget"):
        packer.package(source, output, max_output_bytes=1)
    assert sum(path.stat().st_size for path in output.rglob("*") if path.is_file()) <= 1


def test_inline_code_requires_equal_backtick_runs(packer, tmp_path):
    literal = b"`a``` [literal](other.md#message-2) z`\n"
    literal += b"``a` [literal](other.md#message-2) z``\n"
    literal += b"\\` [real](other.md#message-2) \\`\n"
    other = b"# Other\n" + b"text\n" * 900 + b"\n## Message 2\n\nlast\n"
    _, output, _ = build(packer, tmp_path, {"code.md": literal, "other.md": other})
    result = output.joinpath("code.md").read_bytes().splitlines()
    assert result[:2] == literal.splitlines()[:2]
    assert b"other.__part" in result[2]
    assert b"".join(packer.reconstruct(output, "code.md")) == literal


def test_configurable_source_cap_rejects_before_reading(packer, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    source.joinpath("large.md").write_bytes(b"a" * 100)
    with pytest.raises(ValueError, match="source.*byte"):
        packer.package(source, tmp_path / "bounded", max_source_bytes=50)


def test_quoted_fences_and_indented_code_remain_literal(packer, tmp_path):
    literal = b"> ```text\n" + b"> [literal](other.md#message-2)\n" * 300
    literal += b"> ```\n\n>     [indented](other.md#message-2)\n"
    literal += b"\n[Real](other.md#message-2)\n"
    other = b"# Other\n" + b"x\n" * 2500 + b"\n## Message 2\n"
    _, output, _ = build(packer, tmp_path, {"code.md": literal, "other.md": other})
    rendered = "\n".join(path.read_text() for path in sorted(output.glob("code.__part*.md")))
    assert "> [literal](other.__part" not in rendered
    assert ">     [indented](other.__part" not in rendered
    assert "[Real](other.__part" in rendered
    for path in output.glob("code.__part*.md"):
        assert packer.fence_state(path.read_text()) is None
    assert b"".join(packer.reconstruct(output, "code.md")) == literal


def test_navigation_growth_obeys_the_output_cap_before_sqlite_writes(packer, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    source.joinpath("many.md").write_text(
        "".join(f"## Message {number}\n\nbody\n" for number in range(5000))
    )
    output = tmp_path / "bounded"
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        packer.package(source, output, max_note_bytes=4096, max_output_bytes=16384)
    actual = sum(path.stat().st_size for path in output.rglob("*") if path.is_file())
    assert actual <= 16384
    assert not output.joinpath(".cce-package/manifest.json").exists()


def test_verifier_ignores_native_settings_but_checks_preservation_metadata(packer, tmp_path):
    _, output, _ = build(packer, tmp_path, {"Home.md": b"# Home\n"})
    settings = output / ".obsidian"
    settings.mkdir(mode=0o755)
    settings.joinpath("workspace.json").write_text("{}")
    assert packer.verify_package(output)["status"] == "verified"
    inventory = output / ".cce-package/sources.jsonl"
    inventory.write_bytes(inventory.read_bytes() + b"{}\n")
    with pytest.raises(ValueError, match="inventory.*digest"):
        packer.verify_package(output)


def test_refuses_output_anywhere_inside_canonical_generation(packer, tmp_path):
    generation = tmp_path / "library-all/generations/generation-fixture"
    source = generation / "vault"
    source.mkdir(parents=True)
    source.joinpath("Home.md").write_text("# Home\n")
    with pytest.raises(ValueError, match="canonical generation"):
        packer.package(source, generation / "derived")


def test_refuses_output_inside_a_different_canonical_generation(packer, tmp_path):
    generations = tmp_path / "library-all/generations"
    source = generations / "generation-source/vault"
    source.mkdir(parents=True)
    source.joinpath("Home.md").write_text("# Home\n")
    other = generations / "generation-other"
    other.mkdir()
    with pytest.raises(ValueError, match="canonical generation"):
        packer.package(source, other / "derived")


@pytest.mark.parametrize("ticks", ["`", "``", "```"])
def test_split_inline_code_keeps_images_literal_in_every_part(packer, tmp_path, ticks):
    image = "![pixel](https://example.invalid/tracker)"
    raw = ("prefix " + ticks + "a" * 2047 + image + "b" * 2200 + ticks).encode()
    _, output, _ = build(packer, tmp_path, {"note.md": raw})
    found = 0
    for path in sorted(output.glob("note.__part*.md")):
        rendered = path.read_text()
        runs = list(re.finditer(r"`+", rendered))
        assert len(runs) == 2
        assert runs[0][0] == runs[1][0] == ticks
        assert packer.fence_state(rendered) is None
        if image in rendered:
            position = rendered.index(image)
            assert runs[0].end() <= position < position + len(image) <= runs[1].start()
            found += 1
    assert found == 1
    assert b"".join(packer.reconstruct(output, "note.md")) == raw


def test_split_never_cuts_through_a_backtick_delimiter(packer, tmp_path):
    raw = b"x" * 2046 + b"```" + b"body" * 1400 + b"```"
    _, output, _ = build(packer, tmp_path, {"note.md": raw})
    row = json.loads(output.joinpath(".cce-package/ledger.jsonl").read_text())
    for piece in row["pieces"]:
        assert piece["source_end"] not in {2047, 2048, len(raw) - 2, len(raw) - 1}
    assert b"".join(packer.reconstruct(output, "note.md")) == raw


def test_multiline_inline_code_preserves_literal_images_across_parts(packer, tmp_path):
    raw = (
        b"prefix `first\n"
        + b"a" * 2047
        + b"![pixel](https://example.invalid/)"
        + b"b" * 2200
        + b"\nlast`\n"
    )
    _, output, _ = build(packer, tmp_path, {"note.md": raw})
    parser = MarkdownIt("commonmark")
    for path in output.glob("note.__part*.md"):
        queue = parser.parse(path.read_text())
        while queue:
            token = queue.pop()
            assert token.type != "image"
            queue.extend(token.children or [])
    assert b"".join(packer.reconstruct(output, "note.md")) == raw
