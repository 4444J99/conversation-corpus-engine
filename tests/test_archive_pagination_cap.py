"""Hub pagination can be bounded by an entry count as well as by bytes."""

from __future__ import annotations

from conversation_corpus_engine.archive_split import paginate_rows
from conversation_corpus_engine.archive_vault import hub_pages


def test_a_row_cap_closes_a_page_before_the_byte_budget_does() -> None:
    rows = [f"- row {number}" for number in range(25)]
    pages = paginate_rows([], rows, 10_000, 10_000, max_rows=10)
    assert [len(page) for page in pages] == [10, 10, 5]
    assert [row for page in pages for row in page] == rows


def test_the_byte_budget_still_applies_under_a_row_cap() -> None:
    rows = ["x" * 400 for _ in range(12)]
    pages = paginate_rows([], rows, 1000, 1000, max_rows=100)
    assert all(len(page) <= 2 for page in pages)
    assert sum(len(page) for page in pages) == 12


def test_without_a_row_cap_pagination_is_unchanged() -> None:
    rows = [f"- row {number}" for number in range(25)]
    assert paginate_rows([], rows, 10_000, 10_000) == [rows]
    assert paginate_rows(["# Head"], rows, 10_000, 10_000, max_rows=None) == [rows]


def test_the_head_does_not_count_as_a_row() -> None:
    rows = [f"- row {number}" for number in range(4)]
    pages = paginate_rows(["# Head", "", "intro"], rows, 10_000, 10_000, max_rows=4)
    assert pages == [rows]


def test_an_archive_hub_pages_at_the_entry_cap_and_keeps_its_first_name() -> None:
    items = [
        {
            "uid": f"chatgpt-c{number:03d}",
            "title": f"Conversation {number}",
            "provider": "chatgpt",
            "message_count": 2,
            "created": "2026-01-01",
            "last_activity": "",
        }
        for number in range(230)
    ]
    pages = hub_pages("provider-chatgpt", "ChatGPT", "230 conversations.", items, 200_000, 100)
    assert pages[0][0] == "provider-chatgpt" and len(pages) == 3
    assert [text.count("\n- ") for _, text in pages] == [100, 100, 30]
    unbounded = hub_pages("provider-chatgpt", "ChatGPT", "230 conversations.", items, 200_000)
    assert len(unbounded) == 1
