"""Synthetic, read-only project snapshot imports; no operator data or credentials."""

import hashlib
import json

import pytest

from conversation_corpus_engine import knovv_project_drafts as adapter
from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.archive_sealed_crypto import address
from conversation_corpus_engine.knovv_project_drafts import prepare_project_drafts
from conversation_corpus_engine.knovv_projection import PAGE_CHARS, _revision_sources
from conversation_corpus_engine.library_store import Library

ID_KEY = b"p" * 32
RAW_HASH = "a" * 64


@pytest.fixture
def snapshot(tmp_path):
    library_path = tmp_path / "library.sqlite"
    library = Library.create(library_path)
    item = Conversation(
        provider="chatgpt",
        source_id="synthetic",
        title="Source discussion",
        messages=[
            Message(role="user", text="A short request.", ts="2026-01-26T18:06:57Z"),
            Message(role="assistant", text="Long answer. " * 1000, ts="2026-01-26T13:07:02-05:00"),
        ],
        provenance={"source_file": "synthetic.json", "source_sha256": RAW_HASH},
    )
    revision = library.put(item)
    library.close()
    vault = tmp_path / "vault"
    projects = vault / "projects"
    projects.mkdir(parents=True)
    target = vault / "conversations" / "conversation-chatgpt-synthetic.md"
    target.parent.mkdir()
    target.write_text(
        f'---\nuid: "chatgpt-synthetic"\nsource_sha256: "{RAW_HASH}"\n---\n'
        "# Source discussion\n\n### User · 2026-01-26 18:06\n\nA short request.\n"
        "\n### Assistant · 2026-01-26 18:07\n\nLong answer.\n"
    )
    project = projects / "Example.md"
    project.write_text(
        "# Example project\n\nPartial history; adoption remains unknown.\n"
        "[[conversations/conversation-chatgpt-synthetic#User · 2026-01-26 18:06|Request]]\n"
        "[[conversations/conversation-chatgpt-synthetic#Assistant · 2026-01-26 18:07|Proposal]]\n"
        "[[conversations/conversation-chatgpt-synthetic#User · 2026-01-26 18:06|Again]]\n"
        "[Repository evidence](https://example.org/tree/revision)\n\n"
        "The cited conversation's CCE identity is\n"
        "`chatgpt-synthetic`, revision `1.0.0`, normalized\n"
        f"digest `{revision['digest']}`.\n"
    )
    return library_path, vault, projects, target, project, item, revision


def prepare(snapshot):
    library_path, vault, projects, *_ = snapshot
    with Library(library_path) as library:
        return prepare_project_drafts(library, vault, projects, ID_KEY)


def test_drafts_bind_exact_source_pages_and_preserve_inputs_and_uncertainty(snapshot):
    library_path, vault, _, target, project, _, revision = snapshot
    before = {p: p.read_bytes() for p in (library_path, target, project)}
    [draft] = prepare(snapshot)
    assert draft["kind"] == "project"
    assert draft["status"] == "draft"
    assert draft["title"] == "Example project"
    assert draft["logical_id"] == "project-snapshot:projects/Example.md"
    assert "author" not in draft  # The submitting agent declares its own authorship.
    assert "Partial history; adoption remains unknown." in draft["body"]
    assert "[Repository evidence](https://example.org/tree/revision)" in draft["body"]
    assert "[[" not in draft["body"]
    with Library(library_path) as library:
        source_pages = list(
            _revision_sources(
                next(library.rows()),
                library.record("chatgpt-synthetic", version="1.0.0"),
                "1.0.0",
                [],
                ID_KEY,
            )
        )
    by_id = {page["id"]: page for page in source_pages}
    assert len(draft["sources"]) == len(draft["links"]) == 2
    for index, (text, end) in enumerate(
        [
            ("A short request.", 16),
            ("Long answer. " * 1000, PAGE_CHARS),
        ]
    ):
        identity = address(ID_KEY, f"cce:chatgpt-synthetic@1.0.0#main:{index}:0".encode())
        reference = {
            "uid": "chatgpt-synthetic",
            "version": "1.0.0",
            "sequence": "main",
            "message": index,
            "digest": hashlib.sha256(text.encode()).hexdigest(),
            "start": 0,
            "end": end,
        }
        assert reference in draft["sources"]
        assert identity in draft["links"]
        assert by_id[identity]["sources"] == [reference]
        assert f"](#/document/{identity})" in draft["body"]
    provenance = draft["provenance"]
    assert json.loads(json.dumps(provenance)) == provenance
    assert provenance["source_file"] == project.relative_to(vault).as_posix()
    assert provenance["source_sha256"] == hashlib.sha256(before[project]).hexdigest()
    assert provenance["source_authorship"] == {"author": "unknown", "basis": "unverified"}
    assert provenance["source_modified_at"].endswith("Z")
    assert provenance["prepared_at"].endswith("Z")
    assert provenance["revisions"][0]["normalized_digest"] == revision["digest"]
    assert provenance["source_sha256"] in draft["body"]
    assert "Source authorship: unknown" in draft["body"]
    assert {p: p.read_bytes() for p in before} == before
    project.write_text(project.read_text().replace("Partial history", "Still partial history"))
    assert prepare(snapshot)[0]["logical_id"] == draft["logical_id"]


