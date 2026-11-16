"""Versioned compressed packing uses disposable synthetic sources and identities only."""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from conversation_corpus_engine import archive_sealed as sealed
from conversation_corpus_engine import archive_sealed_packing as packed
from conversation_corpus_engine.archive_sealed_canary import scan
from conversation_corpus_engine.archive_sealed_commands import run as sealed_run
from conversation_corpus_engine.archive_sealed_crypto import Identity
from conversation_corpus_engine.archive_sealed_units import project_units

FORMAT = "cce-sealed.v2"
MARKER = b"SYNTHETIC-PACKING-CANARY-195d3ce"


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Synthetic",
            "GIT_AUTHOR_EMAIL": "synthetic@example.invalid",
            "GIT_COMMITTER_NAME": "Synthetic",
            "GIT_COMMITTER_EMAIL": "synthetic@example.invalid",
        },
    )


def snapshot(repo: Path) -> None:
    git(repo, "add", ".")
    git(repo, "commit", "-m", "chore: update encrypted archive")


@pytest.fixture
def vault(tmp_path):
    source = tmp_path / "private"
    source.mkdir()
    (source / "note").write_bytes(MARKER + b"\r\n\x00" + b"synthetic repeated text " * 3000)
    (source / "binary").write_bytes(random.Random(71).randbytes(9000))
    (source / "empty").write_bytes(b"")
    atom = {
        "schema": "cce-knowledge-atom.v1",
        "id": "synthetic-atom",
        "text": MARKER.decode(),
        "authorship": {"role": "user", "evidence": "fixture"},
        "source": {"fixture": True},
        "timestamps": {},
        "derived_from": [],
    }
    (source / "note.atoms.jsonl").write_text(json.dumps(atom) + "\n")
    owner = sealed.create_legacy_identity(tmp_path / "synthetic.key", "synthetic-app", "test")
    trust = sealed.create_trust(tmp_path / "trust.json", owner)
    return source, tmp_path / "cipher", owner, trust


def args(vault):
    source, repo, owner, trust = vault
    return (
        source,
        ["note", "binary", "empty", "note.atoms.jsonl"],
        repo,
        owner,
        trust,
        [owner.recipient()],
    )


def publish(vault, **kwargs):
    return sealed.publish(*args(vault), storage_format="v2", **kwargs)


def opened(vault):
    _, repo, owner, trust = vault
    return sealed._open(repo, owner, sealed._trust(trust))


def tree_bytes(repo):
    return {p.relative_to(repo).as_posix(): p.read_bytes() for p in repo.rglob("*") if p.is_file()}


def assert_restored(vault, destination):
    source, repo, owner, trust = vault
    sealed.restore(repo, destination, owner, trust)
    assert tree_bytes(destination) == tree_bytes(source)


def test_v2_compresses_exactly_restores_and_keeps_atoms(vault, tmp_path):
    _, repo, owner, trust = vault
    report = publish(vault)
    head, manifest, _ = opened(vault)
    assert head["format"] == manifest["format"] == FORMAT
    assert report["storage_format"] == FORMAT
    assert report["shards"] > 0
    assert report["packed_compressed_bytes"] < report["packed_plaintext_bytes"]
    assert set(manifest) == {"format", "files", "atoms", "objects", "shards", "recipients"}
    assert all(p.stat().st_size <= 48 * 1024**2 for p in (repo / "shards").glob("*/*"))
    assert sealed.verify(repo, owner, trust)["files"] == 4
    assert sealed.read_unit(repo, "synthetic-atom", owner, trust)["text"] == MARKER.decode()
    assert_restored(vault, tmp_path / "restore")
    assert scan(repo, [MARKER, b"synthetic-app", b"note.atoms.jsonl"])["passed"]


def test_v1_remains_default_and_readable(vault, tmp_path):
    _, repo, owner, trust = vault
    sealed.publish(*args(vault))
    assert opened(vault)[0]["format"] == "cce-sealed.v1"
    assert (repo / "objects").is_dir()
    assert not (repo / "shards").exists()
    assert sealed.verify(repo, owner, trust)["files"] == 4
    assert_restored(vault, tmp_path / "restore-v1")


def test_v2_repeat_is_noop_and_default_preserves_v2(vault):
    publish(vault)
    before = tree_bytes(vault[1])
    report = sealed.publish(*args(vault))
    assert report["changed"] is False
    assert report["storage_format"] == FORMAT
    assert tree_bytes(vault[1]) == before
    assert publish(vault)["changed"] is False


