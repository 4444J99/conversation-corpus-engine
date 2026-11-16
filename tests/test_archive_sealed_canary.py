"""Literal canary checks use invented text and disposable Git histories only."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conversation_corpus_engine import archive_sealed_canary as canary

MARKER = b"SYNTHETIC-CANARY-DO-NOT-PUBLISH-7c941fe2"


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Synthetic",
            "GIT_AUTHOR_EMAIL": "synthetic@example.invalid",
            "GIT_COMMITTER_NAME": "Synthetic",
            "GIT_COMMITTER_EMAIL": "synthetic@example.invalid",
        },
    )


def test_clean_tree_has_counts_and_no_content(tmp_path):
    root = tmp_path / "cipher"
    root.mkdir()
    (root / "object").write_bytes(b"opaque bytes")
    result = canary.scan(root, [MARKER])
    assert result["passed"]
    assert result["working_files"] == 1
    assert result["matched_markers"] == 0
    assert result["git_history_scanned"] is False
    assert "opaque bytes" not in json.dumps(result)
    assert str(root) not in json.dumps(result)


def test_marker_crossing_stream_boundary_is_found(tmp_path, monkeypatch):
    monkeypatch.setattr(canary, "READ_BYTES", 13)
    (tmp_path / "object").write_bytes(b"x" * 11 + MARKER + b"tail")
    result = canary.scan(tmp_path, [MARKER])
    assert not result["passed"]
    assert result["matched_markers"] == 1


def test_marker_in_filename_is_found(tmp_path):
    (tmp_path / MARKER.decode()).write_bytes(b"opaque")
    assert not canary.scan(tmp_path, [MARKER])["passed"]


@pytest.mark.parametrize("location", ["body", "filename", "commit"])
def test_deleted_history_leaks_are_still_detected(tmp_path, location):
    git(tmp_path, "init", "-b", "main")
    path = tmp_path / (MARKER.decode() if location == "filename" else "old")
    path.write_bytes(MARKER if location == "body" else b"opaque")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", MARKER.decode() if location == "commit" else "synthetic")
    path.unlink()
    git(tmp_path, "add", "-u")
    git(tmp_path, "commit", "-m", "synthetic removal")
    result = canary.scan(tmp_path, [MARKER])
    assert not result["passed"]
    assert result["git_history_scanned"]
    assert result["git_objects"] > 0


def test_empty_git_history_is_explicitly_scanned(tmp_path):
    git(tmp_path, "init", "-b", "main")
    result = canary.scan(tmp_path, [MARKER])
    assert result["passed"]
    assert result["git_history_scanned"]
    assert result["git_objects"] == 0


def test_non_ascii_filename_in_removed_history_is_scanned_as_raw_bytes(tmp_path):
    marker = "Synthetic-été-canary".encode()
    git(tmp_path, "init", "-b", "main")
    path = tmp_path / marker.decode()
    path.write_bytes(b"opaque")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "synthetic")
    path.unlink()
    git(tmp_path, "add", "-u")
    git(tmp_path, "commit", "-m", "synthetic removal")
    assert not canary.scan(tmp_path, [marker])["passed"]


def test_unreadable_directory_fails_instead_of_silently_skipping(tmp_path, monkeypatch):
    (tmp_path / "nested").mkdir()
    real_scandir = os.scandir

    def denied(path):
        if Path(path).name == "nested":
            raise PermissionError("synthetic private path")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", denied)
    with pytest.raises(canary.CanaryError, match="tree-inspection-failed"):
        canary.scan(tmp_path, [MARKER])


def test_byte_budget_exhaustion_is_not_a_clean_scan(tmp_path):
    (tmp_path / "object").write_bytes(b"x" * 100)
    with pytest.raises(canary.CanaryError, match="scan-budget-exceeded"):
        canary.scan(tmp_path, [MARKER], budget=50)


def test_total_deadline_bounds_many_tiny_files_even_with_spare_byte_budget(tmp_path):
    for i in range(100):
        (tmp_path / f"tiny-{i}").write_bytes(b"x")
    ticks = [0.0]

    def clock():
        ticks[0] += 0.1
        return ticks[0]

    with pytest.raises(canary.CanaryError, match="scan-deadline-exceeded"):
        canary.scan(tmp_path, [MARKER], budget=100000, max_seconds=1, clock=clock)


@pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan")])
def test_invalid_total_deadlines_are_rejected(tmp_path, seconds):
    with pytest.raises(canary.CanaryError, match="invalid-deadline"):
        canary.scan(tmp_path, [MARKER], max_seconds=seconds)


def test_git_failure_is_not_a_clean_scan(tmp_path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(canary.CanaryError, match="git-inspection-failed"):
        canary.scan(tmp_path, [MARKER])


def test_symlink_is_not_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.write_bytes(MARKER)
    root = tmp_path / "cipher"
    root.mkdir()
    (root / "link").symlink_to(outside)
    with pytest.raises(canary.CanaryError, match="symlink-refused"):
        canary.scan(root, [MARKER])


@pytest.mark.parametrize("markers", [[], [b""], ["not bytes"], [b"x" * 4097]])
def test_bad_marker_inputs_are_rejected(tmp_path, markers):
    with pytest.raises(canary.CanaryError, match="invalid-markers"):
        canary.scan(tmp_path, markers)


def test_cli_failure_does_not_echo_markers_or_private_paths(tmp_path):
    root = tmp_path / "private-destination"
    root.mkdir()
    (root / "object").write_bytes(MARKER)
    markers = tmp_path / "markers.json"
    markers.write_text(json.dumps([MARKER.decode()]))
    script = Path(__file__).resolve().parents[1] / "scripts" / "vault_canary_check.py"
    result = subprocess.run(
        [sys.executable, str(script), "--repo", str(root), "--markers", str(markers)],
        check=False,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(script.parent.parent / "src")},
    )
    assert result.returncode == 1
    assert not json.loads(result.stdout)["passed"]
    assert MARKER not in result.stdout + result.stderr
    assert str(root).encode() not in result.stdout + result.stderr


def test_regular_file_swapped_to_fifo_cannot_bypass_total_deadline(tmp_path):
    (tmp_path / "object").write_bytes(b"opaque")
    code = """
import os, sys
from pathlib import Path
from conversation_corpus_engine import archive_sealed_canary as canary
root = Path(sys.argv[1])
target = root / "object"
real_open = os.open
def swapped(path, *args, **kwargs):
    if Path(path) == target:
        target.unlink()
        os.mkfifo(target)
    return real_open(path, *args, **kwargs)
canary.os.open = swapped
try:
    canary.scan(root, [b"SYNTHETIC-CANARY"], max_seconds=0.05)
except canary.CanaryError as error:
    print(str(error))
    raise SystemExit(2)
raise SystemExit(0)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    )
    assert result.returncode == 2
    assert result.stdout.strip() == "non-regular-file"
    assert not result.stderr
