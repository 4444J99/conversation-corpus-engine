"""Bounded history inspection with synthetic, highly compressed historical poison."""

import os
import subprocess

import pytest

from conversation_corpus_engine import archive_sealed as sealed


def _git(repo, *args, data=None):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        input=data,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Synthetic",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Synthetic",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        },
    ).stdout


def _snapshot(repo):
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "chore: update encrypted archive")


@pytest.fixture
def history(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "note").write_bytes(b"synthetic history note")
    owner = sealed.create_legacy_identity(tmp_path / "owner", "synthetic", "owner")
    trust = sealed.create_trust(tmp_path / "trust", owner)
    repo = tmp_path / "cipher"
    sealed.publish(source, ["note"], repo, owner, trust, [owner.recipient()])
    _git(repo, "init", "-b", "main")
    _snapshot(repo)
    return source, repo, owner, trust


@pytest.mark.parametrize(
    "name,error",
    [
        ("objects/ab/" + "ab" + "0" * 62, "history-blob-too-large"),
        ("HEAD.cce", "history-blob-too-large"),
        ("synthetic-private-name", "history-path-refused"),
    ],
)
def test_historical_poison_is_refused_before_body_cat_file(history, monkeypatch, name, error):
    _, repo, owner, trust = history
    path = repo / name
    previous = path.read_bytes() if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    size = sealed.MAX_MANIFEST * 3 + 1 if name == "HEAD.cce" else 8 * 1024 * 1024 + 1
    path.write_bytes(b"0" * size)
    poison = _git(repo, "hash-object", str(path)).decode().strip()
    _snapshot(repo)
    if previous is None:
        path.unlink()
    else:
        path.write_bytes(previous)
    _snapshot(repo)
    _git(repo, "gc", "--prune=now")
    real_popen = subprocess.Popen

    def observe(command, *args, **kwargs):
        assert command[-3:] != ["cat-file", "blob", poison], "auditor requested poisoned blob body"
        return real_popen(command, *args, **kwargs)

    monkeypatch.setattr(sealed.subprocess, "Popen", observe)
    with pytest.raises(sealed.SealedError, match=error):
        sealed.audit_history(repo, owner, trust)


def test_bounded_audit_keeps_ordinary_history_and_rotation(history):
    source, repo, owner, trust = history
    first = sealed.audit_history(repo, owner, trust)
    assert first["commits"] == 1
    (source / "note").write_bytes(b"updated synthetic note")
    sealed.publish(source, ["note"], repo, owner, trust, [owner.recipient()])
    _snapshot(repo)
    sealed.publish(source, ["note"], repo, owner, trust, [owner.recipient()], rotate=True)
    _snapshot(repo)
    result = sealed.audit_history(repo, owner, trust)
    assert result["commits"] == 3
    assert result["blobs"] > first["blobs"]
    assert sealed.verify(repo, owner, trust)["generation"] == 3


def test_history_audit_refuses_aggregate_inspection_over_budget(history, monkeypatch):
    _, repo, owner, trust = history
    # Inspection must enforce a finite aggregate budget even when each Git blob is valid.
    monkeypatch.setattr(sealed, "DEFAULT_BUDGET", 4096)
    with pytest.raises(sealed.SealedError, match="history-audit-limit"):
        sealed.audit_history(repo, owner, trust)


@pytest.mark.parametrize("signed", [False, True])
def test_alias_history_authenticates_before_retaining_path_metadata(history, monkeypatch, signed):
    _, repo, owner, trust = history
    base = _git(repo, "rev-parse", "HEAD").decode().strip()
    clean_tree = _git(repo, "rev-parse", "HEAD^{tree}").decode().strip()
    head_blob = _git(repo, "rev-parse", "HEAD:HEAD.cce").decode().strip()
    if signed:
        original = next((repo / "objects").glob("*/*"))
        poison = _git(repo, "hash-object", "-w", str(original)).decode().strip()
        error = "history-blob-alias-refused"
    else:
        poison = _git(repo, "hash-object", "-w", "--stdin", data=b"x").decode().strip()
        error = "unauthenticated-retained-object"
    aliases = [f"{i:064x}" for i in range(2000)]
    entries = "".join(f"100644 blob {poison}\t{oid}\n" for oid in aliases).encode()
    subtree = _git(repo, "mktree", data=entries).decode().strip()
    objects = _git(repo, "mktree", data=f"040000 tree {subtree}\t00\n".encode()).decode().strip()
    tree = (
        _git(
            repo,
            "mktree",
            data=(f"100644 blob {head_blob}\tHEAD.cce\n040000 tree {objects}\tobjects\n").encode(),
        )
        .decode()
        .strip()
    )
    bad = (
        _git(repo, "commit-tree", tree, "-p", base, "-m", "chore: update encrypted archive")
        .decode()
        .strip()
    )
    clean = (
        _git(repo, "commit-tree", clean_tree, "-p", bad, "-m", "chore: update encrypted archive")
        .decode()
        .strip()
    )
    _git(repo, "update-ref", "refs/heads/main", clean)
    real_match = sealed.re.fullmatch
    alias_paths = {f"objects/00/{oid}" for oid in aliases}
    visited = []

    def check_path(pattern, value, *args, **kwargs):
        if value in alias_paths:
            visited.append(value)
            assert len(visited) == 1, (
                "auditor retained aliases before authenticating the blob address"
            )
        return real_match(pattern, value, *args, **kwargs)

    monkeypatch.setattr(sealed.re, "fullmatch", check_path)
    with pytest.raises(sealed.SealedError, match=error):
        sealed.audit_history(repo, owner, trust)
    assert len(visited) == 1
