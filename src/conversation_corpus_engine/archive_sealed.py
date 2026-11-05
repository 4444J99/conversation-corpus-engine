"""Ciphertext-only publication as an opt-in layer over existing archive files.

All metadata lives inside the signed encrypted manifest. The destination is a dedicated
ciphertext tree (optionally a Git worktree), never the CCE code repository or private store.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path, PurePosixPath
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from .archive_sealed_crypto import (
    FORMAT,
    Identity,
    SealedError,
    address,
    authenticate,
    authenticate_object,
    canonical,
    decrypt,
    digest,
    encrypt,
    sign,
    sign_object,
    unwrap,
    wrap,
)
from .archive_sealed_units import project_units
from .corpus_store import CorpusStoreError, _git_root, require_git_private

CHUNK = 512 * 1024
DEFAULT_BUDGET = 512 * 1024 * 1024
MAX_MANIFEST = 16 * 1024 * 1024
MAX_UNIT = 4 * 1024 * 1024
HISTORY_GIT_TIMEOUT_SECONDS = 30
HISTORY_MAX_COMMITS = 10000
HEX = re.compile(r"[0-9a-f]{64}")


def safe_path(path: Path) -> Path:
    path = Path(os.path.abspath(path.expanduser()))  # noqa: PTH100 - inspect before resolving
    if any(part.is_symlink() for part in [path, *path.parents]):
        raise SealedError("symlink-refused")
    return path


def private_path(path: Path) -> Path:
    path = safe_path(path)
    parent = path if path.is_dir() else path.parent
    while not parent.exists():
        parent = parent.parent
    try:
        require_git_private(path if path.is_dir() else path.parent)
        if _git_root(parent) is not None:
            raise SealedError("private-path-in-git")
    except CorpusStoreError as exc:
        raise SealedError("private-path-in-git") from exc
    return path


def _publish_exclusive(temp: Path, path: Path) -> None:
    """Make a fully written temp file visible at `path` only if nothing exists there.

    A hard link is an atomic create-if-absent: an interrupted or failed write can never leave a
    truncated object at its final name, and a concurrent creator surfaces as FileExistsError.
    """
    try:
        os.link(temp, path)
    except FileExistsError:
        raise
    except OSError:
        # Filesystem without hard links: callers hold the publisher lock, so check then rename.
        if path.exists() or path.is_symlink():
            raise FileExistsError(path) from None
        temp.replace(path)


def _sync_directory(directory: Path) -> None:
    # Best effort: make the new directory entry durable. Not every platform can open a directory.
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        with suppress(OSError):
            os.fsync(fd)
    finally:
        os.close(fd)


def _make_private_dirs(directory: Path) -> None:
    """Create missing directories (ancestors included) as 0700; existing ones are untouched.

    Path.mkdir(parents=True, mode=...) applies the mode to the leaf only, leaving new ancestors at
    the umask default (typically 0755), so each missing level is created and set explicitly.
    """
    missing = []
    probe = directory
    while not probe.exists() and probe != probe.parent:
        missing.append(probe)
        probe = probe.parent
    for created in reversed(missing):
        try:
            created.mkdir(mode=0o700)
        except FileExistsError:
            continue
        created.chmod(0o700)


def _write(path: Path, data: bytes, *, exclusive: bool = False) -> None:
    """Write via a same-directory temp file, fsync, then rename/link; remove the temp on failure."""
    safe_path(path)
    _make_private_dirs(path.parent)
    fd, name = tempfile.mkstemp(prefix=".cce-", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if exclusive:
            _publish_exclusive(temp, path)
        else:
            temp.replace(path)
        _sync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def create_legacy_identity(path: Path, app_id: str, instance_id: str) -> Identity:
    if not app_id or not instance_id:
        raise SealedError("identity-label-required")
    identity = Identity(
        app_id,
        instance_id,
        X25519PrivateKey.generate().private_bytes_raw(),
        Ed25519PrivateKey.generate().private_bytes_raw(),
    )
    _write(private_path(path), canonical(identity.to_dict()), exclusive=True)
    return identity


def load_legacy_identity(path: Path) -> Identity:
    path = private_path(path)
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise SealedError("credential-permissions")
    data = json.loads(path.read_bytes())
    if data["format"] != FORMAT:
        raise SealedError("unsupported-format")
    return Identity(
        data["app_id"],
        data["instance_id"],
        bytes.fromhex(data["encryption_key"]),
        bytes.fromhex(data["signing_key"]),
    )


def create_identity(path: Path, app_id: str, instance_id: str, *, passphrase: bytes) -> Identity:
    """Create a passphrase-protected owner/device credential, never a plaintext key file."""
    from .archive_sealed_credentials import (  # noqa: PLC0415
        protect_identity,
        require_new_passphrase,
    )

    require_new_passphrase(passphrase)
    path = private_path(path)
    if not app_id or not instance_id:
        raise SealedError("identity-label-required")
    identity = Identity(
        app_id,
        instance_id,
        X25519PrivateKey.generate().private_bytes_raw(),
        Ed25519PrivateKey.generate().private_bytes_raw(),
    )
    _write(path, protect_identity(identity, passphrase), exclusive=True)
    return identity


def load_identity(path: Path, *, passphrase: bytes | None = None) -> Identity:
    """Unlock protected credentials; legacy plaintext requires the separate named reader."""
    from .archive_sealed_credentials import unlock_identity  # noqa: PLC0415

    path = private_path(path)
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise SealedError("credential-permissions")
    if path.stat().st_size > 65536:
        raise SealedError("credential-unlock-failed")
    return unlock_identity(path.read_bytes(), passphrase)


def create_trust(path: Path, owner: Identity) -> Path:
    path = private_path(path)
    _write(
        path,
        canonical(
            {
                "format": FORMAT,
                "vault": os.urandom(32).hex(),
                "owner": owner.signing_public(),
                "generation": 0,
                "head": "",
            }
        ),
        exclusive=True,
    )
    return path


def _trust(path: Path) -> dict[str, Any]:
    trust = json.loads(private_path(path).read_bytes())
    if trust["format"] != FORMAT or not HEX.fullmatch(trust["vault"]):
        raise SealedError("invalid-trust")
    return trust


def _relative(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in name
        or ":" in name
        or path.as_posix() != name
        or any(part.lower() == ".git" for part in path.parts)
    ):
        raise SealedError("unsafe-path")
    return path


def _object(repo: Path, oid: str) -> Path:
    if not isinstance(oid, str) or not HEX.fullmatch(oid):
        raise SealedError("invalid-address")
    return safe_path(repo / "objects" / oid[:2] / oid)


def _tree(repo: Path) -> None:
    safe_path(repo)
    if not repo.exists():
        return
    lock = repo / ".cce-lock"
    if lock.exists():
        safe_path(lock)
        if not lock.is_dir() or any(lock.iterdir()):
            raise SealedError("non-ciphertext-tree")
    for base, dirs, files in os.walk(repo, followlinks=False):
        root = Path(base)
        if root == repo:
            dirs[:] = [d for d in dirs if d not in {".git", ".cce-lock"}]
        for name in [*dirs, *files]:
            path = safe_path(root / name)
            rel = path.relative_to(repo).as_posix()
            if rel in {".git", "HEAD.cce", "objects"}:
                continue
            if path.is_dir() and re.fullmatch(r"objects/[0-9a-f]{2}", rel):
                continue
            if (
                path.is_file()
                and re.fullmatch(r"objects/[0-9a-f]{2}/[0-9a-f]{64}", rel)
                and path.parent.name == path.name[:2]
            ):
                continue
            raise SealedError("non-ciphertext-tree")


@contextmanager
def _lock(repo: Path) -> Iterator[None]:
    repo.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = safe_path(repo / ".cce-lock")
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise SealedError("publisher-locked") from exc
    try:
        yield
    finally:
        lock.rmdir()


def _open(repo: Path, identity: Identity, trust: dict[str, Any]) -> tuple[dict, dict, bytes]:
    _tree(repo)
    if (repo / "HEAD.cce").stat().st_size > MAX_MANIFEST * 3:
        raise SealedError("manifest-too-large")
    raw = (repo / "HEAD.cce").read_bytes()
    head = json.loads(raw)
    if raw != canonical(head):
        raise SealedError("noncanonical-head")
    authenticate(head, trust["owner"])
    if head["format"] != FORMAT or head["vault"] != trust["vault"]:
        raise SealedError("wrong-vault")
    generation = head["generation"]
    if generation < trust["generation"] or (
        generation == trust["generation"] and digest(raw) != trust["head"]
    ):
        raise SealedError("rollback-or-equivocation")
    context = f"{FORMAT}:{head['vault']}:{generation}"
    secret = unwrap(head["envelopes"], identity, context)
    manifest = json.loads(decrypt(secret, bytes.fromhex(head["manifest"]), context + ":manifest"))
    return head, manifest, secret


def _accept(trust_path: Path, trust: dict[str, Any], head: dict) -> None:
    # Pin the HEAD actually authenticated, even if a concurrent writer replaces HEAD.cce.
    checkpoint = {**trust, "generation": head["generation"], "head": digest(canonical(head))}
    lock = private_path(trust_path.with_name(trust_path.name + ".lock"))
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise SealedError("checkpoint-locked") from exc
    try:
        current = _trust(trust_path)
        if current["owner"] != trust["owner"] or current["vault"] != trust["vault"]:
            raise SealedError("checkpoint-conflict")
        if current["generation"] > checkpoint["generation"]:
            raise SealedError("checkpoint-advanced")
        if (
            current["generation"] == checkpoint["generation"]
            and current["head"]
            and current["head"] != checkpoint["head"]
        ):
            raise SealedError("checkpoint-conflict")
        _write(private_path(trust_path), canonical(checkpoint))
    finally:
        lock.rmdir()


def _read_object(repo: Path, oid: str, info: dict, secret: bytes) -> bytes:
    try:
        path = _object(repo, oid)
        if path.stat().st_size > MAX_UNIT + 8192:
            raise SealedError("object-too-large")
        data = path.read_bytes()
        if digest(data) != info["cipher_sha256"]:
            raise SealedError("corrupt-object")
        plain = decrypt(secret, data[64:], FORMAT + ":object:" + oid)
        if address(secret, plain) != oid:
            raise SealedError("invalid-address")
        return plain
    except OSError as exc:
        raise SealedError("missing-object") from exc


def _check(repo: Path, manifest: dict, secret: bytes) -> None:
    for oid, info in manifest["objects"].items():
        _read_object(repo, oid, info, secret)
    for name, recipe in manifest["files"].items():
        _relative(name)
        checksum = hashlib.sha256()
        for oid in recipe["chunks"]:
            checksum.update(_read_object(repo, oid, manifest["objects"][oid], secret))
        if checksum.hexdigest() != recipe["sha256"]:
            raise SealedError("reconstruction-failed")
    for unit_id, oid in manifest["atoms"].items():
        unit = json.loads(_read_object(repo, oid, manifest["objects"][oid], secret))
        if unit["id"] != unit_id:
            raise SealedError("unit-id-mismatch")


def _check_retained(repo: Path, trust: dict) -> None:
    # Authenticate old epochs without disclosing their plaintext to new recipients.
    for path in (repo / "objects").glob("*/*"):
        if path.stat().st_size > MAX_UNIT + 8192:
            raise SealedError("object-too-large")
        authenticate_object(path.read_bytes(), path.name, trust["vault"], trust["owner"])


def publish(
    source: Path,
    selection: list[str],
    repo: Path,
    identity: Identity,  # noqa: PLR0917
    trust_path: Path,
    recipients: list[dict[str, str]],
    *,
    rotate: bool = False,
    budget: int = DEFAULT_BUDGET,
    audit_budget: int | None = None,
    _plan_only: bool = False,
) -> dict[str, Any]:
    source, repo = safe_path(source), safe_path(repo)
    try:
        require_git_private(source)
    except CorpusStoreError as exc:
        raise SealedError("source-not-private") from exc
    if source.is_relative_to(repo) or repo.is_relative_to(source):
        raise SealedError("overlapping-roots")
    probe = repo
    while not probe.exists():
        probe = probe.parent
    git_root = _git_root(probe)
    if git_root is not None and git_root != repo:
        raise SealedError("dedicated-ciphertext-repository-required")
    trust = _trust(trust_path)
    if identity.signing_public() != trust["owner"]:
        raise SealedError("not-publisher")
    if not recipients or len(recipients) > 64 or len(selection) != len(set(selection)):
        raise SealedError("invalid-selection-or-recipients")
    recipients = sorted(recipients, key=canonical)
    if len({r["encryption_public"] for r in recipients}) != len(recipients):
        raise SealedError("duplicate-recipient")
    if identity.recipient() not in recipients:
        raise SealedError("publisher-must-be-recipient")
    for name in selection:
        safe_path(source / _relative(name))
    _tree(repo)
    _check_retained(repo, trust)
    if git_root is not None:
        audit_history(repo, identity, trust_path, budget=audit_budget)
    if budget < 0:
        raise SealedError("invalid-budget")
    if _plan_only:
        return _publish(
            source,
            sorted(selection),
            repo,
            identity,
            trust_path,
            trust,
            recipients,
            rotate,
            budget,
            plan_only=True,
        )
    with _lock(repo):
        return _publish(
            source, sorted(selection), repo, identity, trust_path, trust, recipients, rotate, budget
        )


def _publish(
    source: Path,
    selection: list[str],
    repo: Path,
    identity: Identity,  # noqa: PLR0917
    trust_path: Path,
    trust: dict,
    recipients: list[dict[str, str]],
    rotate: bool,
    budget: int,
    *,
    plan_only: bool = False,
) -> dict[str, Any]:
    previous: dict = {}
    secret = os.urandom(32)
    generation = 1
    parent = ""
    if (repo / "HEAD.cce").exists():
        head, previous, old_secret = _open(repo, identity, trust)
        _check(repo, previous, old_secret)
        generation = head["generation"] + 1
        parent = digest((repo / "HEAD.cce").read_bytes())
        if previous["recipients"] != recipients and not rotate:
            raise SealedError("recipient-change-requires-rotation")
        if not rotate:
            secret = old_secret
    elif trust["generation"]:
        raise SealedError("missing-head")

    pending: dict[str, bytes] = {}
    objects: dict[str, dict] = {}
    pending_bytes = 0

    def add(data: bytes) -> str:
        nonlocal pending_bytes
        if len(data) > MAX_UNIT:
            raise SealedError("unit-too-large")
        oid = address(secret, data)
        if oid not in objects:
            if not rotate and oid in previous.get("objects", {}):
                objects[oid] = previous["objects"][oid]
            elif not rotate and (retained := _object(repo, oid)).is_file():
                # Content addresses survive deselection and A→B→A revisions. Reuse the
                # authenticated original ciphertext; fresh nonces would collide at that path.
                if retained.stat().st_size > MAX_UNIT + 8192:
                    raise SealedError("object-too-large")
                cipher = retained.read_bytes()
                authenticate_object(cipher, oid, trust["vault"], trust["owner"])
                if decrypt(secret, cipher[64:], FORMAT + ":object:" + oid) != data:
                    raise SealedError("object-collision")
                objects[oid] = {"cipher_sha256": digest(cipher)}
            else:
                cipher = sign_object(
                    encrypt(secret, data, FORMAT + ":object:" + oid), oid, trust["vault"], identity
                )
                pending_bytes += len(cipher)
                if pending_bytes > budget and not plan_only:
                    raise SealedError("storage-budget-exceeded")
                pending[oid] = b"" if plan_only else cipher
                objects[oid] = {"cipher_sha256": digest(cipher)}
        return oid

    files: dict[str, dict] = {}
    atoms: dict[str, str] = {}
    selected_bytes = 0
    for name in selection:
        path = safe_path(source / _relative(name))
        if not path.is_file():
            raise SealedError("source-not-regular-file")
        before = path.stat()
        selected_bytes += before.st_size
        chunks = []
        # Byte recipes preserve JSON whitespace, unknown fields and binary export artifacts.
        checksum = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(CHUNK), b""):
                checksum.update(block)
                chunks.append(add(block))
        files[name] = {"chunks": chunks, "sha256": checksum.hexdigest()}
        for unit in project_units(path, name):
            unit_id = unit["id"]
            if unit_id in atoms:
                raise SealedError("duplicate-unit")
            atoms[unit_id] = add(canonical(unit))
        after = path.stat()
        if any(
            getattr(before, field) != getattr(after, field)
            for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        ):
            raise SealedError("source-changed-during-publication")
    manifest = {
        "format": FORMAT,
        "files": files,
        "atoms": atoms,
        "objects": objects,
        "recipients": recipients,
    }
    if not rotate and manifest == previous:
        capacity = _capacity(
            repo, selected_bytes, 0, (repo / "HEAD.cce").stat().st_size, budget, changed=False
        )
        if plan_only:
            return capacity
        if not capacity["fits"]:
            raise SealedError("storage-budget-exceeded")
        _accept(trust_path, trust, head)
        return {"changed": False, "files": len(files), "units": len(atoms)}
    plain_manifest = canonical(manifest)
    if len(plain_manifest) > MAX_MANIFEST:
        raise SealedError("manifest-too-large")
    context = f"{FORMAT}:{trust['vault']}:{generation}"
    head = sign(
        {
            "format": FORMAT,
            "vault": trust["vault"],
            "generation": generation,
            "previous": parent,
            "envelopes": [wrap(secret, r, context) for r in recipients],
            "manifest": encrypt(secret, plain_manifest, context + ":manifest").hex(),
        },
        identity,
    )
    encoded = canonical(head)
    capacity = _capacity(
        repo,
        selected_bytes,
        pending_bytes,
        len(encoded),
        budget,
        changed=True,
        pending_objects=len(pending),
    )
    if plan_only:
        return capacity
    total = capacity["planned_tree_bytes"]
    if not capacity["fits"]:
        raise SealedError("storage-budget-exceeded")
    for oid, data in pending.items():
        path = _object(repo, oid)
        if path.exists():
            if path.read_bytes() != data:
                raise SealedError("object-collision")
        else:
            _write(path, data, exclusive=True)
    _write(repo / "HEAD.cce", encoded)
    _accept(trust_path, trust, head)
    return {
        "changed": True,
        "files": len(files),
        "units": len(atoms),
        "live_bytes": total,
        "generation": generation,
    }


def _capacity(
    repo: Path,
    selected: int,
    pending: int,
    head_bytes: int,
    budget: int,
    *,
    changed: bool,
    pending_objects: int = 0,
) -> dict[str, Any]:
    retained = sum(p.stat().st_size for p in (repo / "objects").glob("*/*") if p.is_file())
    retained_count = sum(1 for p in (repo / "objects").glob("*/*") if p.is_file())
    current = retained + ((repo / "HEAD.cce").stat().st_size if (repo / "HEAD.cce").exists() else 0)
    history = _git_bytes(repo)
    tree = retained + pending + head_bytes
    # Reserve for loose-object allocation, rewritten trees, a commit and repository metadata.
    # Git count-objects reports allocated KiB, not raw ciphertext length. A source-byte-only
    # estimate undercounts even the first commit. This headroom is a local forecast, not a
    # guarantee about arbitrary filesystem allocation or a remote host's quota accounting.
    git_reserve = (1024 * 1024 + 4096 * (retained_count + pending_objects + 1)) if changed else 0
    planned_history = history + (pending + head_bytes + git_reserve if changed else 0)
    return {
        "selected_plaintext_bytes": selected,
        "current_ciphertext_bytes": current,
        "retained_object_bytes": retained,
        "pending_ciphertext_bytes": pending,
        "git_history_bytes": history,
        "git_metadata_reserve_bytes": git_reserve,
        "planned_tree_bytes": tree,
        "planned_history_bytes": planned_history,
        "planned_total_bytes": tree + planned_history,
        "budget": budget,
        "fits": tree + planned_history <= budget,
    }


def plan_capacity(
    source: Path,
    selection: list[str],
    repo: Path,
    identity: Identity,
    trust_path: Path,
    recipients: list[dict[str, str]],
    *,
    rotate: bool = False,
    budget: int = DEFAULT_BUDGET,
    audit_budget: int | None = None,
) -> dict[str, Any]:
    """Forecast selected content and a conservative next Git snapshot without writing.

    Sizes are deterministic for unchanged inputs; encryption nonces never affect lengths.
    Re-run immediately before publish: this observation is not a capacity reservation.
    """
    return publish(
        source,
        selection,
        repo,
        identity,
        trust_path,
        recipients,
        rotate=rotate,
        budget=budget,
        audit_budget=audit_budget,
        _plan_only=True,
    )


def verify(repo: Path, identity: Identity, trust_path: Path) -> dict[str, int]:
    repo = safe_path(repo)
    trust = _trust(trust_path)
    head, manifest, secret = _open(repo, identity, trust)
    _check(repo, manifest, secret)
    _check_retained(repo, trust)
    _accept(trust_path, trust, head)
    return {
        "files": len(manifest["files"]),
        "units": len(manifest["atoms"]),
        "objects": len(manifest["objects"]),
        "generation": head["generation"],
    }


def restore(repo: Path, target: Path, identity: Identity, trust_path: Path) -> dict[str, int]:
    repo, target = safe_path(repo), private_path(target)
    if target.exists() or target.is_relative_to(repo) or repo.is_relative_to(target):
        raise SealedError("restore-target-refused")
    trust = _trust(trust_path)
    head, manifest, secret = _open(repo, identity, trust)
    _check(repo, manifest, secret)
    _check_retained(repo, trust)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage = Path(tempfile.mkdtemp(prefix=".cce-restore-", dir=target.parent))
    try:
        for name, recipe in manifest["files"].items():
            path = stage / _relative(name)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            checksum = hashlib.sha256()
            with path.open("xb") as handle:
                path.chmod(0o600)
                for oid in recipe["chunks"]:
                    data = _read_object(repo, oid, manifest["objects"][oid], secret)
                    checksum.update(data)
                    handle.write(data)
            if checksum.hexdigest() != recipe["sha256"]:
                raise SealedError("reconstruction-failed")
        # Never replace user files. A concurrent creator causes rename to fail for nonempty dirs.
        if target.exists():
            raise SealedError("restore-target-refused")
        _accept(trust_path, trust, head)
        stage.rename(target)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {"files": len(manifest["files"]), "generation": head["generation"]}


def read_unit(repo: Path, unit_id: str, identity: Identity, trust_path: Path) -> dict[str, Any]:
    """Return plaintext to a private caller; the CLI writes it only to an explicit private file."""
    repo = safe_path(repo)
    trust = _trust(trust_path)
    head, manifest, secret = _open(repo, identity, trust)
    oid = manifest["atoms"].get(unit_id)
    if oid is None:
        raise SealedError("unit-not-found")
    result = json.loads(_read_object(repo, oid, manifest["objects"][oid], secret))
    _accept(trust_path, trust, head)
    return result


def repair(repo: Path, mirror: Path, identity: Identity, trust_path: Path) -> dict[str, Any]:
    repo, mirror = safe_path(repo), safe_path(mirror)
    trust = _trust(trust_path)
    _tree(repo)
    observed = (repo / "HEAD.cce").read_bytes() if (repo / "HEAD.cce").exists() else None
    repaired_head = False
    try:
        head, manifest, secret = _open(repo, identity, trust)
    except (OSError, ValueError):
        head, manifest, secret = _open(mirror, identity, trust)
        if digest(canonical(head)) != trust["head"]:
            raise SealedError("recovery-checkpoint-mismatch") from None
        repaired_head = True
    replacements = {}
    for oid, info in manifest["objects"].items():
        try:
            _read_object(repo, oid, info, secret)
        except SealedError:
            _read_object(mirror, oid, info, secret)
            replacements[oid] = _object(mirror, oid).read_bytes()
    with _lock(repo):
        current = (repo / "HEAD.cce").read_bytes() if (repo / "HEAD.cce").exists() else None
        if current != observed or _trust(trust_path) != trust:
            raise SealedError("head-changed")
        for oid, data in replacements.items():
            _write(_object(repo, oid), data)
        if repaired_head:
            _write(repo / "HEAD.cce", canonical(head))
    _check(repo, manifest, secret)
    _accept(trust_path, trust, head)
    return {"repaired": len(replacements), "repaired_head": repaired_head}


def audit_history(
    repo: Path, identity: Identity, trust_path: Path, *, budget: int | None = None
) -> dict[str, int]:
    """Authenticate reachable history with bounded Git output and hash-only blob caching.

    `budget` caps the total Git output inspected and, at one entry per 4 KiB, the retained hash
    metadata; it defaults to DEFAULT_BUDGET and is independent of the storage budget.
    Historical credentials are required. Unreachable/reflog/server-side objects still require
    separate host custody inspection. Neither Git diagnostics nor private paths are echoed.
    """
    import selectors  # noqa: PLC0415 - keep the bounded subprocess helper local to the audit
    import time  # noqa: PLC0415

    cap = DEFAULT_BUDGET if budget is None else budget
    if cap < 0:
        raise SealedError("invalid-budget")
    repo = safe_path(repo)
    trust = _trust(trust_path)
    inspected = 0

    def git(*args: str, limit: int = 1024 * 1024) -> bytes:
        nonlocal inspected
        limit = min(limit, cap - inspected)
        if limit <= 0:
            raise SealedError("history-audit-limit")
        try:
            with subprocess.Popen(
                ["git", "-C", str(repo), *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            ) as process:
                try:
                    assert process.stdout is not None
                    data = bytearray()
                    deadline = time.monotonic() + HISTORY_GIT_TIMEOUT_SECONDS
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ)
                        while True:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0 or not selector.select(remaining):
                                raise SealedError("history-inspection-failed")
                            block = os.read(
                                process.stdout.fileno(), min(65536, limit + 1 - len(data))
                            )
                            if not block:
                                break
                            inspected += len(block)
                            if len(data) + len(block) > limit:
                                raise SealedError("history-audit-limit")
                            data.extend(block)
                    if process.wait(timeout=max(0.001, deadline - time.monotonic())):
                        raise SealedError("history-inspection-failed")
                    return bytes(data)
                except BaseException:
                    process.kill()
                    process.wait()
                    raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise SealedError("history-inspection-failed") from exc

    refs = git("rev-list", f"--max-count={HISTORY_MAX_COMMITS + 1}", "--all").decode().splitlines()
    if len(refs) > HISTORY_MAX_COMMITS:
        raise SealedError("history-audit-limit")
    for ref in git("for-each-ref", "--format=%(refname) %(objecttype)").decode().splitlines():
        name, kind = ref.split(" ", 1)
        if kind != "commit" or not re.fullmatch(
            r"refs/(heads/(main|master)|remotes/[a-z0-9-]+/(main|master|HEAD)|tags/snapshot-[0-9]+)",
            name,
        ):
            raise SealedError("history-ref-refused")
    allowed: dict[str, set[str]] = {}
    observed: dict[str, set[str]] = {}
    blobs: dict[str, str] = {}
    heads: set[str] = set()
    object_oids: dict[str, str] = {}
    # Minimum padded objects are 4 KiB; also bound hash/set overhead independently of bytes.
    metadata_limit = max(1, cap // 4096)
    allowed_entries = 0

    def allow(manifest: dict) -> None:
        nonlocal allowed_entries
        for oid, info in manifest["objects"].items():
            checksum = info["cipher_sha256"]
            if checksum not in allowed.get(oid, set()):
                if allowed_entries >= metadata_limit:
                    raise SealedError("history-audit-limit")
                allowed.setdefault(oid, set()).add(checksum)
                allowed_entries += 1

    if (repo / "HEAD.cce").is_file():
        _, current, _ = _open(repo, identity, trust)
        allow(current)
    elif refs:
        raise SealedError("history-not-empty")
    for ref in refs:
        if (
            git("show", "-s", "--format=%B", ref, limit=1024).strip()
            != b"chore: update encrypted archive"
        ):
            raise SealedError("history-commit-message-refused")
        found_head = False
        for entry in git("ls-tree", "-r", "-l", "-z", ref, limit=MAX_MANIFEST * 3).split(b"\0"):
            if not entry:
                continue
            meta, name_bytes = entry.split(b"\t", 1)
            mode, object_kind, blob_bytes, size_bytes = meta.split()
            name, blob = name_bytes.decode(), blob_bytes.decode()
            if mode != b"100644" or object_kind != b"blob":
                raise SealedError("history-entry-refused")
            is_head = name == "HEAD.cce"
            if not is_head and not re.fullmatch(r"objects/[0-9a-f]{2}/[0-9a-f]{64}", name):
                raise SealedError("history-path-refused")
            if not is_head and name.split("/")[1] != name.split("/")[2][:2]:
                raise SealedError("history-path-refused")
            size = int(size_bytes)
            maximum = MAX_MANIFEST * 3 if is_head else MAX_UNIT + 8192
            if size < 0 or size > maximum:
                raise SealedError("history-blob-too-large")
            oid = "" if is_head else name.rsplit("/", 1)[1]
            if not is_head and blob in object_oids and object_oids[blob] != oid:
                raise SealedError("history-blob-alias-refused")
            if (
                blob not in blobs
                or (is_head and blob not in heads)
                or (not is_head and blob not in object_oids)
            ):
                if blob not in blobs and len(blobs) >= metadata_limit:
                    raise SealedError("history-audit-limit")
                raw = git("cat-file", "blob", blob, limit=size + 1)
                if len(raw) != size:
                    raise SealedError("history-blob-refused")
                if is_head:
                    head = json.loads(raw)
                    if raw != canonical(head):
                        raise SealedError("history-head-refused")
                    authenticate(head, trust["owner"])
                    if head["vault"] != trust["vault"] or head["format"] != FORMAT:
                        raise SealedError("history-vault-refused")
                    context = f"{FORMAT}:{head['vault']}:{head['generation']}"
                    secret = unwrap(head["envelopes"], identity, context)
                    plain = decrypt(secret, bytes.fromhex(head["manifest"]), context + ":manifest")
                    if len(plain) > MAX_MANIFEST:
                        raise SealedError("manifest-too-large")
                    allow(json.loads(plain))
                    heads.add(blob)
                    del head, plain
                else:
                    # Verify the owner/address binding before caching or retaining any path.
                    authenticate_object(raw, oid, trust["vault"], trust["owner"])
                    object_oids[blob] = oid
                blobs[blob] = digest(raw)
                del raw
            if is_head:
                found_head = True
            else:
                observed.setdefault(oid, set()).add(blobs[blob])
        if not found_head:
            raise SealedError("history-head-missing")
    for oid, hashes in observed.items():
        if not hashes <= allowed.get(oid, set()):
            raise SealedError("history-blob-refused")
    # Authenticate retained working-tree objects with the same per-object read bound.
    _check_retained(repo, trust)
    return {"commits": len(refs), "blobs": len(blobs)}


def list_units(repo: Path, identity: Identity, trust_path: Path) -> list[str]:
    trust = _trust(trust_path)
    head, manifest, _ = _open(safe_path(repo), identity, trust)
    _accept(trust_path, trust, head)
    return sorted(manifest["atoms"])


def storage_status(repo: Path, identity: Identity, trust_path: Path) -> dict[str, int]:
    result = verify(repo, identity, trust_path)
    result["ciphertext_tree_bytes"] = (
        sum(p.stat().st_size for p in (repo / "objects").glob("*/*") if p.is_file())
        + (repo / "HEAD.cce").stat().st_size
    )
    result["git_object_store_bytes"] = _git_bytes(repo)
    return result


def _git_bytes(repo: Path) -> int:
    """Git object-store bytes. 0 only when `repo` is absent or provably not a Git work tree.

    Any other failure to measure raises: a silent 0 would let the capacity plan report
    `fits: true` for an arbitrarily large history.
    """
    if not repo.exists():
        return 0
    try:
        in_work_tree = _git_root(repo) is not None
    except CorpusStoreError as exc:
        raise SealedError("git-size-unmeasurable") from exc
    if not in_work_tree:
        return 0
    try:
        git = subprocess.run(
            ["git", "-C", str(repo), "count-objects", "-v"],
            check=False,
            capture_output=True,
            text=True,
        )
        if git.returncode == 0:
            counts = dict(line.split(": ", 1) for line in git.stdout.splitlines())
            return (int(counts["size"]) + int(counts["size-pack"])) * 1024
    except (OSError, ValueError, KeyError) as exc:
        raise SealedError("git-size-unmeasurable") from exc
    raise SealedError("git-size-unmeasurable")
