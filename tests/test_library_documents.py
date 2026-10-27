from __future__ import annotations

import hashlib
import shutil
import zipfile
from pathlib import Path

import pytest

from conversation_corpus_engine.library_documents import iter_documents


def source(root: Path, **options: object) -> dict:
    return {
        "id": "writing",
        "root": str(root),
        "include": ["**/*"],
        "exclude": [],
        "wing": "authored",
        "lane": "creative",
        "kind": "document",
        **options,
    }


def test_identity_survives_edits_and_same_content_files_remain_distinct(tmp_path: Path) -> None:
    (tmp_path / "first.md").write_text("Original text", encoding="utf-8")
    (tmp_path / "second.md").write_text("Original text", encoding="utf-8")
    before = list(iter_documents([source(tmp_path)]))
    assert len(before) == 2
    assert before[0].uid != before[1].uid
    assert before[0].provenance["source_sha256"] == before[1].provenance["source_sha256"]
    (tmp_path / "first.md").write_text("Revised text", encoding="utf-8")
    after = list(iter_documents([source(tmp_path)]))
    assert before[0].uid == after[0].uid
    assert before[0].provenance["source_sha256"] != after[0].provenance["source_sha256"]
    assert before[0].extra["source_id"] == "writing"
    assert before[0].extra["wing"] == "authored"
    assert before[0].extra["lane"] == "creative"


def test_unicode_text_and_original_reference_are_preserved(tmp_path: Path) -> None:
    text = "# Rêverie\n\n日本語 — café\n"
    path = tmp_path / "Rêverie.md"
    path.write_text(text, encoding="utf-8")
    row = list(iter_documents([source(tmp_path)]))[0]
    assert row.title == "Rêverie"
    assert row.messages[0].text == text
    assert row.provenance["source_file"] == "Rêverie.md"
    assert row.provenance["source_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert row.provenance["source_uri"] == path.as_uri()
    assert row.provenance["modified_ns"] > 0
    assert row.extra["body_status"] == "full"


@pytest.mark.parametrize("suffix", [".tex", ".yaml", ".yml", ".org"])
def test_authored_source_formats_remain_literal_utf8(tmp_path: Path, suffix: str) -> None:
    # No TeX expansion, YAML constructors, or Org execution: these are evidence.
    raw = b"\\input{private}\n!!python/object:example\n#+begin_src sh\nfalse\n#+end_src\n"
    path = tmp_path / ("research" + suffix)
    path.write_bytes(raw)
    row = list(iter_documents([source(tmp_path)]))[0]
    assert row.messages[0].text == raw.decode("utf-8")
    assert row.provenance["source_sha256"] == hashlib.sha256(raw).hexdigest()
    assert row.provenance["extractor"] == "utf8@1"
    assert row.extra["body_status"] == "full"


def test_symlink_escape_is_accounted_without_reading_target(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("out of scope")
    (root / "link.md").symlink_to(outside / "secret.md")
    (root / "linked-dir").symlink_to(outside, target_is_directory=True)
    report: dict = {}
    rows = list(iter_documents([source(root)], report=report))
    assert rows == []
    assert {f["code"] for f in report["findings"]} == {"symlink_not_followed"}
    assert {f["path"] for f in report["files"]} == {"link.md", "linked-dir"}
    assert all("out of scope" not in str(f) for f in report["files"])


def test_excludes_prune_directories_and_include_globs_cover_root_files(tmp_path: Path) -> None:
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "private.md").write_text("excluded")
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "kept.md").write_text("kept")
    (tmp_path / "root.md").write_text("root")
    (tmp_path / "ignored.txt").write_text("ignored")
    report: dict = {}
    rows = list(
        iter_documents(
            [source(tmp_path, include=["**/*.md"], exclude=["**/cache/**"])], report=report
        )
    )
    assert {r.provenance["source_file"] for r in rows} == {"notes/kept.md", "root.md"}
    assert report["counts"]["excluded"] == 1


