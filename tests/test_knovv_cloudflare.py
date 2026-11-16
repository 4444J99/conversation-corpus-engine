"""Cloudflare control operations exercised against a real SQLite journal and fake R2."""

import json
import sqlite3
import threading
from dataclasses import asdict, replace

import pytest
from test_knovv_projection import fixture

from conversation_corpus_engine.archive_sealed_crypto import authenticate, canonical, digest, sign
from conversation_corpus_engine.knovv_cloudflare import Cloudflare, Config, main, seed, snapshot
from conversation_corpus_engine.knovv_projection import export_bundle


class Transport:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.objects = {}

    def query(self, sql, params=()):
        cursor = self.db.execute(sql, params)
        self.db.commit()
        return [dict(row) for row in cursor.fetchall()]

    def schema(self, sql):
        self.db.executescript(sql)

    def put(self, oid, path):
        self.objects[oid] = path.read_bytes()

    def get(self, oid, path):
        path.write_bytes(self.objects[oid])
        path.chmod(0o600)


class ParallelTransport(Transport):
    def __init__(self, workers=3):
        super().__init__()
        self.db.close()
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.mutex = threading.RLock()
        self.barrier = threading.Barrier(workers)
        self.workers = workers
        self.active = self.peak = self.reads = self.closed = 0
        self.verified = set()
        self.fail = False

    def query(self, sql, params=()):
        with self.mutex:
            if sql.startswith("INSERT OR IGNORE INTO publications"):
                assert self.verified == set(self.objects)
            return super().query(sql, params)

    def schema(self, sql):
        with self.mutex:
            return super().schema(sql)

    def put(self, oid, path):
        with self.mutex:
            return super().put(oid, path)

    def get(self, oid, path):
        with self.mutex:
            self.active += 1
            self.reads += 1
            self.peak = max(self.active, self.peak)
            synchronize = self.reads <= self.workers and not self.fail
        try:
            if self.fail:
                raise TimeoutError("synthetic-readback-interruption")
            if synchronize:
                self.barrier.wait(timeout=5)
            super().get(oid, path)
            with self.mutex:
                self.verified.add(oid)
        finally:
            with self.mutex:
                self.active -= 1

    def fork(self):
        parent = self

        class Child:
            query = parent.query
            put = parent.put
            get = parent.get

            def close(self):
                with parent.mutex:
                    parent.closed += 1

        return Child()


def setup(tmp_path):
    library, wiki, owner = fixture(tmp_path)
    bundle = tmp_path / "bundle"
    export_bundle(library, wiki, bundle, owner, [owner.recipient()])
    library.close()
    grant = sign(
        {
            "schema": "knovv-device.v1",
            "id": digest(owner.signing_public().encode()),
            "signing_public": owner.signing_public(),
            "generation": 1,
            "revoked": False,
        },
        owner,
    )
    config = Config(
        account="a" * 32,
        database="00000000-0000-4000-8000-000000000001",
        bucket="synthetic-knovv",
        owner_public=owner.signing_public(),
        owner_email="owner@example.invalid",
        audience="b" * 64,
        max_cipher_bytes=512 * 1024 * 1024,
    )
    return config, bundle, grant, owner


def test_seed_is_idempotent_and_snapshot_contains_independently_verifiable_ciphertext(tmp_path):
    config, bundle, grant, owner = setup(tmp_path)
    transport = Transport()
    first = seed(config, bundle, [grant], transport)
    second = seed(config, bundle, [grant], transport)
    assert first["publication"] == second["publication"]
    assert len(transport.objects) == first["objects"]
    target = tmp_path / "snapshot"
    snapshot(config, target, transport)
    assert (target / "HEAD.knovv").read_bytes() == (bundle / "HEAD.knovv").read_bytes()
    assert all(
        path.read_bytes() == (bundle / "objects" / path.name).read_bytes()
        for path in (target / "objects").iterdir()
    )
    assert json.loads((target / "journal.json").read_bytes())["grants"] == [grant]


