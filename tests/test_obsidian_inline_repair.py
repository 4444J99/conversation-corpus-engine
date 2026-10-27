"""Repair an existing derivative without altering its source or body bytes."""

import importlib.util
import json
import shutil
from pathlib import Path

import pytest
from markdown_it import MarkdownIt


@pytest.fixture
def repairer(monkeypatch):
    path = Path(__file__).parents[1] / "scripts/repair-obsidian-inline.py"
    spec = importlib.util.spec_from_file_location("inline_repair", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # Exercise the repair contract on every CI host; Darwin clonefile is an
    # independently measured deployment operation, not emulated by this test.
    def copy(source, output):
        with output.open("xb") as stream:
            stream.write(source.read_bytes())
        output.chmod(0o600)

    monkeypatch.setattr(module, "clone_file", copy)
    return module


def fixture(module, tmp_path, ticks=2, newline=None):
    base = module.base
    source = tmp_path / "v1"
    source.mkdir(mode=0o700)
    meta = source / base.META
    meta.mkdir(mode=0o700)
    raw = (
        b"before "
        + b"`" * ticks
        + b"![hidden](https://example.invalid/image) " * 30
        + b"`" * ticks
        + b" after"
    )
    cut = len(raw) // 2
    if newline is not None:
        raw = raw[:cut] + newline + raw[cut:]
        cut += len(newline)
    pieces = []
    for number, (start, end) in enumerate([(0, cut), (cut, len(raw))]):
        body = raw[start:end]
        rendered = b"# Part\n\n" + body + b"\n\n[Home](Home.md)\n"
        path = source / f"part{number}.md"
        path.write_bytes(rendered)
        pieces.append(
            {
                "path": path.name,
                "bytes": len(rendered),
                "sha256": base.digest(rendered),
                "source_start": start,
                "source_end": end,
                "source_sha256": base.digest(body),
                "body_start": 8,
                "body_bytes": len(body),
                "edits": [],
            }
        )
    summary = {"path": "original.md", "bytes": len(raw), "sha256": base.digest(raw)}
    row = {**summary, "pieces": pieces, "indexes": []}
    (meta / "ledger.jsonl").write_bytes(base.encoded(row))
    (meta / "sources.jsonl").write_bytes(base.encoded(summary))
    (meta / "navigation.sqlite").write_bytes(b"navigation fixture")
    manifest = {
        "schema": base.SCHEMA,
        "status": "verified",
        "source_files": 1,
        "source_tree_sha256": base.digest(base.encoded(summary)),
        "max_note_bytes": 4096,
        "rendered_bytes": sum(p["bytes"] for p in pieces),
        "ledger_sha256": base.hash_file(meta / "ledger.jsonl"),
        "source_inventory_sha256": base.hash_file(meta / "sources.jsonl"),
        "navigation_sha256": base.hash_file(meta / "navigation.sqlite"),
    }
    (meta / "manifest.json").write_bytes(base.encoded(manifest))
    for path in source.rglob("*"):
        if path.is_file():
            path.chmod(0o600)
    audit = {
        "schema": "cce.obsidian-inline-boundary-audit.v1",
        "status": "affected",
        "package_manifest_sha256": base.hash_file(meta / "manifest.json"),
        "boundaries_inside_delimiters": 0,
        "affected_notes": 1,
        "affected_parts": 2,
        "crossing_spans": 1,
        "findings": [
            {
                "source_path": "original.md",
                "source_sha256": base.digest(raw),
                "span_start": 7,
                "content_start": 7 + ticks,
                "content_end": len(raw) - 6 - ticks,
                "span_end": len(raw) - 6,
                "delimiter_bytes": ticks,
                "boundaries": [cut],
                "parts": [p["path"] for p in pieces],
            }
        ],
    }
    audit_path = tmp_path / "audit.json"
    audit_path.write_bytes(base.encoded(audit))
    return source, audit_path, raw


def test_repair_preserves_source_reconstructs_all_bytes_and_protects_wrappers(repairer, tmp_path):
    source, audit, raw = fixture(repairer, tmp_path)
    before = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    output = tmp_path / "v2"
    result = repairer.repair(source, output, audit, max_output_bytes=1_000_000, max_seconds=30)
    assert result["repair"]["affected_parts"] == 2
    assert result["repair"]["source_metadata_unchanged"] is True
    assert b"".join(repairer.base.reconstruct(output, "original.md")) == raw
    assert output.joinpath("part0.md").read_bytes().endswith(b" ``\n\n\n[Home](Home.md)\n")
    assert (
        output.joinpath("part1.md").read_bytes().startswith(b"# Part\n\nInline code continued: `` ")
    )
    assert before == {
        str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()
    }
    assert repairer.base.verify_package(output)["source_files"] == 1


def test_repair_refuses_wrong_audit_and_existing_destination(repairer, tmp_path):
    source, audit, _ = fixture(repairer, tmp_path)
    bad = json.loads(audit.read_text())
    bad["package_manifest_sha256"] = "0" * 64
    audit.write_bytes(repairer.base.encoded(bad))
    with pytest.raises(ValueError, match="audit.*manifest"):
        repairer.repair(source, tmp_path / "wrong", audit)
    with pytest.raises(ValueError, match="exist|overlap"):
        repairer.repair(source, source, audit)


def test_repair_bounds_output_before_writes(repairer, tmp_path):
    source, audit, _ = fixture(repairer, tmp_path)
    output = tmp_path / "tiny"
    with pytest.raises(ValueError, match="budget"):
        repairer.repair(source, output, audit, max_output_bytes=1)
    assert sum(p.stat().st_size for p in output.rglob("*") if p.is_file()) <= 1


def test_completed_clone_receipt_is_required_and_bound_to_exact_package(repairer, tmp_path):
    source, audit, raw = fixture(repairer, tmp_path)
    output = tmp_path / "v2"
    shutil.copytree(source, output)
    receipt_path = tmp_path / "clone.json"
    receipt = {
        "schema": "cce.obsidian-cow-clone.v1",
        "status": "running",
        "source": str(source),
        "output": str(output),
        "source_metadata_sha256": {
            n: repairer.base.hash_file(source / repairer.base.META / n) for n in repairer.META_FILES
        },
        "files": sum(p.is_file() for p in output.rglob("*")),
        "logical_bytes": sum(p.stat().st_size for p in output.rglob("*") if p.is_file()),
    }
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="clone not completed"):
        repairer.repair(source, output, audit, clone_receipt=receipt_path)
    receipt["status"] = "cloned"
    receipt_path.write_text(json.dumps(receipt))
    repairer.repair(source, output, audit, clone_receipt=receipt_path)
    assert b"".join(repairer.base.reconstruct(output, "original.md")) == raw