def test_v2_reuses_retained_ciphertext_after_a_b_a(vault):
    source, repo, _, _ = vault
    publish(vault)
    initial = tree_bytes(repo)
    initial_shards = {k: v for k, v in initial.items() if k.startswith("shards/")}
    note = (source / "note").read_bytes()
    (source / "note").write_bytes(note + b"changed")
    publish(vault)
    (source / "note").write_bytes(note)
    publish(vault)
    for name, content in initial_shards.items():
        assert (repo / name).read_bytes() == content
    _, manifest, _ = opened(vault)
    assert {p.rsplit("/", 1)[1] for p in initial_shards} == set(manifest["shards"])


def test_v1_history_survives_v2_upgrade_and_audit(vault, tmp_path):
    _, repo, owner, trust = vault
    repo.mkdir()
    git(repo, "init", "-b", "main")
    sealed.publish(*args(vault))
    snapshot(repo)
    old_objects = {p: p.read_bytes() for p in (repo / "objects").glob("*/*")}
    publish(vault)
    snapshot(repo)
    assert all(path.read_bytes() == content for path, content in old_objects.items())
    assert sealed.audit_history(repo, owner, trust)["commits"] == 2
    assert_restored(vault, tmp_path / "upgraded-restore")
    assert scan(repo, [MARKER])["passed"]


def test_v2_explicit_downgrade_is_refused_without_writes(vault):
    publish(vault)
    before = tree_bytes(vault[1])
    with pytest.raises(sealed.SealedError, match="format-downgrade-refused"):
        sealed.publish(*args(vault), storage_format="v1")
    assert tree_bytes(vault[1]) == before


@pytest.mark.parametrize("label", ["wrong-key", "wrong-app", "wrong-instance"])
def test_v2_key_and_label_authorization(vault, tmp_path, label):
    _, repo, owner, trust = vault
    publish(vault)
    if label == "wrong-key":
        other = sealed.create_legacy_identity(
            tmp_path / "other.key", owner.app_id, owner.instance_id
        )
    else:
        other = Identity(
            "other" if label == "wrong-app" else owner.app_id,
            "other" if label == "wrong-instance" else owner.instance_id,
            owner.encryption_key,
            owner.signing_key,
        )
    with pytest.raises(sealed.SealedError, match="not-authorized"):
        sealed.verify(repo, other, trust)


def test_v2_capacity_measures_actual_ciphertext_without_writes(vault):
    _, repo, _, trust = vault
    before_trust = trust.read_bytes()
    plan = sealed.plan_capacity(*args(vault), storage_format="v2")
    assert not repo.exists()
    assert trust.read_bytes() == before_trust
    report = publish(vault)
    assert plan["pending_ciphertext_bytes"] == sum(
        p.stat().st_size for p in (repo / "shards").glob("*/*")
    )
    assert plan["planned_tree_bytes"] == report["live_bytes"]
    assert plan["maximum_shard_bytes"] == max(
        p.stat().st_size for p in (repo / "shards").glob("*/*")
    )
    assert plan["shards"] == report["shards"]


def test_v2_tamper_missing_and_repair_are_atomic(vault, tmp_path):
    _, repo, owner, trust = vault
    publish(vault)
    mirror = tmp_path / "mirror"
    shutil.copytree(repo, mirror)
    shard = next((repo / "shards").glob("*/*"))
    original = shard.read_bytes()
    shard.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    destination = tmp_path / "not-restored"
    with pytest.raises(sealed.SealedError):
        sealed.restore(repo, destination, owner, trust)
    assert not destination.exists()
    assert sealed.repair(repo, mirror, owner, trust)["repaired"] == 1
    assert shard.read_bytes() == original
    shard.unlink()
    assert sealed.repair(repo, mirror, owner, trust)["repaired"] == 1
    assert_restored(vault, tmp_path / "repaired-restore")


def test_final_signed_padded_size_boundary_is_enforced(vault, monkeypatch):
    plan = sealed.plan_capacity(*args(vault), storage_format="v2")
    exact = plan["maximum_shard_bytes"]
    monkeypatch.setattr(packed, "SHARD_LIMIT", exact)
    publish(vault)
    before = tree_bytes(vault[1])
    monkeypatch.setattr(packed, "SHARD_LIMIT", exact - 1)
    with pytest.raises(sealed.SealedError, match="packed-shard-too-large"):
        publish(vault)
    assert tree_bytes(vault[1]) == before