def test_parallel_ciphertext_transfer_is_bounded_and_publishes_after_every_readback(tmp_path):
    config, bundle, grant, _ = setup(tmp_path)
    transport = ParallelTransport()
    result = seed(config, bundle, [grant], transport, workers=3)
    assert transport.peak == 3 and transport.closed == 3
    assert len(transport.verified) == result["objects"]
    transport.reads = transport.peak = 0
    target = tmp_path / "parallel-snapshot"
    receipt = snapshot(config, target, transport, workers=3)
    assert receipt["objects"] == result["objects"]
    assert transport.peak == 3 and transport.closed == 6
    assert (target / "HEAD.knovv").read_bytes() == (bundle / "HEAD.knovv").read_bytes()


def test_interrupted_parallel_upload_never_publishes_and_remains_resumable(tmp_path):
    config, bundle, grant, _ = setup(tmp_path)
    transport = ParallelTransport()
    transport.fail = True
    with pytest.raises(TimeoutError, match="synthetic-readback-interruption"):
        seed(config, bundle, [grant], transport, workers=3)
    assert not transport.query("SELECT digest FROM publications")
    assert transport.active == 0 and transport.closed > 0
    transport.fail = False
    transport.reads = 0
    result = seed(config, bundle, [grant], transport, workers=3)
    assert len(transport.verified) == result["objects"]


@pytest.mark.parametrize("workers", [0, 5, True])
def test_invalid_parallelism_cannot_mutate_remote_storage(tmp_path, workers):
    config, bundle, grant, _ = setup(tmp_path)
    transport = Transport()
    with pytest.raises(ValueError, match="invalid-transfer-workers"):
        seed(config, bundle, [grant], transport, workers=workers)
    assert not transport.objects
    assert not transport.query("SELECT name FROM sqlite_master WHERE type='table'")


def test_seed_refuses_unrelated_database_and_corrupt_objects_before_remote_writes(tmp_path):
    config, bundle, grant, _ = setup(tmp_path)
    transport = Transport()
    transport.query("CREATE TABLE unrelated (private TEXT)")
    with pytest.raises(ValueError, match="unowned-database"):
        seed(config, bundle, [grant], transport)
    assert not transport.objects
    transport = Transport()
    path = next((bundle / "objects").iterdir())
    path.write_bytes(b"bad")
    with pytest.raises(ValueError, match="projection-object-digest"):
        seed(config, bundle, [grant], transport)
    assert not transport.objects
    assert not transport.query("SELECT name FROM sqlite_master WHERE type='table'")


def test_stale_revocation_cannot_be_reported_as_applied(tmp_path):
    config, bundle, grant, owner = setup(tmp_path)
    transport = Transport()
    seed(config, bundle, [grant], transport)
    active = sign(
        {key: value for key, value in {**grant, "generation": 3}.items() if key != "signature"},
        owner,
    )
    seed(config, bundle, [active], transport)
    revoked = sign(
        {
            key: value
            for key, value in {**grant, "generation": 2, "revoked": True}.items()
            if key != "signature"
        },
        owner,
    )
    with pytest.raises(ValueError, match="device-grant-conflict"):
        seed(config, bundle, [revoked], transport)


def test_snapshot_pages_a_stable_cutoff_without_a_lifetime_limit(tmp_path):
    config, bundle, grant, _ = setup(tmp_path)
    transport = Transport()
    seed(config, bundle, [grant], transport)
    for n in range(5):
        transport.query(
            "INSERT INTO revisions(id,doc,parent,object,device,envelope) VALUES (?,?,?,?,?,?)",
            [f"{n:064x}", "d" * 64, None, "f" * 64, grant["id"], json.dumps({"id": f"{n:064x}"})],
        )
    target = tmp_path / "paged"
    receipt = snapshot(config, target, transport, page_entries=2)
    manifest = json.loads((target / "journal.json").read_bytes())
    assert manifest["schema"] == "knovv-journal.v2"
    assert manifest["cutoff"] == 5 and receipt["revisions"] == 5
    assert [page["entries"] for page in manifest["pages"]] == [2, 2, 1]
    sequences = []
    for page in manifest["pages"]:
        raw = (target / page["file"]).read_bytes()
        assert digest(raw) == page["sha256"] and len(raw) == page["bytes"]
        sequences.extend(json.loads(line)["seq"] for line in raw.splitlines())
    assert sequences == [1, 2, 3, 4, 5]


