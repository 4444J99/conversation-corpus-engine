"""Exact UTF-8 code-span boundaries from the pinned CommonMark parser.

Block container stripping is traced at getLines, rather than inferred from
rendered token text. Only inline blocks crossing actual cuts are parsed.
"""

from __future__ import annotations

import bisect
import importlib.metadata
import re
import types
from array import array

from markdown_it import MarkdownIt
from markdown_it.rules_block.state_block import StateBlock
from markdown_it.rules_inline.backticks import backtick

VERSION = "4.2.0"
LINES = re.compile(rb"[^\r\n]*(?:\r\n|\r|\n|$)")


def _require(condition, message):
    if not condition:
        raise ValueError(message)


class SourceMap:
    """Sparse line offsets; never allocate one Python integer per character."""

    def __init__(self, raw, normalized, check):
        self.raw = raw
        self.bytes = array("Q")
        self.characters = array("Q")
        self.body_ends = array("Q")
        position = 0
        for index, match in enumerate(LINES.finditer(raw)):
            value = match[0]
            if not value:
                continue
            if index % 4096 == 0:
                check()
            body = value.rstrip(b"\r\n")
            self.bytes.append(match.start())
            self.characters.append(position)
            self.body_ends.append(match.start() + len(body))
            position += len(body.decode("utf-8")) + (len(body) != len(value))
        self.bytes.append(len(raw))
        self.characters.append(position)
        _require(position == len(normalized), "uncertain normalized source mapping")

    def to_bytes(self, position):
        _require(0 <= position <= self.characters[-1], "character position outside source")
        if position == self.characters[-1]:
            return len(self.raw)
        line = bisect.bisect_right(self.characters, position) - 1
        start, end = self.bytes[line], self.body_ends[line]
        column = position - self.characters[line]
        body = self.raw[start:end].decode("utf-8")
        if column <= len(body):
            return start + len(body[:column].encode("utf-8"))
        return self.bytes[line + 1]

    def to_characters(self, position):
        _require(0 <= position <= len(self.raw), "byte position outside source")
        if position == len(self.raw):
            return self.characters[-1]
        line = bisect.bisect_right(self.bytes, position) - 1
        start, end = self.bytes[line], self.body_ends[line]
        if position <= end:
            return self.characters[line] + len(self.raw[start:position].decode("utf-8"))
        return self.characters[line + 1]


class InlineMap:
    def __init__(self, text, rows):
        self.text = text
        self.content = text.strip()
        self.trim = len(text) - len(text.lstrip())
        self.rows = rows
        self.output_starts = [row[0] for row in rows]
        self.source_starts = [row[1] for row in rows]

    def to_source(self, position):
        target = position + self.trim
        index = bisect.bisect_right(self.output_starts, target) - 1
        _require(index >= 0, "inline position precedes mapped source")
        output, start, end, padding = self.rows[index]
        delta = target - output - padding
        _require(0 <= delta <= end - start, "code delimiter lies in synthetic indentation")
        return start + delta

    def to_inline(self, position):
        index = bisect.bisect_right(self.source_starts, position) - 1
        if index < 0:
            return 0
        output, start, end, padding = self.rows[index]
        target = output + padding + min(position - start, end - start) - self.trim
        return max(0, min(len(self.content), target))


class MappedState(StateBlock):
    def getLines(self, begin, end, indent, keepLastLF):
        value = super().getLines(begin, end, indent, keepLastLF)
        if self.parentType != "paragraph" or begin >= end:
            return value
        # Trace the indentation walk of markdown-it-py 4.2.0 getLines (MIT).
        # The parser's own result remains authoritative and its length is checked.
        rows = []
        output = 0
        for line in range(begin, end):
            if line % 4096 == 0:
                self.env["cce_check"]()
            first = line_start = self.bMarks[line]
            last = self.eMarks[line] + (line + 1 < end or keepLastLF)
            columns = 0
            while first < last and columns < indent:
                character = self.src[first]
                if character == "\t":
                    columns += 4 - (columns + self.bsCount[line]) % 4
                elif character == " " or first - line_start < self.tShift[line]:
                    columns += 1
                else:
                    break
                first += 1
            padding = max(0, columns - indent)
            rows.append((output, first, last, padding))
            output += padding + last - first
        _require(output == len(value), "pinned parser indentation mapping changed")
        self.env["cce_maps"].setdefault(begin, []).append((end, InlineMap(value, rows)))
        return value


def _block_parse(parser, src, md, env, tokens):
    env["cce_normalized"] = src
    if not src:
        return None
    state = MappedState(src, md, env, tokens)
    parser.tokenize(state, state.line, state.lineMax)
    return state.tokens


def _capture_backticks(state, silent):
    begin = state.pos
    count = len(state.tokens)
    result = backtick(state, silent)
    if (
        result
        and not silent
        and len(state.tokens) > count
        and state.tokens[-1].type == "code_inline"
    ):
        state.tokens[-1].meta["cce_span"] = (begin, state.pos, id(state.src))
    state.env["cce_tick_calls"] += 1
    if state.env["cce_tick_calls"] % 1024 == 0:
        state.env["cce_check"]()
    return result


