from __future__ import annotations

from conversation_corpus_engine.archive_text import (
    html_to_text,
    render_markdown_html,
    term_counts,
)


def test_html_to_text_preserves_structure() -> None:
    text = html_to_text(
        "<h2>Title</h2><p>One <strong>bold</strong> and <em>em</em> "
        '<a href="https://example.invalid/x">link</a>.</p>'
        "<ul><li>a</li><li>b</li></ul><ol><li>x</li><li>y</li></ol>"
        "<pre><code>kubectl get pods</code></pre><script>evil()</script>"
    )
    assert "## Title" in text
    assert "**bold**" in text
    assert "*em*" in text
    assert "[link](https://example.invalid/x)" in text
    assert "- a\n- b" in text
    assert "1. x\n2. y" in text
    assert "```\nkubectl get pods\n```" in text
    assert "evil" not in text


def test_html_to_text_drops_non_http_hrefs() -> None:
    assert html_to_text('<a href="javascript:alert(1)">x</a>') == "[x]"


def test_markdown_renderer_escapes_before_transforming() -> None:
    rendered = render_markdown_html("<script>alert(1)</script> and **bold** and `code <b>`")
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "<strong>bold</strong>" in rendered
    assert "<code>code &lt;b&gt;</code>" in rendered


def test_markdown_renderer_links_only_safe_schemes() -> None:
    safe = render_markdown_html("[ok](https://example.invalid/a?b=1&c=2)")
    assert 'href="https://example.invalid/a?b=1&amp;c=2"' in safe
    assert 'rel="noopener noreferrer nofollow"' in safe
    unsafe = render_markdown_html("[bad](javascript:alert(1)) [worse](data:text/html,x)")
    assert "<a " not in unsafe


def test_markdown_renderer_handles_fences_lists_and_headings() -> None:
    rendered = render_markdown_html(
        "# Head\n\n- one\n- two\n\n1. first\n\n```python\nx = '<b>'\n```\n\ntail"
    )
    assert "<h3>Head</h3>" in rendered
    assert "<ul>\n<li>one</li>\n<li>two</li>\n</ul>" in rendered
    assert "<ol>\n<li>first</li>\n</ol>" in rendered
    assert "<pre><code>x = &#x27;&lt;b&gt;&#x27;</code></pre>" in rendered
    assert rendered.endswith("<p>tail</p>")


def test_markdown_renderer_closes_unterminated_fence() -> None:
    assert render_markdown_html("```\nopen <i>").endswith("open &lt;i&gt;</code></pre>")


def test_term_counts_ignore_code_urls_stopwords_and_noise() -> None:
    counts = term_counts(
        "Kubernetes cluster kubernetes. visit https://example.invalid/kubernetes now\n"
        "```\nsecretcodeword secretcodeword\n```\n"
        "also `inlinecodeword` the and 12345 deadbeefcafe0123 abc"
    )
    assert counts["kubernetes"] == 2
    assert counts["cluster"] == 1
    for banned in ("secretcodeword", "inlinecodeword", "the", "12345", "deadbeefcafe0123", "abc"):
        assert banned not in counts
