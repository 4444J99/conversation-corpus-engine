"""Scoped Cloudflare activation and encrypted readback using existing cf authentication.

No credential is retrieved here. The control process handles public trust material
and ciphertext only. Database ownership, capacity and digests are checked before
publishing a head; interrupted uploads can be resumed without replacing originals.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import selectors
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, local

from .archive_sealed import load_identity, safe_path
from .archive_sealed_crypto import authenticate, canonical, digest, sign
from .knovv_cli import _private
from .knovv_projection import DEFAULT_BUDGET, MAX_BUDGET, _object

HEX = re.compile(r"[0-9a-f]{64}\Z")
SCHEMA = Path(__file__).resolve().parents[2] / "apps/knovv/server/schema.sql"


class _ObjectPool:
    """At most four independent CLI clients; no unbounded future queue or new deadline."""

    def __init__(self, transport, workers):
        if type(workers) is not int or not 1 <= workers <= 4:
            raise ValueError("invalid-transfer-workers")
        if workers > 1 and not callable(getattr(transport, "fork", None)):
            raise ValueError("transport-cannot-fork")
        self.transport, self.workers = transport, workers
        self.children = []
        self.mutex = Lock()
        self.local = local()
        self.executor = None

    def __enter__(self):
        if self.workers > 1:
            self.executor = ThreadPoolExecutor(max_workers=self.workers)
        return self

    def __exit__(self, *_args):
        if self.executor:
            self.executor.shutdown(wait=True, cancel_futures=True)
        with ExitStack() as cleanup:
            for child in self.children:
                cleanup.callback(child.close)

    def run(self, action, items):
        if self.workers == 1:
            for item in items:
                action(self.transport, item)
            return
        iterator, stopped = iter(items), Event()

        def consume():
            while not stopped.is_set():
                with self.mutex:
                    try:
                        item = next(iterator)
                    except StopIteration:
                        return
                try:
                    if not hasattr(self.local, "transport"):
                        child = self.transport.fork()
                        with self.mutex:
                            self.children.append(child)
                        self.local.transport = child
                    action(self.local.transport, item)
                except BaseException:
                    stopped.set()
                    raise

        futures = [self.executor.submit(consume) for _ in range(self.workers)]
        for future in futures:
            future.result()


@dataclass(frozen=True)
class Config:
    account: str
    database: str
    bucket: str
    owner_public: str
    owner_email: str
    audience: str
    max_cipher_bytes: int = DEFAULT_BUDGET
    journal_origin: str = ""
    publisher_client_id: str = ""
    access_issuer: str = ""

    def __post_init__(self):
        if (
            not re.fullmatch(r"[0-9a-f]{32}", self.account)
            or (
                not re.fullmatch(r"[0-9a-f-]{36}", self.database)
                if not self.journal_origin
                else bool(self.database)
            )
            or (
                self.journal_origin
                and not re.fullmatch(r"https://[a-z0-9.-]+(?::[0-9]+)?", self.journal_origin)
            )
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
            "KNOVV_PUBLISHER_CLIENT_ID": self.publisher_client_id,
            "KNOVV_ACCESS_ISSUER": self.access_issuer,
            "KNOVV_R2_BUCKET": self.bucket,
            "KNOVV_OWNER_PUBLIC_KEY": self.owner_public,
            "KNOVV_OWNER_EMAIL": self.owner_email,
            "KNOVV_ACCESS_AUD": self.audience,
            "KNOVV_MAX_CIPHER_BYTES": str(self.max_cipher_bytes),
        }


class Cloudflare:
    def __init__(self, config: Config, *, seconds=3600, owner=None, access_credentials=None):
        self.config = config
        self.deadline = time.monotonic() + seconds
        self._process = None
        self._owner = owner
        self._access = None
        self._access_credentials = access_credentials
        if config.journal_origin:
            if (
                owner is None
                or owner.signing_public() != config.owner_public
                or access_credentials is None
            ):
                raise ValueError("owner-control-credentials-required")
            self._access = json.loads(_private(access_credentials))
            if (
                set(self._access) != {"client_id", "client_secret"}
                or self._access["client_id"] != config.publisher_client_id
                or not all(
                    isinstance(v, str) and v and "\n" not in v and "\r" not in v
                    for v in self._access.values()
                )
            ):
                raise ValueError("invalid-access-credentials")

    def fork(self):
        child = Cloudflare(
            self.config,
            seconds=max(0, self.deadline - time.monotonic()),
            owner=self._owner,
            access_credentials=self._access_credentials,
        )
        child.deadline = self.deadline
        return child

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        if self._process is not None:
            if self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=5)
            for stream in (self._process.stdin, self._process.stdout):
                stream.close()
            self._process = None

    def _run(self, *args: str, output=None):
        remaining = min(60, self.deadline - time.monotonic())
        if remaining <= 0:
            raise ValueError("cloudflare-operation-deadline")
        if self._process is None:
            runner = Path(__file__).resolve().parents[2] / "apps/knovv/agent/cf-runner.mjs"
            self._process = subprocess.Popen(
                ["node", str(runner)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={**os.environ, **self.config.environment()},
            )
        request = {"args": list(args)}
        if output is not None:
            if not isinstance(output, Path):
                output.flush()
            request["output"] = str(output if isinstance(output, Path) else output.name)
        process = self._process
        try:
            process.stdin.write(canonical(request) + b"\n")
            process.stdin.flush()
            raw = bytearray()
            until = time.monotonic() + remaining
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while not raw.endswith(b"\n"):
                    timeout = until - time.monotonic()
                    if timeout <= 0 or not selector.select(timeout):
                        raise ValueError("cloudflare-operation-deadline")
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk or len(raw) + len(chunk) > 90 * 1024 * 1024:
                        raise ValueError("cloudflare-response-bound")
                    raw.extend(chunk)
            response = json.loads(raw)
            if response.get("ok") is not True:
                raise ValueError("cloudflare-command-failed:" + args[0])
            return base64.b64decode(response["stdout"], validate=True) if output is None else b""
        except Exception:
            self.close()
            raise

    def _control(self, operation, params):
        remaining = min(60, self.deadline - time.monotonic())
        if remaining <= 0:
            raise ValueError("cloudflare-operation-deadline")
        value = sign(
            {
                "schema": "knovv-control.v1",
                "operation": operation,
                "params": list(params),
                "origin": self.config.journal_origin,
                "issued": int(time.time()),
                "nonce": os.urandom(32).hex(),
            },
            self._owner,
        )
        request = urllib.request.Request(
            self.config.journal_origin + "/api/control",
            data=canonical(value),
            headers={
                "content-type": "application/json",
                "user-agent": "KNOVV-Publisher/0.1",
                "origin": self.config.journal_origin,
                "CF-Access-Client-Id": self._access["client_id"],
                "CF-Access-Client-Secret": self._access["client_secret"],
            },
        )
        try:
            # Redirects must never forward this app-scoped credential elsewhere.
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *_args):
                    raise ValueError("control-redirect-refused")

            response = urllib.request.build_opener(NoRedirect()).open(request, timeout=remaining)
            with response:
                raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError("control-response-bound")
            result = json.loads(raw)
            if result.get("success") is not True or not isinstance(result.get("results"), list):
                raise ValueError("invalid-control-response")
            return result["results"]
        except urllib.error.HTTPError as error:
            raise ValueError(f"cloudflare-control-http-{error.code}") from None
        except Exception:
            raise ValueError("cloudflare-control-failed") from None

    def query(self, sql: str, params=()):
        if self.config.journal_origin:
            queries = json.loads((SCHEMA.parent / "control-queries.json").read_bytes())
            operation = next((name for name, query in queries.items() if query == sql), None)
            if operation is None:
                raise ValueError("unsupported-control-query")
            return self._control(operation, params)
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
        if self.config.journal_origin:
            if sql != SCHEMA.read_text():
                raise ValueError("unsupported-control-schema")
            self._control("initialize", [])
            return
        self._run("d1", "query", self.config.database, "--sql", sql)

    def put(self, oid: str, path: Path):
        self._run(
            "r2", "objects", "put", oid, "--bucket-name", self.config.bucket, "--file", str(path)
        )

    def get(self, oid: str, path: Path):
        safe_path(path)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            self._run("r2", "objects", "get", oid, "--bucket-name", self.config.bucket, output=path)
            stream.flush()
            os.fsync(stream.fileno())


def seed(
    config: Config, bundle: Path, grants: list[dict], transport, *, schema: Path = SCHEMA, workers=1
):
    pool = _ObjectPool(transport, workers)
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

        def upload(client, item):
            oid, length, path = item
            client.query(
                "INSERT OR IGNORE INTO objects(id,size) SELECT ?,? WHERE (SELECT bytes FROM usage WHERE id=1)+?<=?",
                [oid, length, length, config.max_cipher_bytes],
            )
            reserved = client.query("SELECT size,uploaded FROM objects WHERE id=?", [oid])
            if not reserved or reserved[0]["size"] != length:
                raise ValueError("storage-budget-or-object-conflict")
            if not reserved[0]["uploaded"]:
                client.put(oid, path)
            check = Path(temporary) / oid
            client.get(oid, check)
            if check.stat().st_size != length or digest(check.read_bytes()) != oid:
                raise ValueError("remote-ciphertext-readback-mismatch")
            check.unlink()
            client.query("UPDATE objects SET uploaded=1 WHERE id=?", [oid])

        with pool:
            pool.run(upload, objects)
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
        stored = transport.query("SELECT envelope FROM devices WHERE id=?", [grant["id"]])
        if len(stored) != 1 or json.loads(stored[0]["envelope"]) != grant:
            raise ValueError("device-grant-conflict")
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


def snapshot(config: Config, destination: Path, transport, *, page_entries: int = 256, workers=1):
    pool = _ObjectPool(transport, workers)
    if not 1 <= page_entries <= 1000:
        raise ValueError("invalid-page-bound")
    from .knovv_journal import _atomic  # noqa: PLC0415

    destination = safe_path(destination)
    marker = destination / "snapshot-plan.json"
    configuration = digest(canonical(config.environment()))

    def store(path, raw):
        safe_path(path)
        if path.exists():
            if path.stat().st_size != len(raw) or path.read_bytes() != raw:
                raise ValueError("snapshot-local-conflict")
        else:
            _atomic(path, raw)

    if destination.exists():
        if not marker.is_file() or marker.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("unowned-snapshot-directory")
        if (destination / "HEAD.knovv").exists():
            raise ValueError("snapshot-already-complete-use-new-destination")
        plan = json.loads(marker.read_bytes())
        if plan.get("schema") != "knovv-snapshot-plan.v1" or plan.get("config") != configuration:
            raise ValueError("wrong-snapshot-owner")
        head, cutoff, grants = plan["head"], plan["cutoff"], plan["grants"]
        page_entries = plan["page_entries"]
        if (
            type(cutoff) is not int
            or cutoff < 0
            or type(page_entries) is not int
            or not 1 <= page_entries <= 1000
        ):
            raise ValueError("invalid-snapshot-plan")
    else:
        # One statement fixes publication, grants and high-water mark for every retry.
        rows = transport.query(
            "SELECT 'publication' kind,envelope value FROM publications UNION ALL SELECT 'device',envelope FROM devices UNION ALL SELECT 'cutoff',CAST(COALESCE(MAX(seq),0) AS TEXT) FROM revisions"
        )
        heads = [json.loads(r["value"]) for r in rows if r["kind"] == "publication"]
        if len(heads) != 1:
            raise ValueError("snapshot-publication-unavailable")
        head = heads[0]
        cutoff = int(next(r["value"] for r in rows if r["kind"] == "cutoff"))
        grants = [json.loads(r["value"]) for r in rows if r["kind"] == "device"]
        plan = {
            "schema": "knovv-snapshot-plan.v1",
            "config": configuration,
            "head": head,
            "cutoff": cutoff,
            "grants": grants,
            "page_entries": page_entries,
        }
    authenticate(head, config.owner_public)
    for grant in grants:
        authenticate(grant, config.owner_public)
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    store(marker, canonical(plan))
    for name in ("objects", "journal-pages", "partials"):
        safe_path(destination / name).mkdir(mode=0o700, exist_ok=True)
    size = count = 0
    after = ""

    def download(client, item):
        path = destination / "objects" / item["id"]
        safe_path(path)
        complete = (
            path.is_file()
            and path.stat().st_size == item["size"]
            and digest(path.read_bytes()) == item["id"]
        )
        if not complete:
            if path.exists():
                path.rename(destination / "partials" / (item["id"] + "-" + uuid.uuid4().hex))
            client.get(item["id"], path)
            if path.stat().st_size != item["size"] or digest(path.read_bytes()) != item["id"]:
                raise ValueError("snapshot-ciphertext-mismatch")

    with pool:
        while True:
            objects = transport.query(
                "SELECT id,size FROM objects WHERE uploaded=1 AND id>? ORDER BY id LIMIT ?",
                [after, page_entries],
            )
            if not objects:
                break
            for item in objects:
                if (
                    not HEX.fullmatch(item.get("id", ""))
                    or item["id"] <= after
                    or type(item.get("size")) is not int
                    or not 0 <= item["size"] <= 48 * 1024 * 1024
                ):
                    raise ValueError("snapshot-storage-bound")
                size += item["size"]
                if size > config.max_cipher_bytes:
                    raise ValueError("snapshot-storage-bound")
                after = item["id"]
            pool.run(download, objects)
            count += len(objects)
    pages = []
    cursor = revisions = 0
    while cursor < cutoff:
        entries = transport.query(
            "SELECT seq,envelope FROM revisions WHERE seq>? AND seq<=? ORDER BY seq LIMIT ?",
            [cursor, cutoff, page_entries],
        )
        if not entries:
            raise ValueError("snapshot-journal-gap")
        values = []
        for entry in entries:
            if type(entry["seq"]) is not int or not cursor < entry["seq"] <= cutoff:
                raise ValueError("snapshot-journal-order")
            values.append(
                canonical({"seq": entry["seq"], "envelope": json.loads(entry["envelope"])}) + b"\n"
            )
            cursor = entry["seq"]
        raw = b"".join(values)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("snapshot-page-bound")
        name = f"journal-pages/{len(pages):08}.jsonl"
        path = destination / name
        store(path, raw)
        pages.append(
            {
                "file": name,
                "sha256": digest(raw),
                "bytes": len(raw),
                "entries": len(entries),
                "first": entries[0]["seq"],
                "last": cursor,
            }
        )
        revisions += len(entries)
    journal = {
        "schema": "knovv-journal.v2",
        "vault": head["vault"],
        "epoch": head["epoch"],
        "grants": grants,
        "cutoff": cutoff,
        "pages": pages,
    }
    store(destination / "journal.json", canonical(journal))
    receipt = {
        "schema": "knovv-ciphertext-snapshot.v2",
        "publication": digest(canonical(head)),
        "objects": count,
        "cipher_bytes": size,
        "revisions": revisions,
        "cutoff": cutoff,
        "verified_ciphertext": True,
        "content_restore": "requires-enrolled-device",
    }
    store(destination / "receipt.json", canonical(receipt))
    # HEAD is the completion marker and appears only after every readback succeeds.
    store(destination / "HEAD.knovv", canonical(head))
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["seed", "snapshot", "environment"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--grant", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--schema", type=Path, default=SCHEMA)
    parser.add_argument("--owner-key", type=Path)
    parser.add_argument("--owner-passphrase-file", type=Path)
    parser.add_argument("--access-credentials", type=Path)
    parser.add_argument("--workers", type=int, default=1, choices=range(1, 5))
    args = parser.parse_args(argv)
    config = Config(**json.loads(safe_path(args.config).read_bytes()))
    if args.action == "environment":
        print(json.dumps(config.environment(), sort_keys=True))
        return None
    owner = None
    if config.journal_origin:
        if not args.owner_key or not args.owner_passphrase_file or not args.access_credentials:
            parser.error(
                "journal transport requires owner key, passphrase file and Access credentials"
            )
        owner = load_identity(
            args.owner_key,
            passphrase=_private(args.owner_passphrase_file),  # allow-secret: protected file input
        )
    transport = Cloudflare(config, owner=owner, access_credentials=args.access_credentials)
    try:
        return _main_action(args, config, transport)
    finally:
        transport.close()


def _main_action(args, config, transport):
    if args.action == "environment":
        print(json.dumps(config.environment(), sort_keys=True))
    elif args.action == "seed":
        if not args.bundle or not args.grant:
            raise ValueError("seed requires --bundle and --grant")
        print(
            json.dumps(
                seed(
                    config,
                    args.bundle,
                    [json.loads(safe_path(p).read_bytes()) for p in args.grant],
                    transport,
                    schema=args.schema,
                    workers=args.workers,
                ),
                sort_keys=True,
            )
        )
    else:
        if not args.output:
            raise ValueError("snapshot requires --output")
        print(
            json.dumps(
                snapshot(config, args.output, transport, workers=args.workers), sort_keys=True
            )
        )


if __name__ == "__main__":
    main()
