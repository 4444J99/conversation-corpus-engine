#!/usr/bin/env python3
"""Repair audited inline-code boundaries in an exclusive COW reading copy."""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import json
import os
import stat
import sys
import time
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "obsidian_package_base", Path(__file__).with_name("package-obsidian-vault.py")
)
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)
META_FILES = ("manifest.json", "ledger.jsonl", "sources.jsonl", "navigation.sqlite")


def clone_file(source, output):
    base.require(sys.platform == "darwin", "COW repair requires Darwin clonefile")
    libc = ctypes.CDLL(None, use_errno=True)
    clone = libc.clonefile
    clone.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
    clone.restype = ctypes.c_int
    if clone(os.fsencode(source), os.fsencode(output), 0):
        raise OSError(ctypes.get_errno(), "clonefile failed", str(source))
    output.chmod(0o600)


def metadata(source, budget):
    return {name: base.hash_file(source / base.META / name, budget) for name in META_FILES}


def inventory(root, budget):
    count = size = 0
    for path in root.rglob("*"):
        budget.check()
        mode = path.lstat().st_mode
        base.require(stat.S_ISDIR(mode) or stat.S_ISREG(mode), "non-regular package entry")
        if stat.S_ISREG(mode):
            count += 1
            size += path.stat().st_size
    base.require(size <= budget.maximum, "output byte budget exceeded")
    return count, size


def replace_owned(path, data, budget):
    original = path.stat().st_size
    temporary = path.with_name(path.name + ".inline-repair-tmp")
    base.write(temporary, data, budget)
    temporary.replace(path)
    budget.written -= original


def publish_manifest(path, data, budget):
    temporary = path.with_name("manifest.inline-repair-tmp")
    budget.charge(len(data))
    with base.private_file(temporary) as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    # A same-directory link publishes atomically and refuses an existing name.
    # A failure before this point leaves only an uncommitted temporary file.
    os.link(temporary, path)
    temporary.unlink()


