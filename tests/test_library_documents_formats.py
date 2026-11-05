"""Document format breadth, honest coverage reasons, enumeration and hydration planning.

Everything here uses synthetic files created under ``tmp_path``. The legacy converter is
injected, and no test hydrates, reads or enumerates any real cloud folder.
"""

from __future__ import annotations

import importlib.util
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conversation_corpus_engine import library_commands, library_documents
from conversation_corpus_engine.archive_model import Conversation
from conversation_corpus_engine.cli import build_parser
from conversation_corpus_engine.corpus_store import register_corpus_store
from conversation_corpus_engine.library_build import build_library, publish_library
from conversation_corpus_engine.library_documents import iter_documents
from conversation_corpus_engine.library_store import Library

GIB = 1024**3
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "icloud_hydrate.py"


def source(root: Path, **options: object) -> dict:
    return {"id": "docs", "root": str(root), "include": ["**/*"], "exclude": [], **options}


def fake_converter(calls: list[str] | None = None):
    def convert(data: bytes, suffix: str, maximum: int) -> str:
        if calls is not None:
            calls.append(suffix)
        return f"converted {suffix} body ({len(data)} bytes)"

    return convert


def flag_dataless(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    original = Path.lstat
    dataless = getattr(library_documents.stat, "SF_DATALESS", 0x40000000)

    def flagged(item: Path):
        info = original(item)
        if item.name in names:
            values = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
            values["st_flags"] = dataless
            return SimpleNamespace(**values)
        return info

    monkeypatch.setattr(Path, "lstat", flagged)


def zip_bytes(members: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def epub_bytes(chapters: dict[str, str], spine: list[str]) -> bytes:
    manifest = "".join(
        f'<item id="{name}" href="text/{name}.xhtml" media-type="application/xhtml+xml"/>'
        for name in chapters
    )
    itemrefs = "".join(f'<itemref idref="{name}"/>' for name in spine)
    members: dict[str, str | bytes] = {
        "mimetype": "application/epub+zip",
        "META-INF/container.xml": (
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>'
        ),
        "OEBPS/content.opf": (
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            f"<manifest>{manifest}</manifest><spine>{itemrefs}</spine></package>"
        ),
    }
    for name, html in chapters.items():
        members[f"OEBPS/text/{name}.xhtml"] = html
    return zip_bytes(members)


# --- (1) readers --------------------------------------------------------------------------


def test_doc_and_rtf_are_read_through_the_injected_converter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        library_documents, "_LEGACY_CONVERTER", fake_converter(calls), raising=False
    )
    (tmp_path / "memo.doc").write_bytes(b"\xd0\xcf\x11\xe0 synthetic legacy word bytes")
    (tmp_path / "letter.rtf").write_bytes(rb"{\rtf1\ansi Synthetic letter}")
    rows = {r.provenance["source_file"]: r for r in iter_documents([source(tmp_path)])}
    assert sorted(calls) == [".doc", ".rtf"]
    assert rows["memo.doc"].messages[0].text.startswith("converted .doc body")
    assert rows["letter.rtf"].messages[0].text.startswith("converted .rtf body")
    for row in rows.values():
        assert row.extra["body_status"] == "full"
        assert row.provenance["extractor"] == "textutil@1"


def test_legacy_converter_absence_is_an_explicit_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(library_documents.shutil, "which", lambda *_args, **_kw: None)
    (tmp_path / "memo.doc").write_bytes(b"synthetic")
    report: dict = {}
    row = list(iter_documents([source(tmp_path)], report=report))[0]
    assert row.messages == []
    assert row.extra["body_status"] == "metadata_only"
    assert row.extra["body_reason"] == "legacy_converter_unavailable"
    codes = [f["code"] for f in report["findings"]]
    assert codes == ["legacy_converter_unavailable"]


def test_legacy_converter_failure_is_extraction_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(data: bytes, suffix: str, maximum: int) -> str:
        raise ValueError("converter rejected the document")

    monkeypatch.setattr(library_documents, "_LEGACY_CONVERTER", broken, raising=False)
    (tmp_path / "bad.rtf").write_bytes(b"synthetic")
    row = list(iter_documents([source(tmp_path)]))[0]
    assert row.extra["body_status"] == "metadata_only"
    assert row.extra["body_reason"] == "extraction_error"


def test_epub_follows_spine_order_and_ignores_hidden_markup(tmp_path: Path) -> None:
    doctype = (
        '<?xml version="1.0"?><!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
        '"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">'
    )
    chapters = {
        "alpha": f"{doctype}<html><body><h1>Alpha</h1><p>First manifest entry.</p></body></html>",
        "beta": (
            f"{doctype}<html><head><title>skip</title></head><body><p>Second chapter</p>"
            "<script>secret()</script><p hidden>concealed</p></body></html>"
        ),
    }
    (tmp_path / "book.epub").write_bytes(epub_bytes(chapters, spine=["beta", "alpha"]))
    row = list(iter_documents([source(tmp_path)]))[0]
    text = row.messages[0].text
    assert text.index("Second chapter") < text.index("First manifest entry")
    assert "secret()" not in text and "concealed" not in text and "skip" not in text
    assert row.extra["body_status"] == "full"
    assert row.provenance["extractor"] == "epub-xhtml@1"


def test_invalid_and_oversized_epubs_report_extraction_errors(tmp_path: Path) -> None:
    (tmp_path / "notzip.epub").write_bytes(b"not a zip")
    (tmp_path / "nocontainer.epub").write_bytes(zip_bytes({"mimetype": "application/epub+zip"}))
    bomb = epub_bytes({"huge": "<html><body>" + " " * 60000 + "</body></html>"}, ["huge"])
    (tmp_path / "bomb.epub").write_bytes(bomb)
    report: dict = {}
    rows = {
        r.provenance["source_file"]: r
        for r in iter_documents([source(tmp_path, max_bytes=20000)], report=report)
    }
    assert set(rows) == {"notzip.epub", "nocontainer.epub", "bomb.epub"}
    assert all(r.extra["body_reason"] == "extraction_error" for r in rows.values())
    assert report["counts"]["errors"] == 3


def test_textbundle_folder_is_one_record_and_assets_are_not_ingested(tmp_path: Path) -> None:
    bundle = tmp_path / "note.textbundle"
    (bundle / "assets").mkdir(parents=True)
    (bundle / "text.md").write_text("# Bundle note\n\nBody with 日本語.", encoding="utf-8")
    (bundle / "info.json").write_text('{"version": 2}')
    (bundle / "assets" / "attachment.txt").write_text("asset text must not become a record")
    report: dict = {}
    rows = list(iter_documents([source(tmp_path)], report=report))
    assert [r.provenance["source_file"] for r in rows] == ["note.textbundle"]
    assert rows[0].messages[0].text == "# Bundle note\n\nBody with 日本語."
    assert rows[0].extra["body_status"] == "full"
    assert rows[0].provenance["extractor"] == "textbundle@1"
    assert report["counts"]["matched"] == 1


def test_textbundle_zip_reads_text_inside_top_level_folder(tmp_path: Path) -> None:
    (tmp_path / "packed.textbundle").write_bytes(
        zip_bytes({"packed.textbundle/text.markdown": "Zipped bundle text", "x/ignored.md": "no"})
    )
    (tmp_path / "bare.textpack").write_bytes(zip_bytes({"text.md": "Textpack text"}))
    rows = {r.provenance["source_file"]: r for r in iter_documents([source(tmp_path)])}
    assert rows["packed.textbundle"].messages[0].text == "Zipped bundle text"
    assert rows["bare.textpack"].messages[0].text == "Textpack text"


def test_textbundle_with_symlinked_text_never_follows_the_link(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("outside secret")
    root = tmp_path / "root"
    bundle = root / "linked.textbundle"
    bundle.mkdir(parents=True)
    (bundle / "text.md").symlink_to(outside)
    row = list(iter_documents([source(root)]))[0]
    assert row.messages == []
    assert row.extra["body_status"] == "metadata_only"
    assert row.extra["body_reason"] == "extraction_error"
    assert "outside secret" not in json.dumps(row.to_dict())


def test_dataless_text_inside_a_textbundle_is_not_hydrated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "cloud.textbundle"
    bundle.mkdir()
    (bundle / "text.md").write_text("stand-in")
    flag_dataless(monkeypatch, "text.md")

    def no_read(*_args):
        pytest.fail("a cloud placeholder must not be hydrated")

    monkeypatch.setattr(library_documents, "_safe_bytes", no_read)
    row = list(iter_documents([source(tmp_path)]))[0]
    assert row.extra["body_reason"] == "cloud_placeholder_not_hydrated"


# --- (2) known-unsupported receipts -------------------------------------------------------


@pytest.mark.parametrize("name,fmt", [("sheet.numbers", "numbers"), ("deck.key", "key")])
def test_numbers_and_key_files_get_a_known_unsupported_receipt(
    tmp_path: Path, name: str, fmt: str
) -> None:
    (tmp_path / name).write_bytes(b"PK synthetic iwork archive")
    report: dict = {}
    row = list(iter_documents([source(tmp_path)], report=report))[0]
    assert row.messages == []
    assert row.extra["body_status"] == "metadata_only"
    assert row.extra["body_reason"] == "unsupported_format"
    assert row.provenance["known_unsupported_format"] == fmt
    assert "source_sha256" in row.provenance
    assert [f["code"] for f in report["findings"]] == ["unsupported_format"]
    assert report["findings"][0]["detail"] == fmt


@pytest.mark.parametrize("name,fmt", [("sheet.numbers", "numbers"), ("deck.key", "key")])
def test_package_form_iwork_is_one_receipt_and_not_descended(
    tmp_path: Path, name: str, fmt: str
) -> None:
    package = tmp_path / name
    (package / "Index").mkdir(parents=True)
    (package / "Index" / "Document.txt").write_text("inner text must not become a record")
    row_list = list(iter_documents([source(tmp_path)]))
    assert [r.provenance["source_file"] for r in row_list] == [name]
    assert row_list[0].extra["body_reason"] == "unsupported_format"
    assert row_list[0].provenance["known_unsupported_format"] == fmt


# --- (3) reason histogram -----------------------------------------------------------------


def synthetic_mix(root: Path) -> None:
    (root / "full.md").write_text("A complete synthetic note.")
    (root / "empty.txt").write_text("")
    (root / "image.png").write_bytes(b"PNG")
    (root / "broken.docx").write_bytes(b"not a zip")
    (root / "sheet.numbers").write_bytes(b"PK synthetic")
    (root / "cloud.md").write_text("stand-in")
    (root / "pages-preview.pages").write_bytes(
        zip_bytes({"QuickLook/Preview.txt": "Preview of a synthetic pages file"})
    )
    (root / "memo.doc").write_bytes(b"synthetic legacy")
    (root / "bundle.textbundle").mkdir()
    (root / "bundle.textbundle" / "text.md").write_text("Bundle text")


def test_status_histogram_explains_every_non_full_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(library_documents, "_LEGACY_CONVERTER", fake_converter(), raising=False)
    flag_dataless(monkeypatch, "cloud.md")
    root = tmp_path / "docs"
    root.mkdir()
    synthetic_mix(root)
    with Library.create(tmp_path / "library.sqlite") as library:
        for item in iter_documents([source(root)]):
            library.put(item)
        status = library.status()
    assert status["by_body_status"] == {"full": 3, "partial": 1, "metadata_only": 5}
    assert status["body_reasons"] == {
        "partial": {"preview_only": 1},
        "metadata_only": {
            "empty_body": 1,
            "unsupported_format": 2,
            "extraction_error": 1,
            "cloud_placeholder_not_hydrated": 1,
        },
    }
    assert status["unexplained_non_full"] == 0


def test_non_full_record_without_a_reason_is_counted_as_unexplained(tmp_path: Path) -> None:
    with Library.create(tmp_path / "library.sqlite") as library:
        library.put(
            Conversation(
                provider="codex",
                source_id="no-reason",
                title="Reasonless",
                messages=[],
                provenance={"source_file": "x.jsonl"},
                extra={"wing": "ai", "lane": "coding", "body_status": "metadata_only"},
            )
        )
        status = library.status()
    assert status["unexplained_non_full"] == 1
    assert status["body_reasons"] == {}


# --- (4) container enumeration ------------------------------------------------------------


def test_enumeration_counts_document_files_by_extension_and_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.md").write_text("local")
    (tmp_path / "b.md").write_text("cloud only")
    (tmp_path / "c.pdf").write_bytes(b"%PDF")
    (tmp_path / "d.PDF").write_bytes(b"%PDF upper")
    (tmp_path / "photo.jpg").write_bytes(b"not a document type")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "e.docx").write_bytes(b"zip")
    (tmp_path / "pack.textbundle").mkdir()
    (tmp_path / "pack.textbundle" / "text.md").write_text("inside a package")
    (tmp_path / "link.md").symlink_to(tmp_path / "a.md")
    flag_dataless(monkeypatch, "b.md", "e.docx")

    def no_read(*_args, **_kwargs):
        pytest.fail("enumeration must never read file bytes")

    monkeypatch.setattr(library_documents, "_safe_bytes", no_read)
    summary = library_documents.enumerate_documents(tmp_path)
    assert summary["by_extension"] == {
        ".md": {"local": 1, "dataless": 1},
        ".pdf": {"local": 2, "dataless": 0},
        ".docx": {"local": 0, "dataless": 1},
        ".textbundle": {"local": 1, "dataless": 0},
    }
    assert summary["by_state"] == {"local": 4, "dataless": 2}
    assert summary["files_total"] == 6
    assert summary["dataless_bytes"] == len(b"cloud only") + len(b"zip")


def test_enumeration_lists_dataless_files_for_hydration_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "x.rtf").write_bytes(b"12345")
    (tmp_path / "y.rtf").write_bytes(b"123")
    flag_dataless(monkeypatch, "x.rtf")
    entries = list(library_documents.iter_document_files(tmp_path))
    by_path = {e["path"]: e for e in entries}
    assert by_path["x.rtf"] == {
        "path": "x.rtf",
        "extension": ".rtf",
        "size_bytes": 5,
        "state": "dataless",
        "package": False,
    }
    assert by_path["y.rtf"]["state"] == "local"


