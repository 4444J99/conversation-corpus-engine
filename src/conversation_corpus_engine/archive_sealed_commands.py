"""Thin opt-in CLI. Never print source paths, text, private IDs or exception details."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def configure(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="sealed_action", required=True)
    identity = commands.add_parser("identity", help="Create local private credentials")
    identity.add_argument("--labels", type=Path, required=True)
    identity.add_argument("--key", type=Path, required=True)
    identity.add_argument("--recipient", type=Path, required=True)
    init = commands.add_parser("init", help="Pin an owner key and new vault outside Git")
    init.add_argument("--key", type=Path, required=True)
    init.add_argument("--trust", type=Path, required=True)
    for action in (
        "publish",
        "verify",
        "restore",
        "repair",
        "unit",
        "units",
        "status",
        "audit-history",
    ):
        child = commands.add_parser(action)
        child.add_argument("--key", type=Path, required=True)
        child.add_argument("--trust", type=Path, required=True)
        child.add_argument("--repo", type=Path, required=True)
        if action == "publish":
            child.add_argument("--source", type=Path, required=True)
            child.add_argument("--selection", type=Path, required=True)
            child.add_argument("--recipient", type=Path, action="append", required=True)
            child.add_argument("--rotate", action="store_true")
            child.add_argument("--budget", type=int, default=512 * 1024 * 1024)
        if action in {"restore", "unit", "units"}:
            child.add_argument("--out", type=Path, required=True)
        if action == "unit":
            child.add_argument("--id-file", type=Path, required=True)
        if action == "repair":
            child.add_argument("--mirror", type=Path, required=True)


def add_sealed_parser(subparsers: Any) -> None:
    configure(subparsers.add_parser("sealed", help="Encrypted archive publication (opt-in)"))


def run(args: list[str] | argparse.Namespace) -> int:
    if isinstance(args, list):
        parser = argparse.ArgumentParser(prog="cce sealed")
        configure(parser)
        args = parser.parse_args(args)
    try:
        from . import archive_sealed as sealed  # noqa: PLC0415 - optional dependency

        result = _dispatch(args, sealed)
        print(json.dumps(result, sort_keys=True))
        return 0
    except ImportError:
        print("[sealed] install-sealed-extra", file=sys.stderr)
        return 2
    except Exception as exc:
        # Neither provider JSON errors nor OS errors may echo a private path or source fragment.
        from .archive_sealed_crypto import SealedError  # noqa: PLC0415

        code = str(exc) if isinstance(exc, SealedError) else "operation-failed"
        print(f"[sealed] {code}", file=sys.stderr)
        return 2


def _dispatch(args: argparse.Namespace, sealed: Any) -> dict[str, Any]:
    def read(path: Path) -> Any:
        return json.loads(sealed.private_path(path).read_bytes())

    if args.sealed_action == "identity":
        labels = read(args.labels)
        recipient = sealed.private_path(args.recipient)
        identity = sealed.create_identity(args.key, labels["app_id"], labels["instance_id"])
        sealed._write(recipient, sealed.canonical(identity.recipient()), exclusive=True)
        return {"created": True}
    identity = sealed.load_identity(args.key)
    if args.sealed_action == "init":
        sealed.create_trust(args.trust, identity)
        return {"created": True}
    if args.sealed_action == "publish":
        return sealed.publish(
            args.source,
            read(args.selection),
            args.repo,
            identity,
            args.trust,
            [read(path) for path in args.recipient],
            rotate=args.rotate,
            budget=args.budget,
        )
    if args.sealed_action == "verify":
        return sealed.verify(args.repo, identity, args.trust)
    if args.sealed_action == "restore":
        return sealed.restore(args.repo, args.out, identity, args.trust)
    if args.sealed_action == "repair":
        return sealed.repair(args.repo, args.mirror, identity, args.trust)
    if args.sealed_action == "status":
        return sealed.storage_status(args.repo, identity, args.trust)
    if args.sealed_action == "audit-history":
        return sealed.audit_history(args.repo, identity, args.trust)
    out = sealed.private_path(args.out)
    if args.sealed_action == "unit":
        unit_id = sealed.private_path(args.id_file).read_text().strip()
        unit = sealed.read_unit(args.repo, unit_id, identity, args.trust)
        sealed._write(out, sealed.canonical(unit), exclusive=True)
    else:
        units = sealed.list_units(args.repo, identity, args.trust)
        sealed._write(out, sealed.canonical(units), exclusive=True)
    return {"written": True}