def test_explicit_footer_can_pin_historical_revision_without_selecting_latest(snapshot):
    library_path, _, _, _, _, item, _ = snapshot
    item.messages[1].text = "Later changed answer."
    with Library(library_path, writable=True) as library:
        assert library.put(item)["version"] == "1.1.0"
    [draft] = prepare(snapshot)
    assert {ref["version"] for ref in draft["sources"]} == {"1.0.0"}


@pytest.mark.parametrize(
    "mutation, error",
    [
        ("raw_hash", "source-revision"),
        ("footer_digest", "declared-revision"),
        ("footer_version", "declared-revision"),
        ("missing_heading", "heading"),
        ("duplicate_heading", "heading"),
        ("traversal", "path"),
        ("absolute_path", "path"),
        ("encoded_traversal", "path"),
        ("missing_link_anchor", "wikilink"),
        ("missing_footer", "declared-revision"),
    ],
)
def test_invalid_bindings_are_refused(snapshot, mutation, error):
    _, _, _, target, project, _, revision = snapshot
    if mutation == "raw_hash":
        target.write_text(target.read_text().replace(RAW_HASH, "b" * 64))
    elif mutation == "footer_digest":
        project.write_text(project.read_text().replace(revision["digest"], "b" * 64))
    elif mutation == "footer_version":
        project.write_text(project.read_text().replace("revision `1.0.0`", "revision `9.0.0`"))
    elif mutation == "missing_heading":
        target.write_text(target.read_text().replace("### User ·", "### Absent ·"))
    elif mutation == "duplicate_heading":
        target.write_text(target.read_text() + "\n### User · 2026-01-26 18:06\n")
    elif mutation in {"traversal", "absolute_path", "encoded_traversal"}:
        prefix = {"traversal": "../", "absolute_path": "/", "encoded_traversal": "%2e%2e/"}[
            mutation
        ]
        project.write_text(
            project.read_text().replace("[[conversations/", "[[" + prefix + "conversations/")
        )
    elif mutation == "missing_link_anchor":
        project.write_text(project.read_text().replace("#User · 2026-01-26 18:06", ""))
    elif mutation == "missing_footer":
        project.write_text(project.read_text().split("The cited conversation's CCE identity")[0])
    with pytest.raises(ValueError, match=error):
        prepare(snapshot)


def test_ambiguous_role_and_utc_minute_is_refused(snapshot):
    library_path, _, _, _, project, item, revision = snapshot
    item.messages.append(Message(role="user", text="A short request.", ts="2026-01-26T18:06:58Z"))
    with Library(library_path, writable=True) as library:
        changed = library.put(item)
    project.write_text(
        project.read_text()
        .replace("revision `1.0.0`", "revision `1.1.0`")
        .replace(
            revision["digest"],
            changed["digest"],
        )
    )
    with pytest.raises(ValueError, match="ambiguous-message"):
        prepare(snapshot)