def _mapping(token, env, source_map):
    begin, end = token.map
    candidates = [
        mapping
        for stop, mapping in env["cce_maps"].get(begin, [])
        if stop <= end and mapping.content == token.content
    ]
    if candidates:
        rows = candidates[0].rows
        _require(
            all(candidate.rows == rows for candidate in candidates), "ambiguous container mapping"
        )
        return candidates[0]
    # ATX headings do not call getLines. Accept only an unambiguous exact slice.
    normalized = env["cce_normalized"]
    start, stop = source_map.characters[begin], source_map.characters[end]
    position = normalized.find(token.content, start, stop)
    _require(
        position >= 0 and normalized.find(token.content, position + 1, stop) < 0,
        "uncertain inline source mapping",
    )
    return InlineMap(token.content, [(0, position, position + len(token.content), 0)])


def code_spans(
    raw, cuts, *, check=None, max_source_bytes=128 * 1024**2, max_paragraph_chars=128 * 1024**2
):
    """Return all actual code spans crossing cuts, with original byte coordinates."""
    check = check or (lambda: None)
    check()
    _require(len(raw) <= max_source_bytes, "source exceeds configured byte limit")
    _require(
        importlib.metadata.version("markdown-it-py") == VERSION,
        "unsupported CommonMark parser version",
    )
    cuts = sorted(set(cuts))
    _require(all(0 < cut < len(raw) for cut in cuts), "split boundary outside source")
    parser = MarkdownIt("commonmark", {"html": True})
    parser.block.parse = types.MethodType(_block_parse, parser.block)
    parser.core.ruler.disable(["inline", "text_join"])
    parser.inline.ruler.at("backticks", _capture_backticks)
    env = {"cce_check": check, "cce_maps": {}, "cce_tick_calls": 0}
    tokens = parser.parse(raw.decode("utf-8"), env)
    source_map = SourceMap(raw, env.get("cce_normalized", ""), check)
    stats = {"inline_blocks_seen": 0, "inline_blocks_parsed": 0, "source_bytes": len(raw)}
    result = []
    for token in tokens:
        check()
        if token.type != "inline":
            continue
        stats["inline_blocks_seen"] += 1
        _require(token.map is not None, "inline block lacks source coordinates")
        begin, end = (source_map.bytes[line] for line in token.map)
        first, last = bisect.bisect_right(cuts, begin), bisect.bisect_left(cuts, end)
        if first == last or "`" not in token.content:
            continue
        _require(len(token.content) <= max_paragraph_chars, "paragraph character limit exceeded")
        mapping = _mapping(token, env, source_map)
        local_cuts = [mapping.to_inline(source_map.to_characters(cut)) for cut in cuts[first:last]]
        children = []
        parser.inline.parse(token.content, parser, env, children)
        stats["inline_blocks_parsed"] += 1
        queue = list(children)
        while queue:
            child = queue.pop()
            if child.children:
                queue.extend(child.children)
            if child.type != "code_inline":
                continue
            span = child.meta.get("cce_span")
            _require(
                span is not None and span[2] == id(token.content),
                "uncertain nested inline source mapping",
            )
            left, right, _ = span
            if not any(left < cut < right for cut in local_cuts):
                continue
            start = source_map.to_bytes(mapping.to_source(left))
            stop = source_map.to_bytes(mapping.to_source(right))
            marker = child.markup.encode()
            _require(
                raw[start : start + len(marker)] == marker
                and raw[stop - len(marker) : stop] == marker,
                "code delimiter does not match original bytes",
            )
            crossing = [cut for cut in cuts[first:last] if start < cut < stop]
            _require(crossing, "mapped code span lost its split boundary")
            result.append(
                {
                    "span_start": start,
                    "span_end": stop,
                    "content_start": start + len(marker),
                    "content_end": stop - len(marker),
                    "delimiter_bytes": len(marker),
                    "boundaries": crossing,
                    "boundary_inside_delimiter": any(
                        cut < start + len(marker) or cut > stop - len(marker) for cut in crossing
                    ),
                }
            )
    return sorted(result, key=lambda item: (item["span_start"], item["span_end"])), stats


def annotate(raw, segments, *, check=None):
    cuts = [segment["end"] for segment in segments[:-1]]
    for segment in segments:
        segment.pop("inline_before", None)
        segment.pop("inline_after", None)
    spans, stats = code_spans(raw, cuts, check=check)
    by_cut = {cut: index for index, cut in enumerate(cuts)}
    for span in spans:
        _require(not span["boundary_inside_delimiter"], "split boundary cuts a code delimiter")
        marker = "`" * span["delimiter_bytes"]
        for cut in span["boundaries"]:
            index = by_cut[cut]
            segments[index]["inline_after"] = marker
            segments[index + 1]["inline_before"] = marker
    return stats
