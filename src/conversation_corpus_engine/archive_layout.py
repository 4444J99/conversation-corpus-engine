"""Every file, folder and note name the archive layer writes, derived through `derive_name`.

Nothing in the archive layer spells a name by hand. The archive folder, the manifest, the search
database, the vault, the site, the graph, the per-provider stores, the prompt feeds and their
receipts, every note title and every wikilink come from here, so a name written to disk and the
link that points at it can never drift apart.

Two plain words are not minted names: `conversations` (a folder of conversation notes or pages)
and the vault folders `hubs` and `meta`. They are the contract's structural names
(`archive_naming.STRUCTURAL_NAMES`).
"""

from __future__ import annotations

from typing import Any

from .archive_naming import (
    InvalidName,
    conversation_name,
    derive_name,
    hash_identity,
    validate_name,
)

# --- the archive folder and what sits directly in it ---------------------------------------

ARCHIVE_DIRNAME = derive_name("archive", scope="all")
MANIFEST_FILENAME = derive_name("manifest", scope="all") + ".json"
DATABASE_FILENAME = derive_name("database", scope="all", subject="search") + ".sqlite"
ENRICHMENT_FILENAME = derive_name("index", scope="all", subject="enrichment") + ".jsonl"
TOPICS_FILENAME = derive_name("report", scope="all", subject="topics") + ".json"
VAULT_DIRNAME = derive_name("vault", scope="all")
SITE_DIRNAME = derive_name("site", scope="all")
GRAPH_DIRNAME = derive_name("graph", scope="all")

# --- the vault ------------------------------------------------------------------------------

VAULT_CONVERSATIONS_DIRNAME = "conversations"
VAULT_HUBS_DIRNAME = "hubs"
VAULT_META_DIRNAME = "meta"
HOME_NOTE = derive_name("home", scope="all")
COVERAGE_NOTE = derive_name("coverage", scope="all")
GUIDE_NOTE = derive_name("guide", scope="all")

# --- the graph ------------------------------------------------------------------------------

GRAPH_NODES_FILENAME = derive_name("index", scope="all", subject="nodes") + ".jsonl"
GRAPH_EDGES_FILENAME = derive_name("index", scope="all", subject="edges") + ".jsonl"
GRAPH_GRAPHML_FILENAME = derive_name("graph", scope="all") + ".graphml"
GRAPH_SUMMARY_FILENAME = derive_name("report", scope="all", subject="graph") + ".json"

# --- the site -------------------------------------------------------------------------------

SITE_PAGES_DIRNAME = "conversations"
SITE_HOME_FILENAME = HOME_NOTE + ".html"
SITE_GRAPH_PAGE_FILENAME = derive_name("graph", scope="all") + ".html"
SITE_STYLE_FILENAME = SITE_DIRNAME + ".css"
SITE_HOME_SCRIPT_FILENAME = HOME_NOTE + ".js"
SITE_GRAPH_SCRIPT_FILENAME = derive_name("graph", scope="all") + ".js"
SITE_INDEX_DATA_FILENAME = derive_name("index", scope="all", subject="conversations") + ".js"
SITE_TOPIC_DATA_FILENAME = derive_name("index", scope="all", subject="topic-graph") + ".js"


# --- per-provider names ---------------------------------------------------------------------


def provider_dirname(provider: str) -> str:
    """Folder holding one provider's normalized files."""
    return derive_name("provider", scope=provider)


def conversations_filename(provider: str) -> str:
    """The provider's full normalized conversations, one JSON row each."""
    return derive_name("collection", scope=provider, subject="conversations") + ".jsonl"


def index_filename(provider: str) -> str:
    """The provider's light conversation index, one summary row each."""
    return derive_name("index", scope=provider, subject="conversations") + ".jsonl"


def feed_filename(provider: str) -> str:
    """The provider's prompt feed."""
    return derive_name("feed", scope=provider, subject="prompts") + ".jsonl"


def receipt_filename(provider: str, fingerprint: str, day: str) -> str:
    """The receipt that binds a prompt feed to the manifest fingerprint and its latest day."""
    return (
        derive_name("receipt", scope=provider, subject="prompts", identity=fingerprint, date=day)
        + ".json"
    )


# --- note and page names --------------------------------------------------------------------


def conversation_note(provider: str, uid: str) -> str:
    """`conversation-<uid>`: the title of a conversation's vault note and the stem of its page."""
    return conversation_name(provider, uid)


CONVERSATION_PREFIX = "conversation-"
if conversation_note("chatgpt", "chatgpt-x") != f"{CONVERSATION_PREFIX}chatgpt-x":
    # The site's script builds page addresses from this prefix; refuse to import if the naming
    # contract ever stops shaping a conversation name as `conversation-<uid>`.
    raise RuntimeError("conversation names no longer have the form conversation-<uid>")


def conversation_note_for_uid(uid: str) -> str:
    """The note title for a conversation known only by its uid (`<provider>-<tail>`)."""
    return conversation_note(uid.partition("-")[0], uid)


def provider_note(provider: str) -> str:
    return derive_name("provider", scope=provider)


def month_note(stamp: str) -> str | None:
    """`month-all-<yyyymm>` from a normalized timestamp; None when the stamp holds no real month.

    There is no month note for an undated conversation: the subject must be a real month and an
    invented placeholder would be a fake one.
    """
    digits = (stamp or "")[:7].replace("-", "")
    try:
        return derive_name("month", scope="all", subject=digits)
    except InvalidName:
        return None


def topic_note(term: str) -> str | None:
    """`topic-all-<term>`; None when the term is not one valid subject word.

    A term is validated, never rewritten, so a term that is not a plain lowercase word gets no
    hub of its own (it stays a plain label).
    """
    try:
        return derive_name("topic", scope="all", subject=term)
    except InvalidName:
        return None


def collection_subject(collection: str) -> str:
    """The subject word for a collection (project, notebook, GPT).

    A collection is an identifier, never a title. If the identifier is already a valid subject it
    is used as it is; if not (too long, or not lowercase words), its 16 hex character digest
    stands in, so the same collection always gets the same name.
    """
    try:
        derive_name("collection", scope="chatgpt", subject=collection)
    except InvalidName:
        return hash_identity(collection)
    return collection


def collection_note(provider: str, collection: str) -> str:
    return derive_name("collection", scope=provider, subject=collection_subject(collection))


def wikilink(target: str, label: str | None = None, *, in_table: bool = False) -> str:
    """The one place a `[[wikilink]]` is written.

    The target must be a valid contract name, so a link can only point at a note whose title was
    minted by this module. Inside a markdown table the pipe has to be escaped.
    """
    problems = validate_name(target)
    if problems:
        raise InvalidName(repr(target), problems)
    if label is None:
        return f"[[{target}]]"
    separator = "\\|" if in_table else "|"
    return f"[[{target}{separator}{label}]]"


def layout_payload() -> dict[str, Any]:
    """The fixed names of the layout, for documentation and tests."""
    return {
        "archive": ARCHIVE_DIRNAME,
        "manifest": MANIFEST_FILENAME,
        "database": DATABASE_FILENAME,
        "enrichment": ENRICHMENT_FILENAME,
        "topics": TOPICS_FILENAME,
        "vault": VAULT_DIRNAME,
        "site": SITE_DIRNAME,
        "graph": GRAPH_DIRNAME,
        "home": HOME_NOTE,
        "coverage": COVERAGE_NOTE,
        "guide": GUIDE_NOTE,
    }
