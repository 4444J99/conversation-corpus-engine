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


class CorpusStoreUnavailableError(CorpusStoreError):
    """The store, or the archive in it, is not there to attach to (an unmounted volume, a typo).

    Distinct from a custody refusal so the caller can say what is missing instead of telling the
    operator to register a store that may already be registered.
    """


@dataclass(frozen=True)
class CorpusWriteAuthorization:
    project_root: Path
    store_root: Path
    destination: Path


@dataclass(frozen=True)
class CorpusReadAuthorization:
    """Custody for a command that only reads an archive; it can never authorize a write.

    It carries the same three facts as a write authorization but is a different type on purpose
    (not a subclass): every write path demands `CorpusWriteAuthorization`, and the runtime guards
    in `require_inside` and `authorize_additional_corpus_path` refuse this one by type, so a
    read-only attachment cannot be mistaken for permission to write.
    """

    project_root: Path
    store_root: Path
    destination: Path


def require_write_authorization(authorization: object) -> CorpusWriteAuthorization:
    """Return `authorization` if it is write custody; refuse anything else, a read one included."""
    if not isinstance(authorization, CorpusWriteAuthorization):
        raise CorpusStoreError(
            "A write needs write custody, but this is a "
            f"{type(authorization).__name__}, not a CorpusWriteAuthorization"
        )
    return authorization


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


# Variables that tell git where a repository is, or hide it. Left in, GIT_DIR=/nonexistent makes git
# answer "not a git repository" for every directory, which the guards would read as "outside every
# work tree". Configuration variables (GIT_CONFIG_GLOBAL and the like) are kept: they are how a
# developer's own ignore rules reach git, and the guards are meant to honour them.
GIT_LOCATION_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_NAMESPACE",
)


def _git_environment() -> dict[str, str]:
    environment = {
        name: value for name, value in os.environ.items() if name not in GIT_LOCATION_VARIABLES
    }
    environment["LC_ALL"] = "C"  # git's messages are matched by text below
    return environment


def _run_git(cwd: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=False,
        capture_output=True,
        text=True,
        input=stdin,
        env=_git_environment(),
    )


def _enclosing_dot_git(path: Path) -> Path | None:
    """The nearest `.git` (a directory, or the file of a linked work tree) at or above `path`."""
    for candidate in (path, *path.parents):
        marker = candidate / ".git"
        if marker.exists() or marker.is_symlink():
            return marker
    return None


def _git_root(path: Path) -> Path | None:
    """The work tree `path` sits in, or None when it sits in none.

    None means exactly one thing: git said this is not a repository, and no `.git` exists at or
    above `path` to contradict it (git says the same of a damaged `.git`). Any other failure (no
    git binary, a permission error, an ownership refusal) is raised, because treating it as
    "outside every work tree" would let private text land somewhere Git can see.
    """
    try:
        result = _run_git(path, "rev-parse", "--show-toplevel")
    except OSError as exc:
        raise CorpusStoreError(f"Could not run git to inspect {path}: {exc}") from exc
    if result.returncode != 0:
        if result.returncode == NOT_A_REPOSITORY_EXIT and NOT_A_REPOSITORY in result.stderr:
            marker = _enclosing_dot_git(path)
            if marker is not None:
                raise CorpusStoreError(
                    f"git could not inspect {path}: it says this is not a repository, but "
                    f"{marker} exists, so the answer cannot be trusted"
                )
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
    return _validate_git_privacy(root)


def _validate_git_privacy(root: Path) -> Path | None:
    """The Git half of custody, from a handful of git calls and no walk of the store.

    A store outside every work tree is private. One inside a work tree must hold no tracked
    content, must be ignored, and must show no Git-visible file. Returns the work tree or None.
    """
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


# Names Git is asked about beneath a destination inside a work tree: private text written there
# must not be visible to Git under any of them. A rule that ignores only dotfiles, only one
# extension or only the first name cannot make the destination pass, and `x/y.md` also covers a
# negation that re-includes a subdirectory of an otherwise ignored parent.
PRIVATE_PROBE_CHILDREN = (".private-probe", "x.md", "x.json", "x.jsonl", "x/y.md")