def test_many_shards_stage_ciphertext_privately_and_cleanup_before_head(
    vault, tmp_path, monkeypatch
):
    source, repo, _, _ = vault
    monkeypatch.setattr(sealed, "CHUNK", 256)
    monkeypatch.setattr(packed, "PLAIN_LIMIT", 1024)
    (source / "note").write_bytes(random.Random(32).randbytes(8000))
    real_write = sealed._write
    stages = []

    def fail_second_shard(path, data, **kwargs):
        if path.parent.name == "stage":
            stages.append(path)
        if path.parent.parent.name == "shards" and path.parent.parent.parent == repo:
            raise OSError("synthetic publication interruption")
        return real_write(path, data, **kwargs)

    monkeypatch.setattr(sealed, "_write", fail_second_shard)
    with pytest.raises(OSError, match="synthetic publication interruption"):
        publish(vault)
    assert len(stages) > 1
    assert all(not path.exists() for path in stages)
    assert not (repo / "HEAD.cce").exists()
    assert not list((repo / "shards").glob("*/*"))


@pytest.mark.parametrize("failure_point", ["before-head", "after-head"])
def test_v2_interruption_preserves_old_or_new_complete_generation(
    vault, tmp_path, monkeypatch, failure_point
):
    source, repo, owner, trust = vault
    publish(vault)
    old_head = (repo / "HEAD.cce").read_bytes()
    old_files = {p: p.read_bytes() for p in (repo / "shards").glob("*/*")}
    (source / "note").write_bytes(MARKER + b"new generation")
    real_write = sealed._write

    def fail_head(path, data, **kwargs):
        if path == repo / "HEAD.cce":
            if failure_point == "after-head":
                real_write(path, data, **kwargs)
            raise OSError("synthetic HEAD interruption")
        return real_write(path, data, **kwargs)

    monkeypatch.setattr(sealed, "_write", fail_head)
    with pytest.raises(OSError, match="synthetic HEAD interruption"):
        publish(vault)
    monkeypatch.setattr(sealed, "_write", real_write)
    assert all(path.read_bytes() == content for path, content in old_files.items())
    if failure_point == "before-head":
        assert (repo / "HEAD.cce").read_bytes() == old_head
        assert {p for p in (repo / "shards").glob("*/*")} == set(old_files)
    else:
        assert (repo / "HEAD.cce").read_bytes() != old_head
        assert_restored(vault, tmp_path / "after-head-restore")
    assert sealed.verify(repo, owner, trust)["files"] == 4


def rewrite_manifest(vault, mutate):
    """An authorized synthetic writer builds malformed data to exercise reader bounds."""
    _, repo, owner, trust_path = vault
    head, manifest, secret = opened(vault)
    mutate(repo, manifest, secret, owner, head["vault"])
    context = f"{FORMAT}:{head['vault']}:{head['generation']}"
    head["manifest"] = sealed.encrypt(
        secret, sealed.canonical(manifest), context + ":manifest"
    ).hex()
    head = sealed.sign({k: v for k, v in head.items() if k != "signature"}, owner)
    (repo / "HEAD.cce").write_bytes(sealed.canonical(head))
    trust = json.loads(trust_path.read_bytes())
    trust.update(generation=0, head="")
    trust_path.write_bytes(sealed.canonical(trust))


@pytest.mark.parametrize("attack", ["truncated", "trailing", "expanded", "wrong-length"])
def test_hostile_compression_is_rejected_with_bounded_decode(vault, attack):
    _, repo, owner, trust = vault
    publish(vault)

    def mutate(repo, manifest, secret, owner, vault_id):
        sid = next(iter(manifest["shards"]))
        path = repo / "shards" / sid[:2] / sid
        context = FORMAT + ":shard:" + sid
        compressed = sealed.decrypt(secret, path.read_bytes()[64:], context)
        if attack == "truncated":
            compressed = compressed[:-1]
        elif attack == "trailing":
            compressed += b"hidden trailing data"
        elif attack == "expanded":
            compressed = zlib.compress(b"x" * (packed.PLAIN_LIMIT + 1))
        else:
            manifest["shards"][sid]["plain_bytes"] += 1
            final = max(
                (info for info in manifest["objects"].values() if info["shard"] == sid),
                key=lambda info: info["offset"],
            )
            final["length"] += 1  # structurally coherent metadata, wrong decoded size
        cipher = sealed.sign_object(
            sealed.encrypt(secret, compressed, context), sid, vault_id, owner
        )
        path.write_bytes(cipher)
        manifest["shards"][sid].update(
            cipher_sha256=sealed.digest(cipher), compressed_bytes=len(compressed)
        )

    rewrite_manifest(vault, mutate)
    with pytest.raises(sealed.SealedError, match="packed-shard-invalid|invalid-packed-manifest"):
        sealed.verify(repo, owner, trust)