def test_unique_rendered_section_disambiguates_same_minute_with_exact_content(snapshot):
    library_path, _, _, target, project, item, revision = snapshot
    original_text = (
        "# Draft [[Source]]\n\nUncertainty remains.\n\n```md\n### User · 2026-01-26 18:06\n```"
    )
    item.messages[0].text = original_text
    item.messages.append(Message(role="user", text="Other request.", ts="2026-01-26T18:06:58Z"))
    with Library(library_path, writable=True) as library:
        changed = library.put(item)
    project.write_text(
        project.read_text()
        .replace("revision `1.0.0`", "revision `1.1.0`")
        .replace(
            revision["digest"],
            changed["digest"],
        )
    )
    target.write_text(
        target.read_text().replace(
            "A short request.",
            "\\# Draft \\[\\[Source\\]\\]\n\nUncertainty remains.\n\n```md\n### User · 2026-01-26 18:06\n```",
        )
    )
    [draft] = prepare(snapshot)
    assert draft["sources"][0]["message"] == 0
    whole_digest = hashlib.sha256(original_text.encode()).hexdigest()
    assert draft["sources"][0]["digest"] == whole_digest
    binding = draft["provenance"]["citation_bindings"][0]
    assert binding["method"] == "unique-rendered-section"
    assert binding["whole_message_digest"] == whole_digest
    assert binding["renderer"] == "archive_vault.render_message"
    target.write_text(target.read_text().replace("Uncertainty remains.", "Uncertainty changed."))
    with pytest.raises(ValueError, match="ambiguous-message"):
        prepare(snapshot)


def test_explicit_project_selection_keeps_other_unresolved_snapshots_out_of_batch(snapshot):
    library_path, vault, projects, _, project, *_ = snapshot
    (projects / "Unresolved.md").write_text("# Unresolved\n")
    with Library(library_path) as library:
        [draft] = prepare_project_drafts(library, vault, projects, ID_KEY, project_paths=[project])
        assert draft["title"] == "Example project"
        with pytest.raises(ValueError, match="declared-revision"):
            prepare_project_drafts(library, vault, projects, ID_KEY)
        with pytest.raises(ValueError, match="path"):
            prepare_project_drafts(
                library, vault, projects, ID_KEY, project_paths=[vault / "other.md"]
            )
        with pytest.raises(ValueError, match="duplicate"):
            prepare_project_drafts(
                library, vault, projects, ID_KEY, project_paths=[project, project]
            )


def test_raw_hash_cannot_choose_between_unpinned_revisions(snapshot):
    library_path, _, _, _, project, _, _ = snapshot
    other = Conversation(
        provider="chatgpt",
        source_id="other",
        title="Other",
        messages=[
            Message(role="user", text="First", ts="2026-01-26T18:06:01Z"),
        ],
        provenance={"source_sha256": RAW_HASH},
    )
    with Library(library_path, writable=True) as library:
        library.put(other)
        other.messages[0].text = "Second"
        library.put(other)
    other_target = project.parent.parent / "conversations" / "other.md"
    other_target.write_text(
        f"---\nuid: chatgpt-other\nsource_sha256: {RAW_HASH}\n---\n"
        "### User · 2026-01-26 18:06\nFirst\n"
    )
    project.write_text(
        project.read_text() + "\n[[conversations/other#User · 2026-01-26 18:06|Other]]\n"
    )
    with pytest.raises(ValueError, match="ambiguous-revision"):
        prepare(snapshot)


@pytest.mark.parametrize("which", ["project", "target", "target_parent", "projects_root"])
def test_symlink_inputs_are_refused(snapshot, which):
    _, vault, projects, target, project, *_ = snapshot
    path = {
        "project": project,
        "target": target,
        "target_parent": target.parent,
        "projects_root": projects,
    }[which]
    preserved = vault.parent / ("preserved-" + path.name)
    path.rename(preserved)
    path.symlink_to(preserved, target_is_directory=preserved.is_dir())
    with pytest.raises(ValueError, match="symlink"):
        prepare(snapshot)


@pytest.mark.parametrize("which", ["project", "target", "links", "projects"])
def test_input_bounds_are_enforced(snapshot, which):
    _, _, projects, target, project, *_ = snapshot
    if which == "project":
        project.write_bytes(b"x" * (adapter.MAX_PROJECT_BYTES + 1))
    elif which == "target":
        target.write_bytes(b"x" * (adapter.MAX_TARGET_BYTES + 1))
    elif which == "links":
        link = "[[conversations/conversation-chatgpt-synthetic#User · 2026-01-26 18:06|R]]\n"
        project.write_text(project.read_text() + link * adapter.MAX_LINKS)
    elif which == "projects":
        for number in range(adapter.MAX_PROJECTS):
            (projects / f"extra-{number}.md").write_text("# Extra\n")
    with pytest.raises(ValueError, match="bound"):
        prepare(snapshot)


def test_projects_root_must_be_inside_the_vault(snapshot):
    library_path, vault, *_ = snapshot
    with Library(library_path) as library, pytest.raises(ValueError, match="path"):
        prepare_project_drafts(library, vault, vault.parent, ID_KEY)