def _check_ignore(git_root: Path, paths: Sequence[str]) -> set[str]:
    """Those of the work-tree-relative `paths` that Git ignores, judged by name.

    Nothing has to exist: `check-ignore` reads the ignore rules (every .gitignore, `info/exclude`
    and the global excludes file) and answers for the names given. `--no-index` keeps a tracked
    path from hiding a matching rule.
    """
    result = _run_git(
        git_root,
        "check-ignore",
        "--no-index",
        "--stdin",
        "-z",
        stdin="".join(f"{p}\0" for p in paths),
    )
    if result.returncode not in (0, 1):  # 1 means none of them is ignored
        raise CorpusStoreError(
            f"git could not check the ignore rules (exit {result.returncode}): "
            f"{result.stderr.strip() or 'no message'}"
        )
    return {item for item in result.stdout.split("\0") if item}


def _directory_pattern_ignores(git_root: Path, relative: str) -> bool:
    """Whether a rule that only matches directories ignores `relative`, a directory not yet made.

    Git cannot tell that a name which does not exist is a directory, so the bare name misses a
    rule like `out/`. Asked as `out/`, a rule like `out/*` (which ignores what is inside, not
    `out`) also answers yes, because `*` matches the empty name. The matching rule is therefore
    read back, and only one that ends in a slash, and is not a negation, counts.
    """
    result = _run_git(
        git_root, "check-ignore", "--no-index", "-v", "--stdin", "-z", stdin=f"{relative}/\0"
    )
    if result.returncode not in (0, 1):
        raise CorpusStoreError(
            f"git could not check the ignore rules (exit {result.returncode}): "
            f"{result.stderr.strip() or 'no message'}"
        )
    fields = result.stdout.split("\0")  # source, line number, pattern, path
    return len(fields) >= 4 and fields[2].endswith("/") and not fields[2].startswith("!")


def require_git_private(directory: Path) -> Path:
    """Refuse a directory that private text must not land in. Returns the resolved path.

    Outside every Git work tree a directory is private. Inside one it is private only when Git
    ignores the directory itself and also ignores the names a library or archive export writes
    beneath it (`PRIVATE_PROBE_CHILDREN`), and the directory holds no tracked file; otherwise the
    files written there would be untracked, Git-visible files. "Ignores" is Git's own answer from
    `git check-ignore --no-index`, so every rule counts (nested .gitignore files,
    `.git/info/exclude`, the global excludes file, negations) and nothing is created to find out.
    The directory may not exist yet. Symlinks are followed first, so the answer is for where the
    text would really land.

    Git runs without the variables that relocate or hide a repository (`GIT_LOCATION_VARIABLES`),
    and its "not a git repository" counts as "outside every work tree" only when no `.git` exists
    at or above the path. Any other failure to run or understand git is an error, never a pass.

    This says whether the text would be visible to Git, not that it is secret. It does not stop
    `git add -f`, a hook or tool that bypasses ignore rules, or a backup or sync client; it does
    not look at a repository's tracked files outside the directory; and a directory that ignores
    its own contents through an inner `.gitignore` of `*` is refused, because that file does not
    ignore the directory itself. See "Where private text may be written" in
    docs/knowledge-library.md.
    """
    resolved = _absolute_path(directory).resolve(strict=False)
    probe = resolved
    while not probe.exists():
        probe = probe.parent
    git_root = _git_root(probe)
    if git_root is None:
        return resolved

    def visible(why: str) -> CorpusStoreError:
        return CorpusStoreError(
            f"Directory is Git-visible and cannot hold private text: {resolved} ({why})"
        )

    try:
        relative = resolved.relative_to(git_root).as_posix()
    except ValueError:
        raise visible(f"it is not under the work tree {git_root} that Git reported") from None
    if relative == ".":
        raise visible("it is the work tree itself")
    exists = resolved.exists()
    if exists:
        tracked = _run_git(git_root, "ls-files", "-z", "--", relative)
        if tracked.returncode != 0:
            raise CorpusStoreError(f"Could not list the tracked files beneath {resolved}")
        if tracked.stdout:
            raise visible("it holds Git-tracked files")
    children = [f"{relative}/{name}" for name in PRIVATE_PROBE_CHILDREN]
    ignored = _check_ignore(git_root, [relative, *children])
    if relative not in ignored and (exists or not _directory_pattern_ignores(git_root, relative)):
        raise visible("Git does not ignore the directory itself")
    for child in children:
        if child not in ignored:
            raise visible(f"Git would show {child.removeprefix(relative + '/')} written inside it")
    return resolved


