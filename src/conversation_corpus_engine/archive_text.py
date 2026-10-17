from __future__ import annotations

import html
import re
from collections import Counter
from html.parser import HTMLParser

from .answering import tokenize

FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([\w+#.-]*)\s*$")
URL_RE = re.compile(r"https?://\S+")
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
ITALIC_RE = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])")
LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+|mailto:[^)\s]+)\)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
NUMBERED_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
HEX_RE = re.compile(r"^[0-9a-f]{8,}$")

STOP_WORDS = frozenset(
    """
    about above after again against all also always among and any are around because been before
    being below between both but can cannot could did does doing done down during each either else
    even every few for from further get gets getting give given goes going had has have having her
    here hers him his how however into its itself just keep know let like likely make makes many may
    might more most much must need needs never new next not now off often once one only onto other
    ought our ours out over own per please put quite rather really same say see seem seems several
    shall she should since some something still such sure take than that the their theirs them then
    there these they thing things this those though through thus too under until upon use used uses
    using very want wants was way ways well were what when where whether which while who whom whose
    why will with within without would yes yet you your yours yourself able actually already also
    another anything anyone around back based better big
    help provide include includes including following example examples sure okay thanks
    thank hello hey great good best question answer respond response
    prompt user assistant model chat conversation message messages untitled
    """.split()  # noqa: SIM905 - one readable block beats 200 quoted literals
)


CODE_WORDS = frozenset(
    """
    const classname function return import export default class true false null undefined href
    https http html css div span style script width height color background rgba void async await
    string number boolean object array props state render
    """.split()  # noqa: SIM905 - one readable block beats a column of quoted literals
)


class _HtmlToText(HTMLParser):
    BLOCK = {"p", "div", "section", "article", "table", "tr", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.href_stack: list[str] = []
        self.list_stack: list[list[int]] = []
        self.in_pre = 0
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n\n")
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "pre":
            self.in_pre += 1
            self.parts.append("\n\n```\n")
        elif tag == "code" and not self.in_pre:
            self.parts.append("`")
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in {"strong", "b"}:
            self.parts.append("**")
        elif tag in {"em", "i"}:
            self.parts.append("*")
        elif tag in {"ul", "ol"}:
            self.parts.append("\n\n")
            self.list_stack.append([0 if tag == "ul" else 1])
        elif tag == "li":
            marker = "- "
            if self.list_stack and self.list_stack[-1][0] > 0:
                marker = f"{self.list_stack[-1][0]}. "
                self.list_stack[-1][0] += 1
            self.parts.append("\n" + marker)
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            self.href_stack.append(href)
            self.parts.append("[")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n\n")
        elif tag == "pre":
            self.in_pre = max(0, self.in_pre - 1)
            self.parts.append("\n```\n\n")
        elif tag == "code" and not self.in_pre:
            self.parts.append("`")
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n\n")
        elif tag in {"strong", "b"}:
            self.parts.append("**")
        elif tag in {"em", "i"}:
            self.parts.append("*")
        elif tag in {"ul", "ol"} and self.list_stack:
            self.list_stack.pop()
            self.parts.append("\n\n")
        elif tag == "a" and self.href_stack:
            href = self.href_stack.pop()
            self.parts.append(f"]({href})" if href.startswith(("http://", "https://")) else "]")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _HtmlToText()
    parser.feed(markup or "")
    parser.close()
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def strip_code_and_urls(text: str) -> str:
    lines: list[str] = []
    fence = ""
    for line in (text or "").splitlines():
        marker = FENCE_RE.match(line)
        if marker:
            opener = marker.group(2)[0]
            if not fence:
                fence = opener
            elif fence == opener:
                fence = ""
            continue
        if not fence:
            lines.append(line)
    return URL_RE.sub(" ", INLINE_CODE_RE.sub(" ", "\n".join(lines)))


def term_counts(text: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for raw in tokenize(strip_code_and_urls(text)):
        word = raw.strip("-_")
        if len(word) < 4 or len(word) > 24 or word in STOP_WORDS or word in CODE_WORDS:
            continue
        if "_" in word:
            continue
        if word.isdigit() or HEX_RE.match(word) or sum(ch.isdigit() for ch in word) > 2:
            continue
        counts[word] += 1
    return counts


def _inline(escaped: str) -> str:
    escaped = INLINE_CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", escaped)
    escaped = BOLD_RE.sub(lambda m: f"<strong>{m.group(1)}</strong>", escaped)
    escaped = ITALIC_RE.sub(lambda m: f"<em>{m.group(1)}</em>", escaped)
    return LINK_RE.sub(
        lambda m: f'<a href="{m.group(2)}" rel="noopener noreferrer nofollow">{m.group(1)}</a>',
        escaped,
    )


def render_markdown_html(text: str) -> str:
    """Render a conservative markdown subset to HTML. Input is escaped before any transform."""
    out: list[str] = []
    paragraph: list[str] = []
    list_kind = ""
    fence = ""
    code: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            out.append(
                "<p>" + "<br>".join(_inline(html.escape(line)) for line in paragraph) + "</p>"
            )
            paragraph.clear()

    def close_list() -> None:
        nonlocal list_kind
        if list_kind:
            out.append(f"</{list_kind}>")
            list_kind = ""

    for line in (text or "").splitlines():
        marker = FENCE_RE.match(line)
        if fence:
            if marker and marker.group(2)[0] == fence:
                out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
                code.clear()
                fence = ""
            else:
                code.append(line)
            continue
        if marker:
            flush_paragraph()
            close_list()
            fence = marker.group(2)[0]
            continue
        if not line.strip():
            flush_paragraph()
            close_list()
            continue
        heading = HEADING_RE.match(line)
        bullet = BULLET_RE.match(line)
        numbered = NUMBERED_RE.match(line)
        if heading:
            flush_paragraph()
            close_list()
            level = min(6, len(heading.group(1)) + 2)
            out.append(f"<h{level}>{_inline(html.escape(heading.group(2)))}</h{level}>")
        elif item := bullet or numbered:
            flush_paragraph()
            kind = "ul" if bullet else "ol"
            if list_kind != kind:
                close_list()
                out.append(f"<{kind}>")
                list_kind = kind
            out.append(f"<li>{_inline(html.escape(item.group(1)))}</li>")
        else:
            close_list()
            paragraph.append(line)
    if fence:
        out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
    flush_paragraph()
    close_list()
    return "\n".join(out)