@pytest.mark.parametrize("ticks", [3, 4])
def test_long_inline_delimiters_cannot_become_block_fences(repairer, tmp_path, ticks):
    source, audit, raw = fixture(repairer, tmp_path, ticks=ticks)
    output = tmp_path / "v2"
    repairer.repair(source, output, audit)
    text = output.joinpath("part1.md").read_text()
    assert f"Inline code continued: {'`' * ticks} " in text
    assert repairer.base.fence_state(text) is None
    assert b"".join(repairer.base.reconstruct(output, "original.md")) == raw


@pytest.mark.parametrize("ticks", [3, 4])
@pytest.mark.parametrize("newline", [b"\n", b"\r", b"\r\n"])
def test_inline_suffix_after_newline_keeps_source_images_inert(repairer, tmp_path, ticks, newline):
    source, audit, raw = fixture(repairer, tmp_path, ticks=ticks, newline=newline)
    output = tmp_path / "v2"
    repairer.repair(source, output, audit)
    parser = MarkdownIt("commonmark")
    for number in (0, 1):
        tokens = parser.parse(output.joinpath(f"part{number}.md").read_text())
        assert not any(token.type == "fence" for token in tokens)
        children = [child for token in tokens for child in token.children or []]
        assert not any(child.type == "image" for child in children)
    assert b"".join(repairer.base.reconstruct(output, "original.md")) == raw


def test_failed_repair_never_exposes_completed_manifest(repairer, tmp_path, monkeypatch):
    source, audit, _ = fixture(repairer, tmp_path)
    output = tmp_path / "v2"

    def fail(*args, **kwargs):
        raise ValueError("forced verification failure")

    monkeypatch.setattr(repairer.base, "verify_package", fail)
    with pytest.raises(ValueError, match="forced verification"):
        repairer.repair(source, output, audit)
    assert not (output / repairer.base.META / "manifest.json").exists()
    assert (output / repairer.base.META / "parent-manifest.json").read_bytes() == (
        source / repairer.base.META / "manifest.json"
    ).read_bytes()


def test_repair_cannot_write_into_another_canonical_generation(repairer, tmp_path):
    source, audit, _ = fixture(repairer, tmp_path)
    output = tmp_path / "library-all/generations/other/reading"
    with pytest.raises(ValueError, match="canonical generation"):
        repairer.repair(source, output, audit)
    assert not output.exists()


def test_existing_clone_cannot_alias_the_source(repairer, tmp_path):
    source, audit, _ = fixture(repairer, tmp_path)
    (tmp_path / "other").mkdir()
    with pytest.raises(ValueError, match="alias overlap"):
        repairer.repair(
            source, tmp_path / "other/../v1", audit, clone_receipt=tmp_path / "absent.json"
        )