# --- (5) hydration planning script --------------------------------------------------------


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location("icloud_hydrate_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_enumerator(entries: list[tuple[str, int, str]]):
    def enumerate_root(root: Path) -> list[dict]:
        return [
            {
                "path": path,
                "extension": Path(path).suffix.lower(),
                "size_bytes": size,
                "state": state,
                "package": False,
            }
            for path, size, state in entries
        ]

    return enumerate_root


def exploding_hydrator(path: Path) -> None:
    pytest.fail(f"plan mode must never hydrate: {path}")


def run_plan(
    module: Any,
    capsys: pytest.CaptureFixture[str],
    entries: list[tuple[str, int, str]],
    free_bytes: int,
    *extra: str,
) -> tuple[int, dict]:
    code = module.main(
        ["--root", "/synthetic/container", "--plan", "--json", *extra],
        enumerator=fake_enumerator(entries),
        disk_usage=lambda _path: SimpleNamespace(free=free_bytes),
        hydrator=exploding_hydrator,
    )
    return code, json.loads(capsys.readouterr().out)


def test_plan_batches_dataless_files_to_at_most_five_gib(
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = load_script()
    entries = [
        ("a.pdf", 3 * GIB, "dataless"),
        ("b.pdf", 2 * GIB, "dataless"),
        ("c.pdf", 2 * GIB, "dataless"),
        ("d.pdf", 4 * GIB, "dataless"),
        ("e.md", 1, "dataless"),
        ("local.md", 9 * GIB, "local"),
    ]
    code, plan = run_plan(module, capsys, entries, free_bytes=100 * GIB)
    assert code == 0
    assert plan["mode"] == "plan"
    assert plan["batch_limit_bytes"] == 5 * GIB
    batches = plan["batches"]
    assert all(b["bytes"] <= 5 * GIB for b in batches)
    planned = sorted(f["path"] for b in batches for f in b["files"])
    assert planned == ["a.pdf", "b.pdf", "c.pdf", "d.pdf", "e.md"]
    assert "local.md" not in planned
    assert all(b["required_free_bytes"] == 2 * b["bytes"] for b in batches)
    assert all(b["sufficient_free_space"] for b in batches)
    assert plan["totals"] == {
        "dataless_files": 5,
        "dataless_bytes": 11 * GIB + 1,
        "batches": len(batches),
        "oversize_files": 0,
    }


def test_plan_reports_files_larger_than_a_batch_instead_of_splitting_them(
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = load_script()
    entries = [("huge.pdf", 6 * GIB, "dataless"), ("small.md", 10, "dataless")]
    _code, plan = run_plan(module, capsys, entries, free_bytes=100 * GIB)
    assert [f["path"] for f in plan["oversize"]] == ["huge.pdf"]
    assert [f["path"] for b in plan["batches"] for f in b["files"]] == ["small.md"]
    assert plan["totals"]["oversize_files"] == 1


def test_plan_flags_a_batch_when_free_space_is_below_twice_its_size(
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = load_script()
    entries = [("a.pdf", 4 * GIB, "dataless")]
    _code, plan = run_plan(module, capsys, entries, free_bytes=7 * GIB)
    batch = plan["batches"][0]
    assert batch["required_free_bytes"] == 8 * GIB
    assert batch["sufficient_free_space"] is False
    assert plan["free_bytes"] == 7 * GIB
    _code, enough = run_plan(module, capsys, entries, free_bytes=8 * GIB)
    assert enough["batches"][0]["sufficient_free_space"] is True


def test_plan_is_the_default_and_batch_size_cannot_exceed_five_gib(
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = load_script()
    entries = [("a.pdf", 2 * GIB, "dataless"), ("b.pdf", 2 * GIB, "dataless")]
    code = module.main(
        ["--root", "/synthetic/container", "--json", "--batch-gib", "20"],
        enumerator=fake_enumerator(entries),
        disk_usage=lambda _path: SimpleNamespace(free=100 * GIB),
        hydrator=exploding_hydrator,
    )
    plan = json.loads(capsys.readouterr().out)
    assert code == 0 and plan["mode"] == "plan"
    assert plan["batch_limit_bytes"] == 5 * GIB
    assert [b["bytes"] for b in plan["batches"]] == [4 * GIB]
    code = module.main(
        ["--root", "/synthetic/container", "--json", "--batch-gib", "1"],
        enumerator=fake_enumerator(entries),
        disk_usage=lambda _path: SimpleNamespace(free=100 * GIB),
        hydrator=exploding_hydrator,
    )
    assert json.loads(capsys.readouterr().out)["batch_limit_bytes"] == GIB


def test_text_plan_lists_batches_without_hydrating(capsys: pytest.CaptureFixture[str]) -> None:
    module = load_script()
    code = module.main(
        ["--root", "/synthetic/container", "--plan"],
        enumerator=fake_enumerator([("a.pdf", GIB, "dataless")]),
        disk_usage=lambda _path: SimpleNamespace(free=10 * GIB),
        hydrator=exploding_hydrator,
    )
    output = capsys.readouterr().out
    assert code == 0
    assert "batch 1" in output and "a.pdf" in output and "WOULD" in output.upper()


def test_execute_is_refused_without_the_explicit_confirmation_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = load_script()
    code = module.main(
        ["--root", "/synthetic/container", "--execute"],
        enumerator=fake_enumerator([("a.pdf", GIB, "dataless")]),
        disk_usage=lambda _path: SimpleNamespace(free=10 * GIB),
        hydrator=exploding_hydrator,
    )
    assert code == 2
    assert "--confirm-hydrate" in capsys.readouterr().err


def test_a_root_is_required_so_no_folder_is_implicitly_selected() -> None:
    module = load_script()
    with pytest.raises(SystemExit) as error:
        module.main(["--plan"])
    assert error.value.code == 2


def test_cli_library_status_reports_the_reason_histogram_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(library_documents, "_LEGACY_CONVERTER", fake_converter(), raising=False)
    project, store, archive, docs = (
        tmp_path / name for name in ("project", "store", "archive", "docs")
    )
    for folder in (project, store, archive, docs):
        folder.mkdir()
    register_corpus_store(project, store)
    synthetic_mix(docs)
    manifest = {
        "schema": "cce.library-sources.v1",
        "archives": [str(archive)],
        "documents": [source(docs)],
    }
    result = build_library(manifest, project_root=project, store_root=store)
    publish_library(Path(result["generation"]), project_root=project, store_root=store)
    capsys.readouterr()
    args = build_parser().parse_args(
        [
            "library",
            "status",
            "--json",
            "--store-root",
            str(store),
            "--project-root",
            str(project),
        ]
    )
    assert library_commands.handle_library(args) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["unexplained_non_full"] == 0
    reasons = status["body_reasons"]
    assert sum(sum(group.values()) for group in reasons.values()) == (
        status["by_body_status"].get("partial", 0)
        + status["by_body_status"].get("metadata_only", 0)
    )
    assert reasons["metadata_only"]["unsupported_format"] == 2