def test_unknown_storage_format_and_manifest_disagreement_fail_closed(vault):
    with pytest.raises(sealed.SealedError, match="unsupported-storage-format"):
        sealed.publish(*args(vault), storage_format="v99")
    publish(vault)
    rewrite_manifest(vault, lambda _r, manifest, *_rest: manifest.update(format="cce-sealed.v1"))
    with pytest.raises(sealed.SealedError, match="manifest-format-mismatch"):
        sealed.verify(vault[1], vault[2], vault[3])


def test_newly_landed_shard_is_tracked_before_a_post_write_failure(vault, monkeypatch):
    _, repo, _, _ = vault
    real_write = sealed._write

    def interrupted(path, data, **kwargs):
        real_write(path, data, **kwargs)
        if path.parent.parent == repo / "shards":
            raise OSError("synthetic failure after shard publication")

    monkeypatch.setattr(sealed, "_write", interrupted)
    with pytest.raises(OSError, match="after shard publication"):
        publish(vault)
    assert not (repo / "HEAD.cce").exists()
    assert not list((repo / "shards").glob("*/*"))


def test_tiny_atoms_are_verified_by_shard_instead_of_repeatedly_decoding(vault, monkeypatch):
    source, repo, owner, trust = vault
    monkeypatch.setattr(packed, "PLAIN_LIMIT", 2048)
    monkeypatch.setattr(sealed, "CHUNK", 256)
    template = json.loads((source / "note.atoms.jsonl").read_text())
    (source / "note.atoms.jsonl").write_text(
        "".join(json.dumps({**template, "id": f"synthetic-{i}"}) + "\n" for i in range(120))
    )
    publish(vault)
    _, manifest, _ = opened(vault)
    assert len(manifest["objects"]) > len(manifest["shards"]) * 3
    inflations = []
    real_inflate = packed._inflate

    def counted(compressed, expected):
        inflations.append(expected)
        return real_inflate(compressed, expected)

    monkeypatch.setattr(packed, "_inflate", counted)
    sealed.verify(repo, owner, trust)
    assert len(inflations) <= 2 * len(manifest["shards"]) + 1


def test_plan_uses_no_staging_directory(vault, monkeypatch):
    def no_staging(*args, **kwargs):
        raise AssertionError("read-only plan must not stage ciphertext")

    monkeypatch.setattr(packed.tempfile, "TemporaryDirectory", no_staging)
    assert sealed.plan_capacity(*args(vault), storage_format="v2")["fits"]
    assert not vault[1].exists()


def test_packed_slices_are_bound_to_their_logical_object_hmac(vault):
    publish(vault)

    def mutate(_repo, manifest, *_rest):
        a, b, *_ = manifest["objects"]
        manifest["objects"][a], manifest["objects"][b] = (
            manifest["objects"][b],
            manifest["objects"][a],
        )

    rewrite_manifest(vault, mutate)
    with pytest.raises(sealed.SealedError, match="invalid-address"):
        sealed.verify(vault[1], vault[2], vault[3])


@pytest.mark.parametrize(
    "kind,field,value",
    [
        ("objects", "offset", -1),
        ("objects", "length", True),
        ("objects", "length", 0),
        ("shards", "codec", "unknown"),
        ("shards", "plain_bytes", 0),
        ("shards", "plain_bytes", packed.PLAIN_LIMIT + 1),
        ("shards", "compressed_bytes", True),
    ],
)
def test_hostile_packed_manifest_fields_are_rejected_before_decode(
    vault, monkeypatch, kind, field, value
):
    publish(vault)

    def mutate(_repo, manifest, *_rest):
        next(iter(manifest[kind].values()))[field] = value

    rewrite_manifest(vault, mutate)

    def no_decode(*args, **kwargs):
        raise AssertionError("invalid mapping must fail before decompression")

    monkeypatch.setattr(packed, "_inflate", no_decode)
    with pytest.raises(sealed.SealedError, match="invalid-packed-manifest"):
        sealed.verify(vault[1], vault[2], vault[3])


def test_v2_changed_and_rotated_history_keeps_prior_ciphertext(vault):
    source, repo, owner, trust = vault
    repo.mkdir()
    git(repo, "init", "-b", "main")
    publish(vault)
    snapshot(repo)
    old = {path: path.read_bytes() for path in (repo / "shards").glob("*/*")}
    (source / "note").write_bytes(MARKER + b"a changed generation")
    publish(vault)
    snapshot(repo)
    publish(vault, rotate=True)
    snapshot(repo)
    assert all(path.read_bytes() == data for path, data in old.items())
    assert sealed.audit_history(repo, owner, trust)["commits"] == 3
    assert sealed.verify(repo, owner, trust)["generation"] == 3