def test_manifest_sync_failure_never_creates_completion_marker(repairer, tmp_path, monkeypatch):
    source, audit, _ = fixture(repairer, tmp_path)
    output = tmp_path / "v2"

    def fail(_descriptor):
        raise OSError("forced sync failure")

    monkeypatch.setattr(repairer.os, "fsync", fail)
    with pytest.raises(OSError, match="forced sync"):
        repairer.repair(source, output, audit)
    assert not (output / repairer.base.META / "manifest.json").exists()
    assert (output / repairer.base.META / "manifest.inline-repair-tmp").is_file()


def test_existing_repair_reverts_obsolete_wrappers_from_v1_and_preserves_history(
    repairer, tmp_path
):
    source, audit, raw = fixture(repairer, tmp_path)
    output = tmp_path / "v2"
    repairer.repair(source, output, audit)
    before = {str(p.relative_to(output)): p.read_bytes() for p in output.rglob("*") if p.is_file()}
    prior = repairer.base.digest(before[".cce-package/manifest.json"])
    corrected = json.loads(audit.read_text())
    corrected.update(findings=[], crossing_spans=0, affected_notes=0, affected_parts=0)
    audit.write_bytes(repairer.base.encoded(corrected))
    result = repairer.repair(
        source, output, audit, repair_existing=True, prior_manifest_sha256=prior
    )
    assert result["repair"]["affected_parts"] == 0
    assert result["repair"]["rewritten_parts"] == 2
    assert result["repair"]["reverted_parts"] == 2
    assert output.joinpath("part0.md").read_bytes() == source.joinpath("part0.md").read_bytes()
    history = output / result["repair"]["history_path"]
    assert (
        history.joinpath("manifest.json.bin").read_bytes() == before[".cce-package/manifest.json"]
    )
    assert history.joinpath("ledger.jsonl.bin").read_bytes() == before[".cce-package/ledger.jsonl"]
    assert history.joinpath("parts/part0.md.bin").read_bytes() == before["part0.md"]
    assert not list(history.rglob("*.md"))
    assert b"".join(repairer.base.reconstruct(output, "original.md")) == raw


def test_existing_repair_requires_exact_prior_manifest_before_any_mutation(repairer, tmp_path):
    source, audit, _ = fixture(repairer, tmp_path)
    output = tmp_path / "v2"
    repairer.repair(source, output, audit)
    before = {str(p.relative_to(output)): p.read_bytes() for p in output.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="prior manifest"):
        repairer.repair(source, output, audit, repair_existing=True, prior_manifest_sha256="0" * 64)
    assert before == {
        str(p.relative_to(output)): p.read_bytes() for p in output.rglob("*") if p.is_file()
    }


def test_existing_repair_rebuilds_from_original_without_double_wrapping(repairer, tmp_path):
    source, audit, raw = fixture(repairer, tmp_path)
    output = tmp_path / "v2"
    repairer.repair(source, output, audit)
    parts = {p.name: p.read_bytes() for p in output.glob("*.md")}
    prior = repairer.base.hash_file(output / repairer.base.META / "manifest.json")
    repairer.repair(source, output, audit, repair_existing=True, prior_manifest_sha256=prior)
    assert parts == {p.name: p.read_bytes() for p in output.glob("*.md")}
    assert b"".join(repairer.base.reconstruct(output, "original.md")) == raw


def test_existing_repair_failure_preserves_history_and_removes_completion(
    repairer, tmp_path, monkeypatch
):
    source, audit, _ = fixture(repairer, tmp_path)
    output = tmp_path / "v2"
    repairer.repair(source, output, audit)
    marker = output / repairer.base.META / "manifest.json"
    old = marker.read_bytes()
    prior = repairer.base.digest(old)

    def fail(*args, **kwargs):
        raise ValueError("forced final verification failure")

    monkeypatch.setattr(repairer.base, "verify_package", fail)
    with pytest.raises(ValueError, match="forced final"):
        repairer.repair(source, output, audit, repair_existing=True, prior_manifest_sha256=prior)
    assert not marker.exists()
    assert (
        output / repairer.base.META / "repair-history" / prior / "manifest.json.bin"
    ).read_bytes() == old


def test_existing_history_copy_is_charged_to_the_output_cap(repairer, tmp_path):
    source, audit, _ = fixture(repairer, tmp_path)
    output = tmp_path / "v2"
    repairer.repair(source, output, audit)
    marker = output / repairer.base.META / "manifest.json"
    prior = repairer.base.hash_file(marker)
    existing = sum(p.stat().st_size for p in output.rglob("*") if p.is_file())
    maximum = existing + (output / repairer.base.META / "ledger.jsonl").stat().st_size - 1
    with pytest.raises(ValueError, match="budget"):
        repairer.repair(
            source,
            output,
            audit,
            repair_existing=True,
            prior_manifest_sha256=prior,
            max_output_bytes=maximum,
        )
    assert sum(p.stat().st_size for p in output.rglob("*") if p.is_file()) <= maximum
    assert repairer.base.hash_file(marker) == prior
