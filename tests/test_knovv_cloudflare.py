"""Cloudflare control operations exercised against a real SQLite journal and fake R2."""

import json
import sqlite3

import pytest
from test_knovv_projection import fixture

from conversation_corpus_engine.archive_sealed_crypto import digest, sign
from conversation_corpus_engine.knovv_cloudflare import Config, seed, snapshot
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
