from __future__ import annotations

import json
import os
import subprocess
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .federation import REGISTRY_VERSION, load_registry, now_iso, registry_path, save_registry

CORPUS_STORE_ROOT_ENV = "CCE_CORPUS_STORE_ROOT"
# What `git rev-parse` reports, with exit status 128, for a directory outside every work tree.
NOT_A_REPOSITORY = "not a git repository"
NOT_A_REPOSITORY_EXIT = 128


class CorpusStoreError(ValueError):
    """Raised when a corpus write does not satisfy the private-custody contract."""


@dataclass(frozen=True)
class CorpusWriteAuthorization:
    project_root: Path
    store_root: Path
    destination: Path


def _absolute_path(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    return expanded.absolute()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C"},  # git's messages are matched by text below
    )


def _git_root(path: Path) -> Path | None:
    """The work tree `path` sits in, or None when it sits in none.

    None means exactly one thing: git said this is not a repository. Any other failure (no git
    binary, a permission error, an ownership refusal) is raised, because treating it as "outside
    every work tree" would let private text land somewhere Git can see.
    """
    try:
        result = _run_git(path, "rev-parse", "--show-toplevel")
    except OSError as exc:
        raise CorpusStoreError(f"Could not run git to inspect {path}: {exc}") from exc
    if result.returncode != 0:
        if result.returncode == NOT_A_REPOSITORY_EXIT and NOT_A_REPOSITORY in result.stderr:
            return None
        raise CorpusStoreError(
            f"git could not inspect {path} (exit {result.returncode}): "
            f"{result.stderr.strip() or 'no message'}"
        )
    value = result.stdout.strip()
    if not value:
        raise CorpusStoreError(f"git named no work tree for {path} although it reported success")
    return Path(value).resolve()


