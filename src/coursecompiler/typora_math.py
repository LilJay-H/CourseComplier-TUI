"""Position-preserving adaptation of mathematical HTML folds for Typora."""
from __future__ import annotations

from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
import re

from .math_markup import math_diagnostics, math_spans, repair_math_escapes, source_lines
from .resources import markdown_parser


_TAG = re.compile(r'''<!--.*?-->|</?([A-Za-z][\w:-]*)(?:\s+(?:"[^"]*"|'[^']*'|[^'"<>])*)?\s*/?>''', re.S)


def literal_ranges(text: str) -> list[tuple[int, int]]:
    """Protect examples even where CommonMark considers a fence raw HTML."""
    ranges = []
    offsets = [0]
    for line in source_lines(text):
        offsets.append(offsets[-1] + len(line))
    for token in markdown_parser().parse(text):
        if token.type in {'fence', 'code_block'} and token.map:
            ranges.append((offsets[token.map[0]], offsets[token.map[1]]))
    pattern = re.compile(r'<!--.*?-->|<(pre|code|script|style)\b[^>]*>.*?</\1\s*>|`+', re.S | re.I)
    position = 0
    while match := pattern.search(text, position):
        start, end = match.span()
        if match.group().startswith('`'):
            # Runs at line start can be fences hidden inside an HTML block.
            line_start = text.rfind('\n', 0, start) + 1
            prefix = text[line_start:start]
            if len(match.group()) >= 3 and len(prefix) <= 3 and not prefix.strip():
                close = re.search(r'(?m)^ {0,3}`{' + str(len(match.group())) + r',}[ \t]*\r?$', text[end:])
                end = end + close.end() if close else len(text)
            else:
                close = next((m for m in re.finditer(r'`+', text[end:]) if m.group() == match.group()), None)
                if not close:
                    position = end
                    continue
                end += close.end()
        ranges.append((start, end))
        position = end
    # Tilde fences hidden inside raw HTML need the same protection.
    for match in re.finditer(r'(?m)^ {0,3}(~{3,})[^\r\n]*(?:\r?\n|$)', text):
        if any(a <= match.start() < b for a, b in ranges):
            continue
        close = re.search(r'(?m)^ {0,3}~{' + str(len(match.group(1))) + r',}[ \t]*\r?$', text[match.end():])
        ranges.append((match.start(), match.end() + close.end() if close else len(text)))
    return sorted(ranges)


def _mask(text: str, ranges: list[tuple[int, int]]) -> str:
    chars = list(text)
    for start, end in ranges:
        chars[start:end] = ['\n' if c == '\n' else '\r' if c == '\r' else ' ' for c in text[start:end]]
    return ''.join(chars)


def prepare_math_copy(text: str, *, repair_escapes: bool = False) -> str:
    """Apply shared diagnostics/recovery without touching hidden HTML examples."""
    if repair_escapes:
        pieces, previous = [], 0
        for start, end in literal_ranges(text):
            if end <= previous:
                continue
            start = max(start, previous)
            pieces.extend((repair_math_escapes(text[previous:start]), text[start:end]))
            previous = end
        pieces.append(repair_math_escapes(text[previous:]))
        text = ''.join(pieces)
    problems = math_diagnostics(_mask(text, literal_ranges(text)))
    if problems:
        raise ValueError('；'.join(problems))
    return text


@dataclass
class _Fold:
    start: int
    end: int = 0
    expanded: bool = False


class _Attributes(HTMLParser):
    def handle_starttag(self, tag, attrs):
        self.attrs = dict(attrs)


def _attributes(tag: str) -> dict:
    parser = _Attributes()
    parser.feed(tag)
    return parser.attrs


@dataclass
class _Block:
    tag: str
    start: int
    body_start: int
    attrs: dict
    body_end: int = 0
    end: int = 0
    children: list = field(default_factory=list)


def _number(value: str) -> int:
    if not re.fullmatch(r'[0-9]{1,9}', value):
        raise ValueError('有序列表编号超出 Markdown 支持范围，无法安全转换')
    return int(value)