def _resolve_existing_store_root(root: Path) -> Path:
    lexical_root = _absolute_path(root)
    if lexical_root.is_symlink():
        raise CorpusStoreError(f"Corpus-store root may not be a symlink: {lexical_root}")
    try:
        resolved_root = lexical_root.resolve(strict=True)
    except FileNotFoundError as exc:
        raise CorpusStoreUnavailableError(
            f"Corpus-store root does not exist (its volume may be unmounted): {lexical_root}"
        ) from exc
    if not resolved_root.is_dir():
        raise CorpusStoreUnavailableError(f"Corpus-store root is not a directory: {resolved_root}")
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


def _place_destination(store_root: Path, destination: Path) -> Path:
    """Where `destination` lands, refused unless it is a strict descendant of the store.

    Looks only at the path itself: no component of it may be a symlink into the store, and it may
    not be the store or sit outside it. Nothing beneath the destination is walked.
    """
    lexical_destination = _absolute_path(destination)
    _reject_destination_symlinks(store_root, lexical_destination)
    resolved_destination = lexical_destination.resolve(strict=False)
    if resolved_destination == store_root or not _is_relative_to(resolved_destination, store_root):
        raise CorpusStoreError(
            "Corpus destination must be a strict descendant of the registered store: "
            f"{resolved_destination}"
        )
    return resolved_destination


def _require_destination_git_ignored(store_root: Path, resolved_destination: Path) -> None:
    git_root = _git_root(store_root)
    if git_root is not None and not _git_path_is_ignored(git_root, resolved_destination):
        raise CorpusStoreError(f"Corpus destination is Git-visible: {resolved_destination}")


def _validate_destination(
    store_root: Path,
    destination: Path,
    *,
    source_roots: Sequence[Path],
    live_roots: Sequence[Path],
) -> Path:
    resolved_destination = _place_destination(store_root, destination)

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

    _require_destination_git_ignored(store_root, resolved_destination)
    return resolved_destination


def _attach_registered_store(
    project_root: Path, corpus_store_root: Path | None
) -> tuple[Path, Path]:
    """The resolved project root and the registered store root the configuration names.

    The cheap attachment properties both kinds of custody share: the configured root exists, is a
    directory and is no symlink; the registration is active; and it names the configured root.
    """
    resolved_project_root = _absolute_path(project_root).resolve(strict=False)
    configured_root = resolve_configured_corpus_store_root(corpus_store_root)
    registration = load_corpus_store_registration(resolved_project_root)
    registered_root = _resolve_existing_store_root(Path(registration["root"]))
    if configured_root != registered_root:
        raise CorpusStoreError(
            "Configured corpus-store root does not match the active registration: "
            f"{configured_root}"
        )
    return resolved_project_root, registered_root


def authorize_corpus_read(
    project_root: Path,
    archive_root: Path,
    corpus_store_root: Path | None = None,
) -> CorpusReadAuthorization:
    """Attach a read-only command to an archive in the registered store, without the tree walks.

    Write custody proves the whole store holds no nested Git metadata and no symlink, which walks
    every directory of it (tens of seconds on a large store). A command that writes nothing needs
    only the cheap properties, and that is exactly what this keeps: the configured root resolves
    and the registration is active and names it; the archive root is a strict descendant of the
    store with no symlink on the way to it; the archive root exists and is a directory; and the
    store is Git-private by the same handful of git calls write custody makes.

    What it deliberately leaves out is `_find_nested_git_metadata` and `_find_symlink`, the two
    walks. The result is a `CorpusReadAuthorization`, which no write path accepts.
    """
    resolved_project_root, registered_root = _attach_registered_store(
        project_root, corpus_store_root
    )
    _validate_git_privacy(registered_root)
    resolved_archive_root = _place_destination(registered_root, archive_root)
    _require_destination_git_ignored(registered_root, resolved_archive_root)
    if not resolved_archive_root.is_dir():
        raise CorpusStoreUnavailableError(
            f"Archive root does not exist or is not a directory: {resolved_archive_root}"
        )
    return CorpusReadAuthorization(
        project_root=resolved_project_root,
        store_root=registered_root,
        destination=resolved_archive_root,
    )


def authorize_corpus_write(
    *,
    project_root: Path,
    destination: Path,
    corpus_store_root: Path | None = None,
    source_roots: Sequence[Path] = (),
    live_roots: Sequence[Path] = (),
) -> CorpusWriteAuthorization:
    resolved_project_root, registered_root = _attach_registered_store(
        project_root, corpus_store_root
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
    require_write_authorization(authorization)
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
