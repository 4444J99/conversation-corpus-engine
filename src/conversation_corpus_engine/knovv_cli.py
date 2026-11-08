"""Explicit device custody and CCE bridge operations for the KNOVV application."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .archive_sealed import create_identity, load_identity, safe_path
from .archive_sealed_crypto import authenticate, canonical, digest, sign
from .knovv_journal import import_snapshot
from .knovv_projection import DEFAULT_BUDGET, _object, export_bundle, read_bundle, restore_bundle


def _private(path: Path, limit: int = 1024 * 1024) -> bytes:
    safe_path(path)
    if not path.is_file() or path.stat().st_mode & 0o077 or path.stat().st_size > limit:
        raise ValueError("credential-permissions-or-bound")
    return path.read_bytes()


def _write(path: Path, value: dict) -> None:
    safe_path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())


def _device(parser):
    parser.add_argument("--device-key", type=Path, required=True)
    parser.add_argument("--device-passphrase-file", type=Path, required=True)


def _publication(parser):
    _device(parser)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--owner-public", required=True)


def main(argv=None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "agent":
        entry = Path(__file__).resolve().parents[2] / "apps/knovv/build/agent.mjs"
        if not entry.is_file():
            raise ValueError("build-agent-client-first-with-npm-run-build")
        raise SystemExit(
            subprocess.run(
                ["node", str(entry), *arguments[1:]], timeout=180, check=False
            ).returncode
        )
    parser = argparse.ArgumentParser(
        description="KNOVV protected-device and canonical-source bridge; agent --help for enrolled agent writes"
    )
    sub = parser.add_subparsers(dest="action", required=True)
    grant_parser = sub.add_parser("grant", help="Sign a monotonic enrollment or revocation update")
    grant_parser.add_argument("--owner-key", type=Path, required=True)
    grant_parser.add_argument("--owner-passphrase-file", type=Path, required=True)
    grant_parser.add_argument("--previous-grant", type=Path, required=True)
    grant_parser.add_argument("--state", choices=["active", "revoked"], required=True)
    grant_parser.add_argument("--output", type=Path, required=True)
    enroll = sub.add_parser(
        "enroll", help="Create a protected device credential and owner-signed enrollment grant"
    )
    enroll.add_argument("--owner-key", type=Path, required=True)
    enroll.add_argument("--owner-passphrase-file", type=Path, required=True)
    _device(enroll)
    enroll.add_argument("--app-id", default="knovv")
    enroll.add_argument("--instance-id", required=True)
    enroll.add_argument("--output", type=Path, required=True)
    package = sub.add_parser(
        "package", help="Bind a protected device credential to an independently trusted publication"
    )
    _publication(package)
    package.add_argument("--output", type=Path, required=True)
    export = sub.add_parser(
        "export", help="Create an encrypted projection without modifying canonical inputs"
    )
    export.add_argument("--store-root", type=Path, required=True)
    export.add_argument("--wiki-root", type=Path, required=True)
    export.add_argument("--owner-key", type=Path, required=True)
    export.add_argument("--owner-passphrase-file", type=Path, required=True)
    export.add_argument("--recipient", type=Path, action="append", required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--previous", type=Path)
    export.add_argument(
        "--rotate",
        action="store_true",
        help="Rotate the publication key after explicitly changing enrolled recipients",
    )
    export.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    export.add_argument("--deadline-seconds", type=float, default=3600)
    export.add_argument("--attribution-seconds", type=float, default=900)
    restore = sub.add_parser(
        "restore",
        help="Verify a publication and restore source-bound documents to a new private directory",
    )
    _publication(restore)
    restore.add_argument("--checkpoint", type=Path, required=True)
    restore.add_argument("--output", type=Path, required=True)
    ingest = sub.add_parser(
        "import-journal", help="Validate encrypted browser edits and create source-owned CCE inputs"
    )
    _publication(ingest)
    ingest.add_argument("--journal", type=Path, required=True)
    ingest.add_argument("--objects", type=Path, required=True)
    ingest.add_argument("--authoring-home", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.action == "grant":
        owner = load_identity(
            args.owner_key,
            passphrase=_private(args.owner_passphrase_file),  # allow-secret: runtime-only
        )  # allow-secret: runtime-only
        previous = json.loads(_private(args.previous_grant))
        authenticate(previous, owner.signing_public())
        if (
            previous.get("schema") != "knovv-device.v1"
            or previous.get("id") != digest(previous.get("signing_public", "").encode())
            or type(previous.get("generation")) is not int
        ):
            raise ValueError("invalid-device-grant")
        unsigned = {key: value for key, value in previous.items() if key != "signature"}
        value = sign(
            {
                **unsigned,
                "generation": previous["generation"] + 1,
                "revoked": args.state == "revoked",
            },
            owner,
        )
        _write(args.output, value)
        result = {
            "device": value["id"],
            "generation": value["generation"],
            "state": args.state,
            "grant": str(args.output),
        }
    elif args.action == "enroll":
        owner = load_identity(
            args.owner_key,
            passphrase=_private(args.owner_passphrase_file),  # allow-secret: runtime-only
        )  # allow-secret: runtime-only
        if args.output.exists():
            raise ValueError("enrollment-destination-exists")
        device = create_identity(
            args.device_key,
            args.app_id,
            args.instance_id,
            passphrase=_private(args.device_passphrase_file),  # allow-secret: runtime-only
        )
        args.output.mkdir(mode=0o700, parents=True)
        grant = sign(
            {
                "schema": "knovv-device.v1",
                "id": digest(device.signing_public().encode()),
                "signing_public": device.signing_public(),
                "generation": 1,
                "revoked": False,
            },
            owner,
        )
        _write(args.output / "grant.json", grant)
        _write(args.output / "recipient.json", device.recipient())
        result = {"enrollment": str(args.output), "device": grant["id"]}
    elif args.action == "export":
        from .library_commands import open_library  # noqa: PLC0415

        owner = load_identity(
            args.owner_key,
            passphrase=_private(args.owner_passphrase_file),  # allow-secret: runtime-only
        )  # allow-secret: runtime-only
        recipients = [json.loads(_private(path)) for path in args.recipient]
        _, library = open_library(args.store_root)
        try:
            result = export_bundle(
                library,
                args.wiki_root,
                args.output,
                owner,
                recipients,
                budget=args.budget,
                previous=args.previous,
                rotate=args.rotate,
                deadline_seconds=args.deadline_seconds,
                attribution_seconds=args.attribution_seconds,
            )
        finally:
            library.close()
    else:
        device = load_identity(
            args.device_key,
            passphrase=_private(args.device_passphrase_file),  # allow-secret: runtime-only
        )  # allow-secret: runtime-only
        checkpoint = None
        if args.action == "restore":
            value = json.loads(_private(args.checkpoint))
            checkpoint = value.get("trust", value)
            if checkpoint.get("owner") != args.owner_public:
                raise ValueError("wrong-checkpoint-owner")
        head, manifest, secret = read_bundle(  # allow-secret: runtime-only
            args.bundle, device, args.owner_public, checkpoint=checkpoint
        )
        if args.action == "package":
            # Successful decryption proves this exact key, not its labels, is enrolled.
            value = {
                "schema": "knovv-device-package.v1",
                "credential": json.loads(_private(args.device_key)),
                "trust": {
                    "owner": args.owner_public,
                    "vault": head["vault"],
                    "generation": head["generation"],
                    "head": digest(canonical(head)),
                },
            }
            _write(args.output, value)
            result = {"device_package": str(args.output)}
        elif args.action == "restore":
            result = restore_bundle(
                args.bundle, args.output, device, args.owner_public, checkpoint=checkpoint
            )
        else:
            journal = json.loads(_private(args.journal, 64 * 1024 * 1024))
            if journal.get("vault") != head["vault"] or journal.get("epoch") != head["epoch"]:
                raise ValueError("journal-publication-mismatch")
            result = import_snapshot(
                journal,
                args.journal.parent,
                lambda oid: (
                    _object(args.objects.parent, oid)
                    if args.objects.name == "objects"
                    else _object(args.objects, oid)
                ),
                secret,
                args.owner_public,
                args.authoring_home,
                epoch_keys=manifest.get("epoch_keys", {}),
            )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