def repair(
    source,
    output,
    audit_path,
    *,
    clone_receipt=None,
    max_output_bytes=8 * 1024**3,
    max_seconds=1800,
):
    source, output, audit_path = (
        Path(p).expanduser().absolute() for p in (source, output, audit_path)
    )
    base.safe_path(source)
    base.safe_path(output)
    base.safe_path(audit_path)
    base.require(source.is_dir(), "source package directory missing")
    base.require(not output.exists() or not source.samefile(output), "source/output alias overlap")
    base.require(
        not output.is_relative_to(source) and not source.is_relative_to(output),
        "source/output overlap",
    )
    base.require(
        not any(
            ancestor.parent.name == "generations" and ancestor.parent.parent.name == "library-all"
            for ancestor in (output, *output.parents)
        ),
        "derived output must stay outside the canonical generation",
    )
    base.require(max_seconds > 0 and max_output_bytes > 0, "invalid repair limits")
    base.require(clone_receipt is not None or not output.exists(), "output already exists")
    budget = base.Budget(output, max_output_bytes, max_seconds)
    with base.deadline(max_seconds):
        before = metadata(source, budget)
        audit_bytes = base.read_source(audit_path, 16 * 1024**2)
        audit = json.loads(audit_bytes)
        base.require(
            audit.get("schema") == "cce.obsidian-inline-boundary-audit.v1", "unknown audit schema"
        )
        base.require(
            audit.get("package_manifest_sha256") == before["manifest.json"],
            "audit manifest mismatch",
        )
        base.require(audit.get("boundaries_inside_delimiters") == 0, "audit crosses a delimiter")
        findings = {}
        for finding in audit["findings"]:
            findings.setdefault(finding["source_path"], []).append(finding)
        base.require(len(findings) == audit["affected_notes"], "audit note count mismatch")
        base.require(
            sum(map(len, findings.values())) == audit["crossing_spans"], "audit span count mismatch"
        )
        if clone_receipt is not None:
            receipt = json.loads(Path(clone_receipt).read_text())
            base.require(
                receipt.get("schema") == "cce.obsidian-cow-clone.v1"
                and receipt.get("status") == "cloned",
                "clone not completed",
            )
            base.require(
                receipt.get("source") == str(source) and receipt.get("output") == str(output),
                "clone identity mismatch",
            )
            base.require(
                receipt.get("source_metadata_sha256") == before, "clone source metadata mismatch"
            )
            count, budget.written = inventory(output, budget)
            base.require(
                count == receipt["files"] and budget.written == receipt["logical_bytes"],
                "clone inventory mismatch",
            )
            base.require(metadata(output, budget) == before, "clone metadata mismatch")
            parent_manifest = output / base.META / "parent-manifest.json"
            base.require(not parent_manifest.exists(), "clone lineage already exists")
            (output / base.META / "manifest.json").rename(parent_manifest)
        else:
            output.mkdir(mode=0o700)
            for directory, dirs, files in os.walk(source, followlinks=False):
                budget.check()
                relative = Path(directory).relative_to(source)
                for name in dirs:
                    original = Path(directory) / name
                    base.require(not original.is_symlink(), "source symlink refused")
                    (output / relative / name).mkdir(mode=0o700)
                for name in files:
                    original = Path(directory) / name
                    base.require(
                        stat.S_ISREG(original.lstat().st_mode), "non-regular source refused"
                    )
                    budget.charge(original.stat().st_size)
                    destination = output / relative / name
                    if relative == Path(base.META) and name == "manifest.json":
                        destination = destination.with_name("parent-manifest.json")
                    clone_file(original, destination)
        manifest = json.loads((source / base.META / "manifest.json").read_text())
        ledger = output / base.META / "ledger.jsonl"
        temporary = ledger.with_name("ledger.inline-repair-tmp")
        old_ledger_size = ledger.stat().st_size
        changed = set()
        seen = set()
        added_bytes = 0
        with base.private_file(temporary) as target, ledger.open() as original:
            for line in original:
                budget.check()
                row = json.loads(line)
                spans = findings.get(row["path"], [])
                if spans:
                    seen.add(row["path"])
                    base.require(
                        0
                        <= row["bytes"]
                        <= min(manifest.get("max_source_bytes", 128 * 1024**2), 128 * 1024**2),
                        "affected source exceeds byte limit",
                    )
                    raw = b"".join(base.recover_row(source, row))
                    for span in spans:
                        delimiter = b"`" * span["delimiter_bytes"]
                        start, end = span["span_start"], span["span_end"]
                        base.require(
                            0
                            <= start
                            < span["content_start"]
                            <= span["content_end"]
                            < end
                            <= len(raw),
                            "invalid audit span",
                        )
                        base.require(
                            row["sha256"] == span["source_sha256"], "audit source hash mismatch"
                        )
                        base.require(
                            raw[start : span["content_start"]] == delimiter
                            and raw[span["content_end"] : end] == delimiter,
                            "audit delimiter mismatch",
                        )
                        actual = [
                            p["source_end"]
                            for p in row["pieces"][:-1]
                            if start < p["source_end"] < end
                        ]
                        base.require(actual == span["boundaries"], "audit boundaries mismatch")
                        base.require(
                            all(span["content_start"] <= b <= span["content_end"] for b in actual),
                            "boundary inside delimiter",
                        )
                    for piece in row["pieces"]:
                        prefix = suffix = b""
                        for span in spans:
                            delimiter = b"`" * span["delimiter_bytes"]
                            if span["span_start"] < piece["source_start"] < span["span_end"]:
                                base.require(not prefix, "overlapping audit spans")
                                prefix = b"Inline code continued: " + delimiter + b" "
                            if span["span_start"] < piece["source_end"] < span["span_end"]:
                                base.require(not suffix, "overlapping audit spans")
                                suffix = b" " + delimiter + b"\n"
                        if not (prefix or suffix):
                            continue
                        path = output / piece["path"]
                        base.safe_path(path)
                        base.require(
                            path.resolve().is_relative_to(output.resolve()), "piece outside output"
                        )
                        data = base.read_source(path, manifest["max_note_bytes"])
                        base.require(
                            len(data) == piece["bytes"] and base.digest(data) == piece["sha256"],
                            "clone piece mismatch",
                        )
                        begin = piece["body_start"]
                        end = begin + piece["body_bytes"]
                        rendered = data[:begin] + prefix + data[begin:end] + suffix + data[end:]
                        base.require(
                            len(rendered) <= manifest["max_note_bytes"],
                            "repaired note exceeds byte limit",
                        )
                        replace_owned(path, rendered, budget)
                        added_bytes += len(prefix) + len(suffix)
                        piece.update(
                            bytes=len(rendered),
                            sha256=base.digest(rendered),
                            body_start=begin + len(prefix),
                            inline_before=prefix.decode(),
                            inline_after=suffix.decode(),
                        )
                        changed.add(piece["path"])
                payload = base.encoded(row)
                budget.charge(len(payload))
                target.write(payload)
        base.require(seen == set(findings), "audit source absent from ledger")
        base.require(len(changed) == audit["affected_parts"], "audit repaired part count mismatch")
        temporary.replace(ledger)
        budget.written -= old_ledger_size
        manifest["ledger_sha256"] = base.hash_file(ledger, budget)
        manifest["rendered_bytes"] += added_bytes
        manifest["repair"] = {
            "schema": "cce.obsidian-inline-repair.v1",
            "source_manifest_sha256": before["manifest.json"],
            "audit_sha256": base.digest(audit_bytes),
            "affected_notes": len(seen),
            "affected_parts": len(changed),
            "added_wrapper_bytes": added_bytes,
            "source_metadata_sha256": before,
            "source_metadata_unchanged": False,
            "body_bytes_unchanged": True,
            "native_obsidian_acceptance": "unverified",
        }
        base.verify_package(output, budget=budget, expected=manifest)
        base.require(metadata(source, budget) == before, "source package metadata changed")
        manifest["repair"]["source_metadata_unchanged"] = True
        manifest["repair"]["elapsed_seconds"] = round(time.monotonic() - budget.started, 3)
        manifest["max_output_bytes"] = max_output_bytes
        manifest["max_seconds"] = max_seconds
        manifest["output_bytes_before_manifest"] = budget.written
        publish_manifest(output / base.META / "manifest.json", base.encoded(manifest), budget)
        return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--clone-receipt", type=Path)
    parser.add_argument("--max-output-bytes", type=int, default=8 * 1024**3)
    parser.add_argument("--max-seconds", type=int, default=1800)
    args = parser.parse_args()
    os.nice(10)
    print(
        json.dumps(
            repair(
                args.source,
                args.output,
                args.audit,
                clone_receipt=args.clone_receipt,
                max_output_bytes=args.max_output_bytes,
                max_seconds=args.max_seconds,
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