def test_docx_extracts_paragraphs_tabs_breaks_and_table_text(tmp_path: Path) -> None:
    path = tmp_path / "essay.docx"
    xml = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>First</w:t><w:tab/><w:t>line</w:t><w:br/><w:t>continued</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Table cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:p><w:r><w:t>日本語</w:t></w:r></w:p></w:body></w:document>'
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)
    row = list(iter_documents([source(tmp_path)]))[0]
    assert row.messages[0].text == "First\tline\ncontinued\n\nTable cell\n\n日本語"
    assert row.provenance["extractor"] == "docx-xml@1"


def test_unsupported_oversized_and_empty_bodies_have_explicit_status(tmp_path: Path) -> None:
    (tmp_path / "image.png").write_bytes(b"PNG")
    (tmp_path / "large.md").write_text("x" * 40)
    (tmp_path / "empty.txt").write_text("")
    report: dict = {}
    rows = list(iter_documents([source(tmp_path, max_bytes=20)], report=report))
    assert len(rows) == 3
    by_path = {r.provenance["source_file"]: r for r in rows}
    assert by_path["image.png"].extra["body_reason"] == "unsupported_format"
    assert by_path["large.md"].extra["body_reason"] == "size_limit"
    assert by_path["empty.txt"].extra["body_reason"] == "empty_body"
    assert all(r.messages == [] and r.extra["body_status"] == "metadata_only" for r in rows)
    assert report["counts"]["matched"] == 3
    assert report["counts"]["metadata_only"] == 3


def test_missing_root_and_symlink_root_are_explicit(tmp_path: Path) -> None:
    root = tmp_path / "actual"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    report: dict = {}
    assert (
        list(
            iter_documents([source(tmp_path / "missing"), source(alias, id="alias")], report=report)
        )
        == []
    )
    assert {f["code"] for f in report["findings"]} == {"missing_root", "symlink_not_followed"}


def test_invalid_utf8_and_invalid_docx_report_extraction_errors(tmp_path: Path) -> None:
    (tmp_path / "broken.txt").write_bytes(b"\xff\xfe\xff")
    (tmp_path / "broken.docx").write_bytes(b"not a zip")
    report: dict = {}
    rows = list(iter_documents([source(tmp_path)], report=report))
    assert len(rows) == 2
    assert all(r.extra["body_reason"] == "extraction_error" for r in rows)
    assert report["counts"]["errors"] == 2


def test_docx_expansion_limit_does_not_accept_zip_bomb(tmp_path: Path) -> None:
    with zipfile.ZipFile(tmp_path / "huge.docx", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", " " * 50000)
    row = list(iter_documents([source(tmp_path, max_bytes=1000)]))[0]
    assert row.extra["body_status"] == "metadata_only"
    assert row.extra["body_reason"] == "extraction_error"


def test_source_manifest_cannot_implicitly_scan_current_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="root"):
        list(iter_documents([{"id": "missing-root"}]))
    with pytest.raises(ValueError, match="duplicate"):
        list(iter_documents([source(tmp_path), source(tmp_path)]))


def test_pdf_body_is_extracted_or_explicitly_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A complete synthetic one-page PDF exercises the actual optional extractor.
    stream = b"BT /F1 12 Tf 72 720 Td (Synthetic research text) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for ordinal, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(f"{ordinal} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(pdf)
    pdf.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010} 00000 n \n".encode())
    pdf.extend(f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    (tmp_path / "paper.pdf").write_bytes(pdf)
    if shutil.which("pdftotext"):
        row = list(iter_documents([source(tmp_path)]))[0]
        assert "Synthetic research text" in row.messages[0].text
        assert row.provenance["extraction_scope"] == "embedded_text_no_ocr"
    monkeypatch.setenv("PATH", "")
    report: dict = {}
    row = list(iter_documents([source(tmp_path)], report=report))[0]
    assert row.extra["body_reason"] == "pdf_extractor_unavailable"
    assert row.extra["body_status"] == "metadata_only"
    assert report["findings"][0]["code"] == "pdf_extractor_unavailable"
