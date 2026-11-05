"""Format and history-audit bounds, exercised with tiny synthetic inputs (no huge allocations)."""

import json
import os
import subprocess
import sys
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from conversation_corpus_engine import archive_sealed as sealed
from conversation_corpus_engine.archive_sealed_commands import run as sealed_run

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Synthetic",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Synthetic",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def _git(repo, *args):
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={**os.environ, **GIT_ENV},
    )


def _snapshot(repo):
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "chore: update encrypted archive")


@pytest.fixture
def vault(tmp_path):
    source = tmp_path / "private"
    source.mkdir()
    (source / "note").write_bytes(b"synthetic bound note")
    owner = sealed.create_legacy_identity(tmp_path / "owner.key", "bound-app", "owner")
    trust = sealed.create_trust(tmp_path / "trust.json", owner)
    return source, tmp_path / "cipher", owner, trust


def _publish(vault, selection=("note",), recipients=None, **kwargs):
    source, repo, owner, trust = vault
    return sealed.publish(
        source,
        list(selection),
        repo,
        owner,
        trust,
        recipients or [owner.recipient()],
        **kwargs,
    )


@pytest.fixture
def history(vault):
    _, repo, owner, trust = vault
    _publish(vault)
    _git(repo, "init", "-b", "main")
    _snapshot(repo)
    return repo, owner, trust


def _opened(vault):
    _, repo, owner, trust = vault
    head, manifest, _ = sealed._open(repo, owner, sealed._trust(trust))
    return head, manifest


def test_format_constants_are_pinned():
    assert sealed.MAX_UNIT == 4 * 1024 * 1024
    assert sealed.MAX_MANIFEST == 16 * 1024 * 1024
    assert sealed.CHUNK == 512 * 1024
    assert sealed.DEFAULT_BUDGET == 512 * 1024 * 1024


# --- S3: the history-audit aggregate cap follows the budget argument -------------------------


def test_history_audit_cap_follows_budget_argument_without_patching_constants(history):
    repo, owner, trust = history
    with pytest.raises(sealed.SealedError, match="history-audit-limit"):
        sealed.audit_history(repo, owner, trust, budget=4096)
    assert sealed.audit_history(repo, owner, trust, budget=1 << 20)["commits"] == 1
    assert sealed.audit_history(repo, owner, trust)["commits"] == 1


def test_history_audit_rejects_negative_budget(history):
    repo, owner, trust = history
    with pytest.raises(sealed.SealedError, match="invalid-budget"):
        sealed.audit_history(repo, owner, trust, budget=-1)


def test_history_audit_metadata_limit_is_derived_from_budget(vault, monkeypatch):
    source, repo, owner, trust = vault
    monkeypatch.setattr(sealed, "CHUNK", 1024)
    (source / "note").write_bytes(b"".join(bytes([k]) * 1024 for k in range(5)))  # 5 chunks
    repo.mkdir()
    _git(repo, "init", "-b", "main")  # a work tree without commits: no bytes to inspect
    _publish(vault)
    assert len(_opened(vault)[1]["objects"]) == 5
    assert sealed.audit_history(repo, owner, trust, budget=4096 * 5)["commits"] == 0
    with pytest.raises(sealed.SealedError, match="history-audit-limit"):
        sealed.audit_history(repo, owner, trust, budget=4096 * 4)


def test_history_audit_budget_reaches_publish_and_plan(vault, history):
    source, repo, owner, trust = vault
    args = source, ["note"], repo, owner, trust, [owner.recipient()]
    with pytest.raises(sealed.SealedError, match="history-audit-limit"):
        sealed.plan_capacity(*args, audit_budget=4096)
    with pytest.raises(sealed.SealedError, match="history-audit-limit"):
        sealed.publish(*args, audit_budget=4096)
    assert sealed.plan_capacity(*args)["fits"]
    # The storage budget and the audit budget are independent controls.
    assert not sealed.plan_capacity(*args, budget=1)["fits"]


def _cli(vault, action, *extra):
    _, repo, _, trust = vault
    return sealed_run(
        [
            action,
            "--repo",
            str(repo),
            "--key",
            str(trust.parent / "owner.key"),
            "--trust",
            str(trust),
            *extra,
            "--legacy-plaintext-key",
        ]
    )


def test_cli_audit_history_budget_flag(vault, history, capsys):
    assert _cli(vault, "audit-history") == 0
    capsys.readouterr()
    assert _cli(vault, "audit-history", "--budget", "4096") == 2
    assert capsys.readouterr().err == "[sealed] history-audit-limit\n"
    assert _cli(vault, "audit-history", "--budget", "1048576") == 0


def test_cli_plan_audit_budget_flag(vault, history, tmp_path, capsys):
    source, repo, owner, trust = vault
    selection = tmp_path / "selection.json"
    selection.write_text('["note"]')
    recipient = tmp_path / "recipient.json"
    recipient.write_bytes(sealed.canonical(owner.recipient()))
    common = ["--source", str(source), "--selection", str(selection)]
    common += ["--recipient", str(recipient)]
    assert _cli(vault, "plan", *common) == 0
    capsys.readouterr()
    assert _cli(vault, "plan", *common, "--audit-budget", "4096") == 2
    assert capsys.readouterr().err == "[sealed] history-audit-limit\n"


# --- S4: bounds that previously had no test --------------------------------------------------


