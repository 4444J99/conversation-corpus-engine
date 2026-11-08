"""Lightweight lexical path checks shared by export and recoverable note writes."""

import os
from pathlib import Path


def _no_symlinks(path: Path) -> None:
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError(f"export path contains a symlink: {component}")


def _absolute(path: Path) -> Path:
    """Normalize lexically; resolving would follow the symlinks we must reject."""
    return Path(os.path.abspath(path))  # noqa: PTH100 - no filesystem walks or symlink following
