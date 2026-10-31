"""The suite must not be broken by CCE_* variables a developer's shell exports (issue #81)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from conftest import CCE_ENVIRONMENT_VARIABLES

REPO_ROOT = Path(__file__).resolve().parents[1]
# Both assert behaviour that an exported CCE_* variable would change if it reached the test.
ENVIRONMENT_SENSITIVE_TESTS = (
    "tests/test_corpus_store.py::test_missing_configuration_and_unregistered_root_create_nothing",
    "tests/test_archive_mcp.py::test_archive_root_comes_from_server_configuration_only",
)


def test_no_cce_variable_reaches_a_test() -> None:
    assert sorted(name for name in os.environ if name.startswith("CCE_")) == []


def test_exported_symlinked_store_root_cannot_break_the_suite(tmp_path: Path) -> None:
    real_store = tmp_path / "real-store"
    real_store.mkdir()
    linked_store = tmp_path / "linked-store"
    linked_store.symlink_to(real_store, target_is_directory=True)
    # The shell that surfaced the defect exported a symlinked store; the engine refuses those.
    exported = {name: str(linked_store) for name in CCE_ENVIRONMENT_VARIABLES}

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--no-header",
            *ENVIRONMENT_SENSITIVE_TESTS,
        ],
        cwd=REPO_ROOT,
        env={**os.environ, **exported},
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout, result.stdout
