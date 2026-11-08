"""Scoped Cloudflare activation and encrypted readback using existing cf authentication.

No credential is retrieved here. The control process handles public trust material
and ciphertext only. Database ownership, capacity and digests are checked before
publishing a head; interrupted uploads can be resumed without replacing originals.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .archive_sealed import safe_path
from .archive_sealed_crypto import authenticate, canonical, digest
from .knovv_projection import DEFAULT_BUDGET, MAX_BUDGET, _object

HEX = re.compile(r"[0-9a-f]{64}\Z")
SCHEMA = Path(__file__).resolve().parents[2] / "apps/knovv/server/schema.sql"


@dataclass(frozen=True)
class Config:
    account: str
    database: str
    bucket: str
    owner_public: str
    owner_email: str
    audience: str
    max_cipher_bytes: int = DEFAULT_BUDGET

    def __post_init__(self):
        if (
            not re.fullmatch(r"[0-9a-f]{32}", self.account)
            or not re.fullmatch(r"[0-9a-f-]{36}", self.database)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", self.bucket)
            or not HEX.fullmatch(self.owner_public)
            or not HEX.fullmatch(self.audience)
            or not re.fullmatch(r"[^\s@]+@[^\s@]+", self.owner_email)
            or type(self.max_cipher_bytes) is not int
            or not 1 <= self.max_cipher_bytes <= MAX_BUDGET
        ):
            raise ValueError("invalid-deployment-configuration")

    def environment(self):
        return {
            "CLOUDFLARE_ACCOUNT_ID": self.account,
            "KNOVV_D1_ID": self.database,
            "KNOVV_R2_BUCKET": self.bucket,
            "KNOVV_OWNER_PUBLIC_KEY": self.owner_public,
            "KNOVV_OWNER_EMAIL": self.owner_email,
            "KNOVV_ACCESS_AUD": self.audience,
            "KNOVV_MAX_CIPHER_BYTES": str(self.max_cipher_bytes),
        }


class Cloudflare:
    def __init__(self, config: Config, *, seconds=3600):
        self.config = config
        self.deadline = time.monotonic() + seconds

    def _run(self, *args: str, output=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("cloudflare-operation-deadline")
        result = subprocess.run(
            ["cf", *args],
            env={**os.environ, **self.config.environment()},
            stdout=output if output is not None else subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=min(60, remaining),
            check=False,
        )
        if result.returncode:
            # Provider errors may contain request payloads. Keep them out of logs.
            raise ValueError("cloudflare-command-failed:" + args[0] + ":" + args[1])
        if output is None:
            if len(result.stdout) > 64 * 1024 * 1024:
                raise ValueError("cloudflare-response-bound")
            return result.stdout
        return b""

    def query(self, sql: str, params=()):
        value = json.loads(
            self._run(
                "d1",
                "query",
                self.config.database,
                "--body",
                canonical({"sql": sql, "params": list(params)}).decode(),
            )
        )
        if isinstance(value, dict):
            value = value.get("result", value)
        if isinstance(value, list):
            value = value[0] if len(value) == 1 else None
        if (
            not isinstance(value, dict)
            or value.get("success") is False
            or not isinstance(value.get("results"), list)
        ):
            raise ValueError("invalid-database-response")
        return value["results"]

    def schema(self, sql: str):
        self._run("d1", "query", self.config.database, "--sql", sql)

    def put(self, oid: str, path: Path):
        self._run(
            "r2", "objects", "put", oid, "--bucket-name", self.config.bucket, "--file", str(path)
        )

    def get(self, oid: str, path: Path):
        safe_path(path)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            self._run(
                "r2", "objects", "get", oid, "--bucket-name", self.config.bucket, output=stream
            )
            stream.flush()
            os.fsync(stream.fileno())


def seed(config: Config, bundle: Path, grants: list[dict], transport, *, schema: Path = SCHEMA):
    bundle = safe_path(bundle)
    raw = (bundle / "HEAD.knovv").read_bytes()
    if len(raw) > 256 * 1024:
        raise ValueError("publication-bound")
    head = json.loads(raw)
    authenticate(head, config.owner_public)
    if (
        canonical(head) != raw
        or head.get("schema") != "knovv-publication.v1"
        or not HEX.fullmatch(head.get("vault", ""))
    ):
        raise ValueError("invalid-publication")
    seen = set()
    for grant in grants:
        authenticate(grant, config.owner_public)
        if (
            grant.get("schema") != "knovv-device.v1"
            or grant["id"] != digest(grant["signing_public"].encode())
            or grant["id"] in seen
        ):
            raise ValueError("invalid-device-grant")
        seen.add(grant["id"])
    objects = []
    size = 0
    for path in sorted((bundle / "objects").iterdir()):
        cipher = _object(bundle, path.name)
        size += len(cipher)
        objects.append((path.name, len(cipher), path))
    if size > config.max_cipher_bytes or head.get("manifest") not in {oid for oid, _, _ in objects}:
        raise ValueError("publication-capacity-or-manifest")
    # The first remote mutation occurs only after the whole bundle is hash checked.
    tables = {
        r["name"]
        for r in transport.query("SELECT name FROM sqlite_master WHERE type='table'")
        if not r["name"].startswith(("sqlite_", "_cf_"))
    }
    if tables and "knovv_identity" not in tables:
        raise ValueError("unowned-database")
    if tables:
        identity = transport.query("SELECT owner FROM knovv_identity WHERE id=1")
        if identity != [{"owner": config.owner_public}]:
            raise ValueError("wrong-database-owner")
    else:
        transport.schema(schema.read_text())
        transport.query("INSERT INTO knovv_identity(id,owner) VALUES (1,?)", [config.owner_public])
    previous = transport.query("SELECT digest,generation,vault,epoch FROM publications WHERE id=1")
    checksum = digest(raw)
    if previous and previous[0]["digest"] != checksum:
        old = previous[0]
        if (
            head.get("parent") != old["digest"]
            or head.get("generation") != old["generation"] + 1
            or head["vault"] != old["vault"]
            or head.get("epoch") not in (old["epoch"], old["epoch"] + 1)
        ):
            raise ValueError("publication-chain-conflict")
    elif not previous and (head.get("generation") != 1 or head.get("parent") is not None):
        raise ValueError("initial-publication-required")
    with tempfile.TemporaryDirectory(prefix="knovv-readback-", dir=bundle.parent) as temporary:
        Path(temporary).chmod(0o700)
        for oid, length, path in objects:
            transport.query(
                "INSERT OR IGNORE INTO objects(id,size) SELECT ?,? WHERE (SELECT bytes FROM usage WHERE id=1)+?<=?",
                [oid, length, length, config.max_cipher_bytes],
            )
            reserved = transport.query("SELECT size,uploaded FROM objects WHERE id=?", [oid])
            if not reserved or reserved[0]["size"] != length:
                raise ValueError("storage-budget-or-object-conflict")
            if not reserved[0]["uploaded"]:
                transport.put(oid, path)
            check = Path(temporary) / oid
            transport.get(oid, check)
            if check.stat().st_size != length or digest(check.read_bytes()) != oid:
                raise ValueError("remote-ciphertext-readback-mismatch")
            check.unlink()
            transport.query("UPDATE objects SET uploaded=1 WHERE id=?", [oid])
    for grant in grants:
        transport.query(
            "INSERT INTO devices(id,signing_public,generation,revoked,envelope) VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET generation=excluded.generation,revoked=excluded.revoked,envelope=excluded.envelope WHERE excluded.generation>devices.generation AND excluded.signing_public=devices.signing_public",
            [
                grant["id"],
                grant["signing_public"],
                grant["generation"],
                int(grant["revoked"]),
                canonical(grant).decode(),
            ],
        )
    # CAS publishes only the checked parent, and cannot reset another writer's head.
    if not previous:
        transport.query(
            "INSERT OR IGNORE INTO publications(id,generation,epoch,vault,digest,envelope) VALUES (1,?,?,?,?,?)",
            [head["generation"], head["epoch"], head["vault"], checksum, raw.decode()],
        )
    else:
        transport.query(
            "UPDATE publications SET generation=?,epoch=?,digest=?,envelope=? WHERE id=1 AND digest=? AND vault=?",
            [
                head["generation"],
                head["epoch"],
                checksum,
                raw.decode(),
                previous[0]["digest"],
                head["vault"],
            ],
        )
    if transport.query("SELECT digest FROM publications WHERE id=1") != [{"digest": checksum}]:
        raise ValueError("publication-concurrent-change")
    return {"publication": checksum, "objects": len(objects), "cipher_bytes": size}


def snapshot(config: Config, destination: Path, transport):
    # One SQL statement establishes a consistent journal/object observation.
    rows = transport.query(
        "SELECT 'publication' kind,envelope value,0 seq FROM publications UNION ALL SELECT 'device',envelope,0 FROM devices UNION ALL SELECT 'revision',envelope,seq FROM revisions UNION ALL SELECT 'object',json_object('id',id,'size',size),0 FROM objects WHERE uploaded=1 LIMIT 100001"
    )
    if len(rows) > 100000:
        raise ValueError("snapshot-row-bound")
    heads = [json.loads(r["value"]) for r in rows if r["kind"] == "publication"]
    if len(heads) != 1:
        raise ValueError("snapshot-publication-unavailable")
    head = heads[0]
    authenticate(head, config.owner_public)
    objects = [json.loads(r["value"]) for r in rows if r["kind"] == "object"]
    if (
        any(
            not HEX.fullmatch(x.get("id", ""))
            or type(x.get("size")) is not int
            or not 0 <= x["size"] <= 48 * 1024 * 1024
            for x in objects
        )
        or sum(x["size"] for x in objects) > config.max_cipher_bytes
    ):
        raise ValueError("snapshot-storage-bound")
    destination = safe_path(destination)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    (destination / "objects").mkdir(mode=0o700)
    for item in objects:
        path = destination / "objects" / item["id"]
        transport.get(item["id"], path)
        if path.stat().st_size != item["size"] or digest(path.read_bytes()) != item["id"]:
            raise ValueError("snapshot-ciphertext-mismatch")
    journal = {
        "schema": "knovv-journal.v1",
        "vault": head["vault"],
        "epoch": head["epoch"],
        "grants": [json.loads(r["value"]) for r in rows if r["kind"] == "device"],
        "revisions": sorted(
            [
                {"seq": r["seq"], "envelope": json.loads(r["value"])}
                for r in rows
                if r["kind"] == "revision"
            ],
            key=lambda r: r["seq"],
        ),
    }
    from .knovv_cli import _write  # noqa: PLC0415

    _write(destination / "journal.json", journal)
    _write(destination / "HEAD.knovv", head)
    receipt = {
        "schema": "knovv-ciphertext-snapshot.v1",
        "publication": digest(canonical(head)),
        "objects": len(objects),
        "revisions": len(journal["revisions"]),
        "verified_ciphertext": True,
        "content_restore": "requires-enrolled-device",
    }
    _write(destination / "receipt.json", receipt)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["seed", "snapshot", "environment"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--grant", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--schema", type=Path, default=SCHEMA)
    args = parser.parse_args(argv)
    config = Config(**json.loads(safe_path(args.config).read_bytes()))
    transport = Cloudflare(config)
    if args.action == "environment":
        print(json.dumps(config.environment(), sort_keys=True))
    elif args.action == "seed":
        if not args.bundle or not args.grant:
            parser.error("seed requires --bundle and --grant")
        print(
            json.dumps(
                seed(
                    config,
                    args.bundle,
                    [json.loads(safe_path(p).read_bytes()) for p in args.grant],
                    transport,
                    schema=args.schema,
                ),
                sort_keys=True,
            )
        )
    else:
        if not args.output:
            parser.error("snapshot requires --output")
        print(json.dumps(snapshot(config, args.output, transport), sort_keys=True))


if __name__ == "__main__":
    main()