def _git_path_is_ignored(git_root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(git_root)
    except ValueError:
        return False
    result = _run_git(git_root, "check-ignore", "--quiet", "--", str(relative))
    return result.returncode == 0


def _find_nested_git_metadata(root: Path) -> Path | None:
    for current_root, dirnames, filenames in os.walk(root, followlinks=False):
        if ".git" in dirnames or ".git" in filenames:
            return Path(current_root) / ".git"
    return None


def _find_symlink(root: Path) -> Path | None:
    if root.is_symlink():
        return root
    if not root.exists():
        return None
    for current_root, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(current_root)
        for name in [*dirnames, *filenames]:
            candidate = current / name
            if candidate.is_symlink():
                return candidate
    return None


def _validate_git_custody(root: Path) -> Path | None:
    nested_git = _find_nested_git_metadata(root)
    if nested_git is not None:
        raise CorpusStoreError(
            f"Corpus-store root contains nested Git metadata and is not private custody: {nested_git}"
        )

    git_root = _git_root(root)
    if git_root is None:
        return None

    relative_root = root.relative_to(git_root)
    tracked = _run_git(git_root, "ls-files", "-z", "--", str(relative_root))
    if tracked.returncode != 0:
        raise CorpusStoreError(
            f"Could not inspect tracked content beneath corpus-store root: {root}"
        )
    if tracked.stdout:
        raise CorpusStoreError(f"Corpus-store root contains Git-tracked content: {root}")

    if not _git_path_is_ignored(git_root, root):
        raise CorpusStoreError(f"Corpus-store root is Git-visible: {root}")
    visible = _run_git(
        git_root,
        "ls-files",
        "-z",
        "--others",
        "--exclude-standard",
        "--",
        str(relative_root),
    )
    if visible.returncode != 0:
        raise CorpusStoreError(f"Could not inspect Git-visible corpus-store content: {root}")
    if visible.stdout:
        visible_path = visible.stdout.split("\0", 1)[0]
        raise CorpusStoreError(
            "Corpus-store root contains Git-visible content or an ignore-rule negation: "
            f"{git_root / visible_path}"
        )
    return git_root


def require_git_private(directory: Path) -> Path:
    """Refuse a directory that private text must not land in.

    A directory outside any Git work tree is private. One inside a work tree must be ignored by
    Git, so the files written there can never be staged by accident. Returns the resolved path.
    """
    resolved = _absolute_path(directory).resolve(strict=False)
    probe = resolved
    while not probe.exists():
        probe = probe.parent
    git_root = _git_root(probe)
    if git_root is not None and not _git_path_is_ignored(git_root, resolved / ".private-probe"):
        raise CorpusStoreError(f"Directory is Git-visible and cannot hold private text: {resolved}")
    return resolved


def _resolve_existing_store_root(root: Path) -> Path:
    lexical_root = _absolute_path(root)
    if lexical_root.is_symlink():
        raise CorpusStoreError(f"Corpus-store root may not be a symlink: {lexical_root}")
    try:
        resolved_root = lexical_root.resolve(strict=True)
    except FileNotFoundError as exc:
        raise CorpusStoreError(f"Corpus-store root does not exist: {lexical_root}") from exc
    if not resolved_root.is_dir():
        raise CorpusStoreError(f"Corpus-store root is not a directory: {resolved_root}")
    return resolved_root


def validate_corpus_store_root(root: Path) -> Path:
    resolved_root = _resolve_existing_store_root(root)
    _validate_git_custody(resolved_root)
    return resolved_root


def resolve_configured_corpus_store_root(
    explicit_root: Path | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    environment = os.environ if environ is None else environ
    configured = explicit_root
    if configured is None:
        raw_environment_root = environment.get(CORPUS_STORE_ROOT_ENV, "").strip()
        if raw_environment_root:
            configured = Path(raw_environment_root)
    if configured is None:
        raise CorpusStoreError(
            "Corpus-store custody is not configured; provide --corpus-store-root or "
            f"{CORPUS_STORE_ROOT_ENV}."
        )
    return _resolve_existing_store_root(configured)


def load_corpus_store_registry(project_root: Path) -> dict[str, Any]:
    path = registry_path(project_root.resolve())
    if not path.is_file():
        raise CorpusStoreError(f"Corpus-store registry does not exist: {path}")
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorpusStoreError(f"Corpus-store registry is malformed: {path}") from exc
    if not isinstance(registry, dict):
        raise CorpusStoreError(f"Corpus-store registry must be a JSON object: {path}")
    if registry.get("registry_version") != REGISTRY_VERSION:
        raise CorpusStoreError(f"Corpus-store registry must use version {REGISTRY_VERSION}: {path}")
    if not isinstance(registry.get("corpora"), list):
        raise CorpusStoreError(f"Corpus-store registry has malformed corpora state: {path}")
    registration = registry.get("corpus_store")
    if not isinstance(registration, dict):
        raise CorpusStoreError(f"Corpus-store root is not registered in: {path}")
    if registration.get("status") != "active":
        raise CorpusStoreError(f"Corpus-store registration is not active in: {path}")
    registered_root = registration.get("root")
    if not isinstance(registered_root, str) or not Path(registered_root).is_absolute():
        raise CorpusStoreError(f"Corpus-store registration has an invalid root in: {path}")
    return registry


def load_corpus_store_registration(project_root: Path) -> dict[str, str]:
    registration = load_corpus_store_registry(project_root).get("corpus_store")
    if not isinstance(registration, dict):
        raise CorpusStoreError(
            f"Corpus-store root is not registered in: {registry_path(project_root.resolve())}"
        )
    return registration


def register_corpus_store(project_root: Path, root: Path) -> dict[str, str]:
    resolved_project_root = _absolute_path(project_root)
    if not resolved_project_root.is_dir():
        raise CorpusStoreError(f"Project root does not exist: {resolved_project_root}")
    resolved_root = validate_corpus_store_root(root)
    registry = load_registry(resolved_project_root)
    registration = registry.get("corpus_store")
    if (
        registry.get("registry_version") == REGISTRY_VERSION
        and isinstance(registration, dict)
        and registration.get("root") == str(resolved_root)
        and registration.get("status") == "active"
    ):
        return deepcopy(registration)

    new_registration = {
        "root": str(resolved_root),
        "status": "active",
        "registered_at": now_iso(),
    }
    registry["corpus_store"] = new_registration
    save_registry(resolved_project_root, registry)
    return deepcopy(new_registration)


def _reject_destination_symlinks(store_root: Path, lexical_destination: Path) -> None:
    current = Path(lexical_destination.anchor)
    for part in lexical_destination.parts[1:]:
        current /= part
        if not current.exists() and not current.is_symlink():
            continue
        if not current.is_symlink():
            continue
        resolved = current.resolve()
        if (
            _is_relative_to(current, store_root)
            or resolved == store_root
            or _is_relative_to(resolved, store_root)
        ):
            raise CorpusStoreError(
                f"Corpus destination traverses a symlink beneath the registered store: {current}"
            )


def _validate_destination(
    store_root: Path,
    destination: Path,
    *,
    source_roots: Sequence[Path],
    live_roots: Sequence[Path],
) -> Path:
    lexical_destination = _absolute_path(destination)
    _reject_destination_symlinks(store_root, lexical_destination)
    resolved_destination = lexical_destination.resolve(strict=False)
    if resolved_destination == store_root or not _is_relative_to(resolved_destination, store_root):
        raise CorpusStoreError(
            "Corpus destination must be a strict descendant of the registered store: "
            f"{resolved_destination}"
        )

    destination_symlink = _find_symlink(resolved_destination)
    if destination_symlink is not None:
        raise CorpusStoreError(
            "Corpus destination contains a symlink beneath the registered store: "
            f"{destination_symlink}"
        )

    for other in [*source_roots, *live_roots]:
        resolved_other = _absolute_path(other).resolve(strict=False)
        if (
            resolved_destination == resolved_other
            or _is_relative_to(resolved_destination, resolved_other)
            or _is_relative_to(resolved_other, resolved_destination)
        ):
            raise CorpusStoreError(
                "Corpus source/live root and destination overlap in either direction: "
                f"{resolved_other} <-> {resolved_destination}"
            )

    git_root = _git_root(store_root)
    if git_root is not None and not _git_path_is_ignored(git_root, resolved_destination):
        raise CorpusStoreError(f"Corpus destination is Git-visible: {resolved_destination}")
    return resolved_destination


def authorize_corpus_write(
    *,
    project_root: Path,
    destination: Path,
    corpus_store_root: Path | None = None,
    source_roots: Sequence[Path] = (),
    live_roots: Sequence[Path] = (),
) -> CorpusWriteAuthorization:
    resolved_project_root = _absolute_path(project_root).resolve(strict=False)
    configured_root = resolve_configured_corpus_store_root(corpus_store_root)
    registration = load_corpus_store_registration(resolved_project_root)
    registered_root = _resolve_existing_store_root(Path(registration["root"]))
    if configured_root != registered_root:
        raise CorpusStoreError(
            "Configured corpus-store root does not match the active registration: "
            f"{configured_root}"
        )
    validate_corpus_store_root(registered_root)
    resolved_destination = _validate_destination(
        registered_root,
        destination,
        source_roots=source_roots,
        live_roots=live_roots,
    )
    return CorpusWriteAuthorization(
        project_root=resolved_project_root,
        store_root=registered_root,
        destination=resolved_destination,
    )


def authorize_additional_corpus_path(
    authorization: CorpusWriteAuthorization,
    destination: Path,
) -> CorpusWriteAuthorization:
    resolved_destination = _validate_destination(
        authorization.store_root,
        destination,
        source_roots=(),
        live_roots=(),
    )
    return CorpusWriteAuthorization(
        project_root=authorization.project_root,
        store_root=authorization.store_root,
        destination=resolved_destination,
    )
