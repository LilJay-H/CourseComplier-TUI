"""Conservative TeX boundaries for reading exports; no semantic rewriting."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable, Iterator

from .resources import markdown_parser

_TAG = re.compile(r'''<!--.*?-->|</?([A-Za-z][\w:-]*)(?:\s+(?:"[^"]*"|'[^']*'|[^'"<>])*)?\s*/?>''', re.S)
_BACKTICKS = re.compile(r'`+')
_LITERAL_TAGS = {'pre', 'code', 'math', 'script', 'style'}


@dataclass(frozen=True)
class MathSpan:
    start: int
    end: int
    content: str
    display: bool
    delimiter: str


def _escaped(text: str, position: int) -> bool:
    previous = position - 1
    while previous >= 0 and text[previous] == '\\':
        previous -= 1
    return (position - previous - 1) % 2 == 1


def math_spans(text: str, *, allow_space: bool = False) -> Iterator[MathSpan]:
    """Find balanced dollar math outside tags, comments and literal code.

    The caller handles fenced/indented Markdown via token maps. Inline formulas
    never cross prose lines or a double delimiter. Attribute values stay literal.
    """
    pos = 0
    literal: list[str] = []
    while pos < len(text):
        if text[pos] == '<':
            tag = _TAG.match(text, pos)
            if tag:
                name = (tag.group(1) or '').lower()
                if name in _LITERAL_TAGS:
                    if tag.group().startswith('</'):
                        if literal and literal[-1] == name:
                            literal.pop()
                    elif not tag.group().endswith('/>'):
                        literal.append(name)
                pos = tag.end()
                continue
        if literal:
            pos += 1
            continue
        if text[pos] == '`' and not _escaped(text, pos):
            opening = _BACKTICKS.match(text, pos)
            closing = next((m for m in _BACKTICKS.finditer(text, opening.end())
                            if m.group() == opening.group()), None)
            pos = closing.end() if closing else opening.end()
            continue
        if text[pos] != '$' or _escaped(text, pos):
            pos += 1
            continue
        length = 2 if text.startswith('$$', pos) else 1
        delimiter = '$' * length
        body_start = pos + length
        if length == 1 and (body_start == len(text) or (not allow_space and text[body_start].isspace())):
            pos = body_start
            continue
        end = body_start
        matched = None
        while end < len(text):
            if length == 1 and text[end] in '\r\n':
                break
            if text[end] != '$' or _escaped(text, end):
                end += 1
                continue
            closing_length = 2 if text.startswith('$$', end) else 1
            if closing_length != length:
                break
            if length == 1 and ((not allow_space and text[end - 1].isspace())
                                or (end + 1 < len(text) and text[end + 1].isdigit())):
                end += 1
                continue
            if end > body_start:
                matched = MathSpan(pos, end + length, text[body_start:end], length == 2, delimiter)
            break
        if matched:
            yield matched
            pos = matched.end
        else:
            pos = body_start


def replace_math(text: str, callback: Callable[[MathSpan], str], *, allow_space: bool = False) -> str:
    pieces, previous = [], 0
    for span in math_spans(text, allow_space=allow_space):
        pieces.extend((text[previous:span.start], callback(span)))
        previous = span.end
    pieces.append(text[previous:])
    return ''.join(pieces)


def source_lines(text: str) -> list[str]:
    """Preserve line endings without treating other control codes as newlines."""
    return [part for part in re.findall(r'[^\r\n]*(?:\r\n|\r|\n|$)', text) if part]


def normalize_table_math(text: str) -> str:
    """Protect formula pipes before Markdown table parsing discards extra cells."""
    lines = source_lines(text)
    for token in markdown_parser().parse(text):
        if token.type != 'table_open' or token.map is None:
            continue
        start, end = token.map
        for index in range(start, end):
            lines[index] = replace_math(lines[index], lambda span: span.delimiter
                + re.sub(r'(?<!\\)\|', r'\\|', span.content) + span.delimiter)
    return ''.join(lines)


# Signature-based repair of one known transport corruption: a JSON string
# writer that emitted "\f", "\b", "\t", "\n" ... instead of "\\frac", "\\bar",
# "\\text", "\\nabla", so the decoder consumed the backslash and its first
# letter as a control character. This is NOT general unescaping: only the exact
# command signatures below, only inside a dollar-math span, never in prose,
# fenced/indented code, inline code or literal HTML. Unknown damage still fails.
_ESCAPE_SIGNATURES = {
    chr(7) + 'pprox': r'\approx',
    chr(8) + 'egin': r'\begin',
    chr(8) + 'ar': r'\bar',
    chr(12) + 'rac': r'\frac',
    chr(9) + 'imes': r'\times',
    chr(9) + 'ext': r'\text',
    chr(9) + 'heta': r'\theta',
    chr(9) + 'au': r'\tau',
    chr(9) + 'o': r'\to',
    chr(11) + 'ec': r'\vec',
}
_DAMAGED_ESCAPE = re.compile('|'.join(re.escape(key) + r'(?![A-Za-z])' for key in _ESCAPE_SIGNATURES))

# "\n" and "\r" decode to legitimate line breaks, so these signatures are only
# damage when the break sits inside what is otherwise ONE single-line formula:
# an inline $...$ (which may never cross a line) or a $$...$$ whose every break
# is such a signature. A display block written over several lines (closing $$
# on its own line) is left untouched even if a line starts with "e^x" or
# "right". Single-letter commands exclude the prose shapes "e.g.", "i.e.", "i)".
_BREAK_SIGNATURES = {
    '\n': ('abla', 'exists', 'ewline', 'leq', 'geq', 'mid', 'eq', 'eg', 'ot', 'e', 'i', 'u'),
    '\r': ('ightleftharpoons', 'ightarrow', 'ight', 'angle', 'floor', 'ceil', 'brace',
           'brack', 'vert', 'ho', 'm'),
}
_LINE_BREAK_DAMAGE = re.compile('|'.join(
    re.escape(char) + '(' + '|'.join(
        re.escape(name) + (r'(?![A-Za-z.)])' if len(name) == 1 else r'(?![A-Za-z])')
        for name in sorted(names, key=len, reverse=True)) + ')'
    for char, names in _BREAK_SIGNATURES.items()))


def _noncode_ranges(text: str):
    lines = source_lines(text)
    protected = set()
    for token in markdown_parser().parse(text):
        if token.type in {'fence', 'code_block'} and token.map:
            protected.update(range(*token.map))
    start = 0
    while start < len(lines):
        literal = start in protected
        end = start + 1
        while end < len(lines) and (end in protected) == literal:
            end += 1
        yield literal, ''.join(lines[start:end])
        start = end


def _line_break_damage(chunk: str) -> tuple[str, int]:
    """Repair break signatures inside otherwise single-line formulas.

    Returns the repaired chunk and the number of repaired signatures. Candidate
    breaks are first replaced by a marker absent from the chunk so a broken
    inline formula is seen as one span; spans that still contain a real line
    break, a dollar sign or an unrepaired candidate keep the original bytes.
    """
    found = list(_LINE_BREAK_DAMAGE.finditer(chunk))
    if not found:
        return chunk, 0
    marker = next(chr(code) for code in range(0xE000, 0xF900) if chr(code) not in chunk)
    marked = _LINE_BREAK_DAMAGE.sub(marker, chunk)
    damaged: set[int] = set()
    positions = [index for index, char in enumerate(marked) if char == marker]
    for span in math_spans(marked):
        content = span.content
        if marker not in content or '$' in content or '\r' in content or '\n' in content:
            continue
        body_start = span.start + len(span.delimiter)
        body_end = span.end - len(span.delimiter)
        damaged.update(number for number, position in enumerate(positions)
                       if body_start <= position < body_end)
    if not damaged:
        return chunk, 0
    pieces, previous = [], 0
    for number, match in enumerate(found):
        pieces.append(chunk[previous:match.start()])
        pieces.append('\\' + {'\n': 'n', '\r': 'r'}[match.group()[0]] + match.group()[1:]
                      if number in damaged else match.group())
        previous = match.end()
    pieces.append(chunk[previous:])
    return ''.join(pieces), len(damaged)


def math_diagnostics(text: str) -> tuple[str, ...]:
    """Reject corrupt math, without claiming semantic validation or guessing it."""
    chunks = [chunk for literal, chunk in _noncode_ranges(text) if not literal]
    prose = ''.join(chunks)
    problems = []
    # Disallowed control bytes are output-integrity failures even in code or
    # quotations. Literal demonstrations must use escaped text such as \\x07,
    # not the control character itself. Arithmetic exclusions are separate.
    if any((ord(char) < 32 and char not in '\t\r\n') or ord(char) == 127 for char in text):
        problems.append('讲义含异常控制字符，可能是 LaTeX 反斜杠被错误转义；请恢复原命令后再导出')
    if (any(_DAMAGED_ESCAPE.search(span.content) for span in math_spans(prose, allow_space=True))
            or any(_line_break_damage(chunk)[1] for chunk in chunks)):
        problems.append('公式含疑似被字符串转义吞掉的 LaTeX 命令；普通缩进不会自动改写')
    return tuple(problems)


def repair_math_escapes(text: str) -> str:
    """Opt-in signature recovery; unknown corruption still fails diagnostics.

    No global unescaping, tab replacement, prose correction or fenced-code edits.
    """
    def repair(chunk: str) -> str:
        chunk = _line_break_damage(chunk)[0]
        return replace_math(chunk, lambda span: span.delimiter
            + _DAMAGED_ESCAPE.sub(lambda m: _ESCAPE_SIGNATURES[m.group()], span.content)
            + span.delimiter, allow_space=True)
    return ''.join(chunk if literal else repair(chunk) for literal, chunk in _noncode_ranges(text))