def test_cli_exposes_v2_read_only_measurement(vault, tmp_path, capsys):
    source, repo, owner, trust = vault
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps(args(vault)[1]))
    recipient = tmp_path / "recipient.json"
    recipient.write_bytes(sealed.canonical(owner.recipient()))
    code = sealed_run(
        [
            "plan",
            "--source",
            str(source),
            "--selection",
            str(selection),
            "--repo",
            str(repo),
            "--key",
            str(tmp_path / "synthetic.key"),
            "--trust",
            str(trust),
            "--recipient",
            str(recipient),
            "--legacy-plaintext-key",
            "--storage-format",
            "v2",
        ]
    )
    output = capsys.readouterr()
    assert code == 0, output.err
    assert json.loads(output.out)["storage_format"] == FORMAT
    assert MARKER.decode() not in output.out + output.err
    assert str(source) not in output.out + output.err
    assert not repo.exists()


def test_v2_oversized_projection_line_preserves_prior_v1_generation(vault, monkeypatch):
    sealed.publish(*args(vault))
    before = tree_bytes(vault[1])
    monkeypatch.setattr(packed, "PROJECTION_LINE_LIMIT", 128)
    with pytest.raises(sealed.SealedError, match="projection-line-too-large"):
        publish(vault)
    assert tree_bytes(vault[1]) == before


def test_projection_line_read_is_bounded_before_json_parsing(tmp_path):
    path = tmp_path / "large.atoms.jsonl"
    path.write_bytes(b'{"schema":"cce-knowledge-atom.v1","text":"' + b"x" * 10000)
    with pytest.raises(sealed.SealedError, match="projection-line-too-large"):
        list(project_units(path, path.name, max_line_bytes=128))


def test_fifo_mirror_shard_is_refused_without_blocking_or_partial_repair(vault, tmp_path):
    _, repo, _, trust = vault
    publish(vault)
    mirror = tmp_path / "mirror"
    shutil.copytree(repo, mirror)
    shard = next((repo / "shards").glob("*/*"))
    other = mirror / shard.relative_to(repo)
    other.unlink()
    os.mkfifo(other)
    shard.unlink()
    before = tree_bytes(repo)
    checkpoint = trust.read_bytes()
    code = """
import sys
from pathlib import Path
from conversation_corpus_engine import archive_sealed as sealed
owner = sealed.load_legacy_identity(Path(sys.argv[3]))
try:
    sealed.repair(Path(sys.argv[1]), Path(sys.argv[2]), owner, Path(sys.argv[4]))
except sealed.SealedError as error:
    print(str(error))
    raise SystemExit(2)
raise SystemExit(0)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(repo),
            str(mirror),
            str(tmp_path / "synthetic.key"),
            str(trust),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    )
    assert result.returncode == 2
    assert result.stdout.strip() == "packed-shard-not-regular"
    assert not result.stderr
    assert tree_bytes(repo) == before
    assert trust.read_bytes() == checkpoint


def test_source_row_can_exceed_a_slab_while_each_projected_unit_fits(vault, monkeypatch):
    source, repo, owner, trust = vault
    monkeypatch.setattr(packed, "PLAIN_LIMIT", 2048)
    monkeypatch.setattr(sealed, "CHUNK", 256)
    row = {
        "schema": "archive-conversation.v2",
        "uid": "synthetic-row",
        "provider": "fixture",
        "messages": [
            {"id": f"m{i}", "role": "user", "text": "Synthetic paragraph " * 4} for i in range(40)
        ],
    }
    raw = (json.dumps(row) + "\n").encode()
    name = "collection-synthetic-conversations.jsonl"
    (source / name).write_bytes(raw)
    assert len(raw) > packed.PLAIN_LIMIT
    assert packed.PROJECTION_LINE_LIMIT == 64 * 1024**2
    arguments = source, [name], repo, owner, trust, [owner.recipient()]
    result = sealed.publish(
        *arguments,
        storage_format="v2",
        max_projection_line_bytes=len(raw),
    )
    assert result["units"] == 40
    assert result["projection_line_limit"] == len(raw)
    assert sealed.verify(repo, owner, trust)["units"] == 40
    before = tree_bytes(repo)
    with pytest.raises(sealed.SealedError, match="projection-line-too-large"):
        sealed.publish(*arguments, storage_format="v2", max_projection_line_bytes=len(raw) - 1)
    assert tree_bytes(repo) == before


def test_projection_override_is_not_silently_ignored_by_v1(vault):
    with pytest.raises(sealed.SealedError, match="projection-bound-requires-v2"):
        sealed.publish(*args(vault), max_projection_line_bytes=1024)
    assert not (vault[1] / "HEAD.cce").exists()