def expand_math_details(text: str) -> str:
    """Unwrap folds containing math, preserving unrelated folds and code bytes."""
    literals = literal_ranges(text)
    visible = _mask(text, literals)
    formulas = list(math_spans(visible))
    if not formulas:
        return text
    tags = list(_TAG.finditer(_mask(visible, [(s.start, s.end) for s in formulas])))
    folds, stack = [], []
    for tag in tags:
        if (tag.group(1) or '').lower() != 'details':
            continue
        if tag.group().startswith('</'):
            if stack:
                stack.pop().end = tag.end()
        else:
            fold = _Fold(tag.start())
            folds.append(fold)
            stack.append(fold)
    for fold in folds:
        stop = fold.end or len(text)
        fold.expanded = any(fold.start <= span.start < stop for span in formulas)
        if fold.expanded and not fold.end:
            raise ValueError('含公式的 details 未闭合，无法安全展开')

    # Raw HTML tables cannot host Typora Markdown math. Fail before publishing,
    # including tables produced from galleries whose captions contain formulas.
    tables = []
    for tag in tags:
        if (tag.group(1) or '').lower() == 'table':
            if tag.group().startswith('</'):
                if tables:
                    start = tables.pop()
                    if any(start <= span.start < tag.end() for span in formulas):
                        raise ValueError('Typora 不支持 HTML 表格内公式；请先改为 Markdown 表格或表外公式')
            else:
                tables.append(tag.start())
    if any(span.start >= start for start in tables for span in formulas):
        raise ValueError('含公式的 HTML 表格未闭合，无法安全转换')
    if not any(f.expanded for f in folds):
        return text

    def active(start, end):
        containers = [f for f in folds if f.start <= start and end <= f.end]
        return bool(containers) and all(f.expanded for f in containers)

    edits = []
    links = []
    blocks, block_stack = [], []
    newline = '\r\n' if '\r\n' in text else '\r' if '\r' in text and '\n' not in text else '\n'
    for tag in tags:
        if not active(tag.start(), tag.end()):
            continue
        name = (tag.group(1) or '').lower()
        closing = tag.group().startswith('</')
        replacement = None
        if name in {'summary', 'blockquote', 'ul', 'ol', 'li'}:
            if closing:
                if not block_stack or block_stack[-1].tag != name:
                    raise ValueError('折叠内引用或列表标签未配对，无法安全转换')
                block = block_stack.pop()
                block.body_end, block.end = tag.start(), tag.end()
            else:
                block = _Block(name, tag.start(), tag.end(), _attributes(tag.group()))
                (block_stack[-1].children if block_stack else blocks).append(block)
                block_stack.append(block)
            continue
        if name in {'details', 'p', 'div', 'section', 'article'}:
            replacement = newline * 2
        elif name == 'br':
            replacement = '  ' + newline
        elif name in {'span', 'small'}:
            replacement = ''
        elif name in {'strong', 'b'}:
            replacement = '**'
        elif name in {'em', 'i'}:
            replacement = '*'
        elif name == 'a':
            if closing and links:
                replacement = links.pop()
            elif not closing:
                attrs = _attributes(tag.group())
                # Retain anchors or links with extra semantics as their original HTML.
                if attrs.get('href') and set(attrs) <= {'href', 'title'}:
                    href = attrs['href'].replace('<', '%3C').replace('>', '%3E')
                    title = (' "' + attrs['title'].replace('"', '&quot;') + '"') if attrs.get('title') else ''
                    replacement = '['
                    links.append('](<' + href + '>' + title + ')')
                else:
                    links.append('</a>')
        if replacement is not None:
            edits.append((tag.start(), tag.end(), replacement))
    if block_stack:
        raise ValueError('折叠内引用或列表标签未闭合，无法安全转换')
    for span in formulas:
        if active(span.start, span.end) and span.display:
            body = unescape(text[span.start + 2:span.end - 2]).strip('\r\n')
            edits.append((span.start, span.end, newline * 2 + '$$' + newline
                          + body + newline + '$$' + newline * 2))
    # Decode HTML text, but never entities inside tags, code or retained folds.
    excluded = literals + [(t.start(), t.end()) for t in tags] + [
        (s.start, s.end) for s in formulas if s.display]
    for entity in re.finditer(r'&(?:#[xX][0-9a-fA-F]+|#\d+|[A-Za-z][A-Za-z0-9]+);', text):
        if active(*entity.span()) and not any(a <= entity.start() < b for a, b in excluded):
            edits.append((*entity.span(), unescape(entity.group())))
    def fragment(start, end, children=()):
        replacements = [e for e in edits if start <= e[0] and e[1] <= end
                        and not any(c.start <= e[0] and e[1] <= c.end for c in children)]
        replacements += [(c.start, c.end, render(c)) for c in children]
        body = text[start:end]
        for a, b, replacement in sorted(replacements, reverse=True):
            body = body[:a - start] + replacement + body[b - start:]
        return body

    def render(block):
        if block.tag in {'ol', 'ul'}:
            items = block.children
            if any(item.tag != 'li' for item in items):
                raise ValueError('列表包含非 li 子结构，无法安全转换')
            previous = block.body_start
            for item in items:
                if text[previous:item.start].strip():
                    raise ValueError('列表项目之间混入内容，无法安全转换')
                previous = item.end
            if text[previous:block.body_end].strip():
                raise ValueError('列表项目之外混入内容，无法安全转换')
            ordered = block.tag == 'ol'
            if ordered and block.attrs.get('type', '1') != '1':
                raise ValueError('暂不支持非数字有序列表，无法安全转换')
            step = -1 if 'reversed' in block.attrs else 1
            number = _number(block.attrs.get('start', str(len(items) if step < 0 else 1)))
            output = []
            last_number = None
            for item in items:
                if ordered and 'value' in item.attrs:
                    number = _number(item.attrs['value'])
                if ordered and not 0 <= number <= 999999999:
                    raise ValueError('有序列表编号超出 Markdown 支持范围，无法安全转换')
                marker = f'{number}. ' if ordered else '- '
                content = fragment(item.body_start, item.body_end, item.children).strip('\r\n ')
                lines = source_lines(content)
                entry = marker + (lines[0] if lines else '')
                entry += ''.join(' ' * len(marker) + line for line in lines[1:])
                # Markdown ignores nonsequential later markers. Start a new list
                # for HTML value resets/reversed numbering so the renderer keeps them.
                if ordered and last_number is not None and number != last_number + 1:
                    output.append('<!-- list numbering -->')
                output.append(entry)
                last_number = number
                number += step
            return newline * 2 + (newline * 2).join(output) + newline * 2
        body = fragment(block.body_start, block.body_end, block.children).strip('\r\n ')
        if block.tag == 'blockquote':
            return newline * 2 + ''.join('> ' + line for line in source_lines(body)) + newline * 2
        if block.tag == 'summary':
            if body.startswith('$$' + newline):
                # A display-only summary still needs a heading, but putting $$
                # on that heading line would prevent display-math parsing.
                return newline * 2 + '### 详情' + newline * 2 + body + newline * 2
            return newline * 2 + '### ' + body + newline * 2
        raise ValueError('li 必须位于列表内，无法安全转换')

    return fragment(0, len(text), blocks)