def test_history_commit_cap_counts_exactly(history, vault, monkeypatch):
    source, repo, owner, trust = vault
    (source / "note").write_bytes(b"second synthetic revision")
    _publish(vault)
    _snapshot(repo)
    _publish(vault, rotate=True)
    _snapshot(repo)
    monkeypatch.setattr(sealed, "HISTORY_MAX_COMMITS", 3)
    assert sealed.audit_history(repo, owner, trust)["commits"] == 3
    monkeypatch.setattr(sealed, "HISTORY_MAX_COMMITS", 2)
    with pytest.raises(sealed.SealedError, match="history-audit-limit"):
        sealed.audit_history(repo, owner, trust)


def test_history_audit_git_subprocess_timeout(history, monkeypatch):
    repo, owner, trust = history
    real_popen = subprocess.Popen

    def hang(command, *args, **kwargs):
        if "rev-list" in command:  # only the audit's bounded helper; leave other git calls alone
            command = [sys.executable, "-c", "import time; time.sleep(30)"]
        return real_popen(command, *args, **kwargs)

    monkeypatch.setattr(sealed, "HISTORY_GIT_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(sealed.subprocess, "Popen", hang)
    started = time.monotonic()
    with pytest.raises(sealed.SealedError, match="history-inspection-failed"):
        sealed.audit_history(repo, owner, trust)
    assert time.monotonic() - started < 10


def test_atomic_unit_bound_is_exact(vault, monkeypatch):
    source, repo, owner, trust = vault
    monkeypatch.setattr(sealed, "MAX_UNIT", 2048)
    monkeypatch.setattr(sealed, "CHUNK", 512)

    def atom_file(name, padding):
        row = {
            "schema": "cce-knowledge-atom.v1",
            "id": name,
            "text": "x" * padding,
            "authorship": {},
            "source": {},
            "timestamps": {},
            "derived_from": [],
        }
        (source / f"{name}.atoms.jsonl").write_text(json.dumps(row) + "\n")
        return len(sealed.canonical(row))

    padding = 2048 - atom_file("exact", 0)
    assert atom_file("exact", padding) == 2048
    assert _publish(vault, ["exact.atoms.jsonl"])["units"] == 1
    assert atom_file("above", padding + 1) == 2049
    with pytest.raises(sealed.SealedError, match="unit-too-large"):
        _publish(vault, ["exact.atoms.jsonl", "above.atoms.jsonl"])


def test_recipient_count_bound_is_exactly_64(vault):
    _, repo, owner, trust = vault

    def stranger(i):
        key = X25519PrivateKey.generate().public_key().public_bytes_raw().hex()
        return {"app_id": "fleet", "instance_id": f"device-{i}", "encryption_public": key}

    too_many = [owner.recipient(), *(stranger(i) for i in range(64))]
    with pytest.raises(sealed.SealedError, match="invalid-selection-or-recipients"):
        _publish(vault, recipients=too_many)
    assert not (repo / "HEAD.cce").exists()
    _publish(vault, recipients=too_many[:64])
    head, manifest = _opened(vault)
    assert len(head["envelopes"]) == len(manifest["recipients"]) == 64


def test_manifest_plaintext_bound_is_exact(vault, tmp_path, monkeypatch):
    source, _, owner, _ = vault
    sizes = []
    real_encrypt = sealed.encrypt

    def spy(secret, data, context):
        if context.endswith(":manifest"):
            sizes.append(len(data))
        return real_encrypt(secret, data, context)

    monkeypatch.setattr(sealed, "encrypt", spy)

    def fresh(name):
        trust = sealed.create_trust(tmp_path / f"{name}-trust", owner)
        return sealed.publish(
            source, ["note"], tmp_path / f"{name}-cipher", owner, trust, [owner.recipient()]
        )

    fresh("measure")
    (size,) = sizes
    monkeypatch.setattr(sealed, "MAX_MANIFEST", size)
    assert fresh("exact")["changed"]
    monkeypatch.setattr(sealed, "MAX_MANIFEST", size - 1)
    with pytest.raises(sealed.SealedError, match="manifest-too-large"):
        fresh("over")
    assert not (tmp_path / "over-cipher" / "HEAD.cce").exists()


def test_signed_head_size_bound_rejects_oversized_manifest_envelope(vault, monkeypatch):
    _, repo, owner, trust = vault
    _publish(vault)
    monkeypatch.setattr(sealed, "MAX_MANIFEST", 1)
    with pytest.raises(sealed.SealedError, match="manifest-too-large"):
        sealed.verify(repo, owner, trust)


@pytest.mark.parametrize(
    "extra,expected_chunks", [(0, 1), (1, 2), (sealed.CHUNK, 2), (sealed.CHUNK + 1, 3)]
)
def test_file_chunk_boundary_and_exact_restore(vault, tmp_path, extra, expected_chunks):
    source, repo, owner, trust = vault
    size = sealed.CHUNK + extra
    data = (bytes(range(256)) * (size // 256 + 1))[:size]
    (source / "big").write_bytes(data)
    _publish(vault, ["big"])
    recipe = _opened(vault)[1]["files"]["big"]
    assert len(recipe["chunks"]) == expected_chunks
    sealed.restore(repo, tmp_path / "restored", owner, trust)
    assert (tmp_path / "restored" / "big").read_bytes() == data


def test_retained_object_size_bound_is_checked_before_reading(vault):
    _, repo, owner, trust = vault
    _publish(vault)
    victim = next((repo / "objects").glob("*/*"))
    victim.write_bytes(b"\0" * (sealed.MAX_UNIT + 8192))
    with pytest.raises(sealed.SealedError, match="corrupt-object"):
        sealed.verify(repo, owner, trust)
    victim.write_bytes(b"\0" * (sealed.MAX_UNIT + 8193))
    with pytest.raises(sealed.SealedError, match="object-too-large"):
        sealed.verify(repo, owner, trust)
