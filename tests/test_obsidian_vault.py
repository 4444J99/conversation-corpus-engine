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
    kwargs.setdefault("source_generation", "test-generation")
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
        packer.package(source, existing, source_generation="test-generation")
    with pytest.raises(ValueError, match="inside|overlap"):
        packer.package(source, source / "nested", source_generation="test-generation")
    source.joinpath("link.md").symlink_to(source / "Home.md")
    with pytest.raises(ValueError, match="symlink"):
        packer.package(source, tmp_path / "new", source_generation="test-generation")


def test_invalid_utf8_and_output_budget_do_not_claim_success(packer, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    source.joinpath("bad.md").write_bytes(b"\xff")
    with pytest.raises(UnicodeError):
        packer.package(source, tmp_path / "invalid", source_generation="test-generation")
    source.joinpath("bad.md").write_bytes(b"x" * 10000)
    with pytest.raises(ValueError, match="budget"):
        packer.package(
            source, tmp_path / "tiny", max_output_bytes=1024, source_generation="test-generation"
        )
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
        packer.package(source, output, max_output_bytes=1, source_generation="test-generation")
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
        packer.package(
            source, tmp_path / "bounded", max_source_bytes=50, source_generation="test-generation"
        )


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
        packer.package(
            source,
            output,
            max_note_bytes=4096,
            max_output_bytes=16384,
            source_generation="test-generation",
        )
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
        packer.package(source, generation / "derived", source_generation="test-generation")


def test_refuses_output_inside_a_different_canonical_generation(packer, tmp_path):
    generations = tmp_path / "library-all/generations"
    source = generations / "generation-source/vault"
    source.mkdir(parents=True)
    source.joinpath("Home.md").write_text("# Home\n")
    other = generations / "generation-other"
    other.mkdir()
    with pytest.raises(ValueError, match="canonical generation"):
        packer.package(source, other / "derived", source_generation="test-generation")


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


def test_raw_unicode_navigation_targets_stay_within_rendered_byte_limit(packer, tmp_path):
    target = "漢" * 50 + ".md"
    files = {
        target: ("# title\n" + "body " * 1000).encode(),
        "links.md": (("[go](" + target + "#title)\n") * 40).encode(),
    }
    _, output, result = build(packer, tmp_path, files)
    assert result["status"] == "verified"
    assert all(path.stat().st_size <= 4096 for path in output.rglob("*.md"))
    for relative, original in files.items():
        assert b"".join(packer.reconstruct(output, relative)) == original
    rendered = b"".join(path.read_bytes() for path in output.glob("links.__part*.md"))
    assert b"%E6%BC%A2" in rendered
    assert b".__part000001.md#title" in rendered


def test_inline_suffix_after_newline_does_not_start_a_fence(packer, tmp_path):
    raw = (
        b"prefix ```\n[example](https://example.invalid/)\n"
        + b"a" * 1000
        + b"\n"
        + b"b" * 1500
        + b"\nend```\n"
    )
    _, output, result = build(packer, tmp_path, {"record.md": raw})
    assert result["split_notes"] == 1
    parser = MarkdownIt("commonmark", {"html": True})
    for path in output.glob("record.__part*.md"):
        tokens = parser.parse(path.read_text())
        assert not any(token.type == "fence" for token in tokens)
        assert not any(
            child.type == "link_open" and child.attrGet("href") == "https://example.invalid/"
            for token in tokens
            for child in token.children or []
        )
    assert b"".join(packer.reconstruct(output, "record.md")) == raw


def test_manifest_names_source_generation(packer, tmp_path):
    _, output, result = build(
        packer, tmp_path, {"Home.md": b"# Home\n"}, source_generation="20261027"
    )
    assert result["source_generation"] == "20261027"
    receipt = json.loads(output.joinpath(".cce-package/manifest.json").read_text())
    assert receipt["source_generation"] == "20261027"
    assert packer.verify_package(output)["status"] == "verified"


def test_missing_source_generation_is_refused(packer, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    source.joinpath("Home.md").write_text("# Home\n")
    with pytest.raises(TypeError):
        packer.package(source, tmp_path / "derived")


def test_agent_entry_outbound_links_rebase_into_vault(packer, tmp_path):
    generation = tmp_path / "generation"
    source = generation / "vault"
    source.mkdir(parents=True)
    source.joinpath("Home.md").write_text("# Home\n")
    big = b"# Big\n\n## Message 2\n\n" + b"body\n" * 1500
    source.joinpath("big.md").write_bytes(big)
    agent = generation / "agent-entry.md"
    agent.write_text("[Home](vault/Home.md)\n[Big](vault/big.md#message-2)\n[Outside](other.md)\n")
    output = tmp_path / "derived"
    packer.package(
        source,
        output,
        agent_entry=agent,
        source_generation="test-generation",
        max_note_bytes=4096,
        max_output_bytes=10_000_000,
        max_seconds=30,
    )
    entry = output.joinpath("agent-entry.md").read_text()
    assert "[Home](Home.md)" in entry
    assert "big.__part" in entry and "#message-2" in entry
    assert "[Outside](other.md)" in entry
    assert b"".join(packer.reconstruct(output, "agent-entry.md")) == agent.read_bytes()


def test_open_fence_note_fails_closed_but_split_wrapper_closes(packer, tmp_path):
    with pytest.raises(ValueError, match="fence"):
        build(packer, tmp_path, {"note.md": b"# Note\n\n```\ncode\n"})
    split_base = tmp_path / "split"
    split_base.mkdir()
    raw = b"# Note\n\n" + b"body\n" * 1500 + b"```python\n" + b"code\n" * 300
    _, output, _ = build(packer, split_base, {"note.md": raw})
    for path in output.glob("note.__part*.md"):
        assert packer.fence_state(path.read_text()) is None
    assert b"".join(packer.reconstruct(output, "note.md")) == raw


def test_container_fences_are_tracked(packer):
    # PR #115 review corrections: a fence opened on a list item's own line
    # used to be missed, so its closing fence was misread as an opener
    # (a closed note failed, an unclosed one passed).
    assert packer.fence_state("- ```python\n  print(1)\n  ```\n") is None
    assert packer.fence_state("- ```python\n  print(1)\n") is not None
    # A fence on a list continuation line is measured from the item's
    # content indent, so 4 spaces inside a "- " item is a fence, while
    # 6 spaces (indented code past the content) is not.
    assert packer.fence_state("- item\n\n    ```\n    code\n") is not None
    assert packer.fence_state("- item\n\n    ```\n    code\n    ```\n") is None
    assert packer.fence_state("- item\n\n      ```\n      code\n") is None
    # Fence markers inside a raw <pre> block are HTML content, not fences.
    assert packer.fence_state("<pre>\n```\n</pre>\n") is None
    assert packer.fence_state("<pre>\n```\n</pre>\n\n```\ncode\n") is not None
    # Round-2 review corrections: a quoted list fence keeps its quote
    # depth through content and closing lines, and a <pre> block closed
    # on its opening line (or by its closing tag) does not swallow the
    # fences that follow it.
    assert packer.fence_state("> - ```python\n>   print(1)\n>   ```\n") is None
    assert packer.fence_state("> - item\n>\n>     ```\n>     code\n>     ```\n") is None
    assert packer.fence_state("> ```\n> code\n> ```\n- ```\ncode\n```\n") is None
    assert packer.fence_state("<pre>\n</pre>\n```python\ncode\n") is not None
    assert packer.fence_state("<pre>```</pre>\n") is None


def test_list_item_fence_note_packages(packer, tmp_path):
    _, output, _ = build(
        packer, tmp_path, {"note.md": b"# Note\n\n- ```python\n  print(1)\n  ```\n"}
    )
    assert packer.verify_package(output)["status"] == "verified"


def test_verifier_rejects_package_ending_in_open_fence(packer, tmp_path):
    _, output, _ = build(packer, tmp_path, {"Home.md": b"# Home\n"})
    home = output / "Home.md"
    home.write_bytes(home.read_bytes() + b"```\n")
    with pytest.raises(ValueError, match="fence"):
        packer.verify_package(output)
