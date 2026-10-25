"""Bounded, in-memory evidence helpers; never rewrite or approve a document.

Offsets are Python string offsets (half-open), not UTF-8 byte offsets. Numeric
results apply only to standalone equality lines in the supported grammar.
Source/unknown HTML, quotations, code and non-asserted examples are not assessed;
known author paragraphs and Complete detail layers can supply candidates.
Callers can supply trusted exclusion spans for source/Complete-verbatim layers;
unmarked quotations cannot reliably be identified from text alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from html.parser import HTMLParser
import re


MAX_TEXT_LENGTH = 100_000
MAX_EXPRESSION_LENGTH = 512
MAX_NODES = 128
MAX_DEPTH = 24
MAX_EXPONENT = 12
MAX_INTEGER_BITS = 256
MAX_CANDIDATES = 128


@dataclass(frozen=True)
class ReviewEvidence:
    start: int
    end: int
    status: str
    reason_code: str
    left: Fraction | None = None
    right: Fraction | None = None


@dataclass(frozen=True)
class NumericReview:
    integrity_status: str
    integrity: tuple[ReviewEvidence, ...]
    numeric: tuple[ReviewEvidence, ...]
    advisory: tuple[ReviewEvidence, ...]
    completeness: str


class _Unsupported(ValueError):
    pass


def _bounded(value: Fraction) -> Fraction:
    if max(abs(value.numerator).bit_length(), value.denominator.bit_length()) > MAX_INTEGER_BITS:
        raise _Unsupported("arithmetic_limit")
    return value


class _Arithmetic:
    def __init__(self, expression: str):
        self.tokens = re.findall(r"\d+(?:\.\d+)?|\.\d+|\*\*|[+*/()^\-]|\S", expression)
        self.position = 0
        self.nodes = 0

    def peek(self) -> str:
        return self.tokens[self.position] if self.position < len(self.tokens) else ""

    def take(self) -> str:
        token = self.peek()
        self.position += 1
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise _Unsupported("node_limit")
        return token

    def parse(self, minimum: int = 0, depth: int = 0) -> Fraction:
        if depth > MAX_DEPTH:
            raise _Unsupported("depth_limit")
        token = self.take()
        if token in ("+", "-"):
            value = self.parse(25, depth + 1)
            if token == "-":
                value = -value
        elif token == "(":
            value = self.parse(0, depth + 1)
            if self.take() != ")":
                raise _Unsupported("unsupported_expression")
        elif re.fullmatch(r"\d+(?:\.\d+)?|\.\d+", token):
            if len(token) > 64:
                raise _Unsupported("arithmetic_limit")
            value = _bounded(Fraction(token))
        else:
            raise _Unsupported("unsupported_expression")
        precedence = {"+": 10, "-": 10, "*": 20, "/": 20, "^": 30, "**": 30}
        while self.peek() in precedence and precedence[self.peek()] >= minimum:
            operator = self.take()
            level = precedence[operator]
            operand = self.parse(level if level == 30 else level + 1, depth + 1)
            if operator == "+":
                value += operand
            elif operator == "-":
                value -= operand
            elif operator == "*":
                value *= operand
            elif operator == "/":
                if operand == 0:
                    raise _Unsupported("division_by_zero")
                value /= operand
            else:
                if operand.denominator != 1 or abs(operand.numerator) > MAX_EXPONENT:
                    raise _Unsupported("exponent_limit")
                if value == 0 and operand <= 0:
                    raise _Unsupported("undefined_power")
                value **= operand.numerator
            value = _bounded(value)
        return value


def _latex(expression: str, depth: int = 0) -> str:
    if depth > MAX_DEPTH:
        raise _Unsupported("depth_limit")
    expression = expression.translate(str.maketrans({"×": "*", "÷": "/", "−": "-"}))
    for command, replacement in ((r"\times", "*"), (r"\cdot", "*"), (r"\div", "/"),
                                 (r"\left", ""), (r"\right", "")):
        expression = re.sub(re.escape(command) + r"(?![A-Za-z])", lambda match: replacement, expression)
    result = ""
    position = 0
    while position < len(expression):
        if expression.startswith(r"\frac", position):
            position += 5
            groups = []
            for _ in range(2):
                while position < len(expression) and expression[position].isspace():
                    position += 1
                if position >= len(expression) or expression[position] != "{":
                    raise _Unsupported("unsupported_expression")
                start = position + 1
                nesting = 1
                position += 1
                while position < len(expression) and nesting:
                    nesting += (expression[position] == "{") - (expression[position] == "}")
                    position += 1
                if nesting:
                    raise _Unsupported("unsupported_expression")
                groups.append(_latex(expression[start:position - 1], depth + 1))
            result += "((" + groups[0] + ")/(" + groups[1] + "))"
        else:
            result += expression[position]
            position += 1
        if len(result) > MAX_EXPRESSION_LENGTH * 4:
            raise _Unsupported("expression_limit")
    return result.replace("{", "(").replace("}", ")")


def evaluate_decimal_expression(expression: str) -> Fraction:
    """Return an exact Fraction, or ValueError for unsupported/over-budget math.

    Grammar: decimal literals, unary +/- and binary + - * /, parentheses,
    integer powers bounded to +/-12, Unicode operators, and explicit LaTeX
    frac/times/cdot/div/left/right. No variables, implicit multiplication,
    scientific notation, units, functions or executable Python are accepted.
    """
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise _Unsupported("expression_limit")
    parser = _Arithmetic(_latex(expression))
    value = parser.parse()
    if parser.position != len(parser.tokens):
        raise _Unsupported("unsupported_expression")
    return value


def calculate_decimal_expression(expression: str, *, domain: str) -> dict:
    """Shared local/cloud tool result; verifies an expression, not its premises."""
    if domain != "decimal_rational" or not isinstance(expression, str):
        raise ValueError("EDITORIAL_CALCULATION_DOMAIN_REQUIRED")
    value = evaluate_decimal_expression(expression)
    return {"domain": domain, "numerator": str(value.numerator),
            "denominator": str(value.denominator), "approximate": float(value),
            "method": "bounded_exact_rational",
            "semantic_scope": "expression_only_not_units_bitwidth_or_source_truth"}


_CONTEXT = re.compile(
    r"quote|quotation|verbatim|source|counter[- ]?example|erroneous|incorrect|wrong|"
    r"not asserted|not a claim|false|mod(?:ulo|ular)?\b|boolean|bit[- ]?vector|"
    r"原文|引用|逐字|反例|错误|错例|不成立|不作断言|模运算|布尔|位向量", re.I
)
_DOMAIN = re.compile(r"mod(?:ulo|ular)?\b|boolean|bit[- ]?vector|模运算|布尔|位向量|二进制|八进制|十六进制|binary|octal|hexadecimal|finite[- ]?width|unsigned|signed|有符号|无符号|位宽|截断|溢出|\b\d+[- ]bit\b", re.I)
_DATE = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{4}")


class _AuthorHTML(HTMLParser):
    """Mask markup without moving offsets; protect quotations/unknown HTML.

    Only ordinary paragraph/emphasis tags and the known author detail layer
    are readable. This is evidence extraction, never an HTML safety validator.
    """
    def __init__(self, text):
        super().__init__(convert_charrefs=False)
        self.text = text
        self.view = list(text)
        self.lines = [0]
        self.lines.extend(match.end() for match in re.finditer("\n", text))
        self.stack = []
        self.protected = []
        self.feed(text)
        if self.rawdata:
            self.protected.append((self.position(), len(text)))

    def position(self):
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def mask(self, length):
        start = self.position()
        for index in range(start, min(start + length, len(self.view))):
            if self.view[index] not in "\r\n":
                self.view[index] = " "

    def handle_starttag(self, tag, attrs):
        self.mask(len(self.get_starttag_text()))
        classes = dict(attrs).get("class", "") or ""
        safe = tag in {"p", "span", "strong", "em", "b", "i", "summary"}
        safe |= tag == "details" and classes.split() == ["complete-detail"]
        if _CONTEXT.search(classes):
            safe = False
        if tag not in {"br", "hr", "img", "input", "meta", "link", "wbr"}:
            self.stack.append((tag, not safe))

    def handle_startendtag(self, tag, attrs):
        self.mask(len(self.get_starttag_text()))

    def handle_endtag(self, tag):
        start = self.position()
        end = self.text.find(">", start)
        self.mask(end - start + 1 if end >= start else len(self.view) - start)
        tags = [entry[0] for entry in self.stack]
        if tag in tags:
            self.stack = self.stack[:len(tags) - 1 - tags[::-1].index(tag)]

    def handle_data(self, data):
        if any(protected for _, protected in self.stack):
            start = self.position()
            self.protected.append((start, start + len(data)))

    def handle_comment(self, data):
        self.protected.append((self.position(), self.position() + len(data) + 7))

    def handle_entityref(self, name):
        self.protected.append((self.position(), self.position() + len(name) + 2))

    def handle_charref(self, name):
        self.protected.append((self.position(), self.position() + len(name) + 3))


def review_numeric_output(text: str, *, output_mode: str = "",
                          excluded_spans: tuple[tuple[int, int], ...] = ()) -> NumericReview:
    """Inspect generated text without mutation or semantic approval.

    Only disallowed C0 characters produce hard integrity failures, with
    content-free reason codes. Mismatches are reviewer evidence, not rejection.
    Supported equalities report exact values; everything else is not_assessed.
    Suspicious tab+ext/imes is advisory. Known author paragraphs/detail layers
    may be inspected; quotations and unknown HTML remain unassessed.
    Numeric conventions are scoped to their paragraph/heading, never the
    whole document. This helper does not infer or evaluate nondecimal rules.
    Trusted caller spans can exclude any additional source/verbatim context.
    Text/candidate budgets return partial/not_assessed, never implicit success.
    """
    if len(text) > MAX_TEXT_LENGTH:
        unknown = ReviewEvidence(0, len(text), "not_assessed", "text_limit")
        return NumericReview("not_assessed", (unknown,), (), (), "partial")
    if len(excluded_spans) > MAX_CANDIDATES or any(
        not isinstance(start, int) or not isinstance(end, int) or not 0 <= start <= end <= len(text)
        for start, end in excluded_spans
    ):
        raise ValueError("invalid_excluded_spans")
    integrity = tuple(ReviewEvidence(position, position + 1, "failure", "c0_escape_corruption")
                      for position, char in enumerate(text) if ord(char) < 32 and char not in "\n\r\t")
    advisory = tuple(ReviewEvidence(match.start(), match.end(), "advisory", "suspected_math_escape")
                     for match in re.finditer(r"\t(?:ext|imes)\b", text))
    numeric = []
    html_view = _AuthorHTML(text)
    readable = "".join(html_view.view)
    context_spans = []
    for paragraph in re.finditer(r"[^\r\n]+(?:\r?\n(?![ \t]*\r?\n)[^\r\n]+)*", readable):
        if _CONTEXT.search(paragraph[0]) or _DOMAIN.search(paragraph[0]) or re.search(r"(?m)^\s*>|[\"“”‘’「」『』']", paragraph[0]):
            context_spans.append(paragraph.span())
    fence = ""
    fence_length = 0
    paragraph_excluded = False
    heading_context: list[tuple[int, bool]] = []
    domain_context = False
    offset = 0
    completeness = "complete"
    inline_marker = 0
    context_index = 0
    html_index = 0
    previous_line = ""
    for line in readable.splitlines(keepends=True):
        stripped = line.strip()
        heading = re.match(r"^(#{1,6})\s+", stripped)
        if heading and not fence:
            domain_context = False
            level = len(heading[1])
            heading_context = [entry for entry in heading_context if entry[0] < level]
            heading_context.append((level, bool(_CONTEXT.search(stripped) or _DOMAIN.search(stripped))))
        elif previous_line and re.fullmatch(r"=+|-+", stripped) and not fence:
            domain_context = False
            level = 1 if stripped.startswith("=") else 2
            heading_context = [entry for entry in heading_context if entry[0] < level]
            heading_context.append((level, bool(_CONTEXT.search(previous_line) or _DOMAIN.search(previous_line))))
        fenced = re.match(r"^[ \t]*(?:>[ \t]*)*(?:(?:[-+*]|\d+[.)])[ \t]+)?(`{3,}|~{3,})(.*)$", line)
        code_context = bool(fence or fenced)
        if fenced:
            marker = fenced[1]
            if not fence:
                fence, fence_length = marker[0], len(marker)
            elif marker[0] == fence and len(marker) >= fence_length and not fenced[2].strip():
                fence = ""
        inline_context = bool(inline_marker)
        if not code_context:
            for run in re.finditer(r"`+", line):
                width = len(run[0])
                if inline_marker == width:
                    inline_marker = 0
                elif not inline_marker:
                    inline_marker = width
                inline_context = True
        if not stripped:
            paragraph_excluded = False
        if _CONTEXT.search(line) or _DOMAIN.search(line) or stripped.startswith(">"):
            paragraph_excluded = True
        while html_index < len(html_view.protected) and html_view.protected[html_index][1] <= offset:
            html_index += 1
        html_excluded = (html_index < len(html_view.protected)
                         and html_view.protected[html_index][0] < offset + len(line))
        # An explicit prose convention can govern following display math
        # across a blank line. It stops at the next heading and never excludes
        # earlier assertions or statements in a later independent section.
        if (_DOMAIN.search(line) and not code_context and not html_excluded and not stripped.startswith(">")
                and not any(start < offset + len(line) and end > offset for start, end in excluded_spans)):
            domain_context = True
        while context_index < len(context_spans) and context_spans[context_index][1] <= offset:
            context_index += 1
        contextual = (context_index < len(context_spans)
                      and context_spans[context_index][0] < offset + len(line))
        excluded = (output_mode in {"verbatim", "complete-verbatim", "complete_verbatim"}
                    or domain_context or paragraph_excluded or any(flag for _, flag in heading_context)
                    or html_excluded or code_context or inline_context
                    or (line.startswith(("    ", "\t")) and "<" not in text[offset:offset + len(line)])
                    or any(char in line for char in '`"“”‘’「」『』')
                    or "'" in line
                    or contextual
                    or any(start < offset + len(line) and end > offset for start, end in excluded_spans))
        if "=" in line:
            if len(numeric) >= MAX_CANDIDATES:
                numeric.append(ReviewEvidence(offset, len(text), "not_assessed", "candidate_limit"))
                completeness = "partial"
                break
            start = offset + len(line) - len(line.lstrip())
            end = offset + len(line.rstrip())
            reason = "excluded_context" if excluded else "unsupported_claim"
            candidate = stripped
            for opening, closing in (("$$", "$$"), ("$", "$"), (r"\(", r"\)"), (r"\[", r"\]")):
                if candidate.startswith(opening) and candidate.endswith(closing):
                    candidate = candidate[len(opening):-len(closing)].strip()
                    break
            evidence = ReviewEvidence(start, end, "not_assessed", reason)
            if not excluded and candidate.count("=") == 1 and not _DATE.search(candidate):
                left_expression, right_expression = candidate.split("=")
                try:
                    left = evaluate_decimal_expression(left_expression)
                    right = evaluate_decimal_expression(right_expression)
                    evidence = ReviewEvidence(start, end, "equal" if left == right else "mismatch",
                                              "exact_equality" if left == right else "exact_expression_mismatch", left, right)
                except ValueError as error:
                    evidence = ReviewEvidence(start, end, "not_assessed", str(error))
            numeric.append(evidence)
        offset += len(line)
        previous_line = stripped
    return NumericReview("failure" if integrity else "pass", integrity, tuple(numeric), advisory, completeness)


def signed_integer_range(bitwidth: int) -> tuple[int, int]:
    """Return a supplied signed bitwidth's range; never infer width from prose."""
    if type(bitwidth) is not int or not 1 <= bitwidth <= MAX_INTEGER_BITS:
        raise ValueError("unsupported_bitwidth")
    return -(1 << (bitwidth - 1)), (1 << (bitwidth - 1)) - 1