def test_interrupted_snapshot_resumes_verified_ciphertext_under_its_original_cutoff(tmp_path):
    config, bundle, grant, owner = setup(tmp_path)

    class Interrupted(Transport):
        interrupt = False
        calls = []

        def get(self, oid, path):
            self.calls.append(oid)
            if self.interrupt and len(self.calls) == 2:
                path.write_bytes(b"partial ciphertext")
                raise TimeoutError("interrupted")
            return super().get(oid, path)

    transport = Interrupted()
    seed(config, bundle, [grant], transport)
    transport.calls = []
    transport.interrupt = True
    target = tmp_path / "resume"
    with pytest.raises(TimeoutError):
        snapshot(config, target, transport, page_entries=2)
    first = transport.calls[0]
    assert not (target / "HEAD.knovv").exists()
    transport.interrupt = False
    head = json.loads((bundle / "HEAD.knovv").read_bytes())
    updated = sign(
        {
            key: value
            for key, value in {**head, "generation": 2, "parent": digest(canonical(head))}.items()
            if key != "signature"
        },
        owner,
    )
    transport.query("UPDATE publications SET envelope=? WHERE id=1", [canonical(updated).decode()])
    snapshot(config, target, transport, page_entries=2)
    assert transport.calls.count(first) == 1
    assert (target / "HEAD.knovv").read_bytes() == (bundle / "HEAD.knovv").read_bytes()
    assert any((target / "partials").iterdir())


def test_durable_control_transport_signs_only_allowlisted_operations(tmp_path, monkeypatch):

    config, _, _, owner = setup(tmp_path)
    config = replace(
        config,
        database="",
        journal_origin="https://knovv.example.test",
        publisher_client_id="synthetic-client",
    )
    credentials = tmp_path / "access.json"
    credentials.write_text(
        json.dumps({"client_id": "synthetic-client", "client_secret": "synthetic-secret"})
    )
    credentials.chmod(0o600)
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, n):
            return b'{"success":true,"results":[{"owner":"verified"}]}'

    def open_request(request, **kwargs):
        requests.append(request)
        assert request.full_url == config.journal_origin + "/api/control"
        assert request.get_header("Cf-access-client-id") == "synthetic-client"
        value = json.loads(request.data)
        authenticate(value, config.owner_public)
        assert value["operation"] == "owner" and value["params"] == []
        assert value["origin"] == config.journal_origin
        return Response()

    monkeypatch.setattr(
        "urllib.request.build_opener",
        lambda *_args: type("Opener", (), {"open": staticmethod(open_request)})(),
    )
    transport = Cloudflare(config, owner=owner, access_credentials=credentials)
    assert transport.query("SELECT owner FROM knovv_identity WHERE id=1") == [{"owner": "verified"}]
    with pytest.raises(ValueError, match="unsupported-control-query"):
        transport.query("DELETE FROM devices")
    assert len(requests) == 1
    credentials.chmod(0o644)
    with pytest.raises(ValueError, match="credential-permissions"):
        Cloudflare(config, owner=owner, access_credentials=credentials)
    with pytest.raises(ValueError, match="owner-control-credentials-required"):
        Cloudflare(config)


def test_cloudflare_cli_runner_reuses_one_process_and_rejects_other_commands(tmp_path):
    config, _, _, _ = setup(tmp_path)
    with Cloudflare(config, seconds=90) as transport:
        assert b"1.0.0-beta" in transport._run("--version")
        process = transport._process
        assert b"1.0.0-beta" in transport._run("--version")
        assert transport._process is process
        with pytest.raises(ValueError, match="cloudflare-command-failed"):
            transport._run("r2", "buckets", "delete", "unrelated")
    assert process.poll() is not None


def test_environment_output_needs_only_public_configuration(tmp_path, capsys):
    config, _, _, _ = setup(tmp_path)
    config = replace(
        config,
        database="",
        journal_origin="https://knovv.example.test",
        access_issuer="https://team.cloudflareaccess.com",
    )
    path = tmp_path / "config.json"
    path.write_text(json.dumps(asdict(config)))
    main(["environment", "--config", str(path)])
    assert json.loads(capsys.readouterr().out)["KNOVV_D1_ID"] == ""
