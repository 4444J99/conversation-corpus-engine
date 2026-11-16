"""Owner-only output for recovered private text."""

from __future__ import annotations

import os
from pathlib import Path


def prepare_directory(out: Path) -> Path:
    """A new or empty directory, mode 0700 even when an existing empty one was more open."""
    out = out.expanduser().absolute()
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise FileExistsError(f"output directory already exists and is not empty: {out}")
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    out.chmod(0o700)
    return out


def write_private(path: Path, text: str) -> None:
    """Create ``path`` readable by its owner only; it never exists with wider access."""
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
