"""Shared extractive quotation checks, independent of authoring/Agent runners."""
from __future__ import annotations
from html.parser import HTMLParser
import re
from .resources import markdown_parser
from .source_refs import resolve_ref, SourceRefError

QUOTE_POLICY = 'source-excerpt-v1'
QUOTE_DIAGNOSTIC_CODES = frozenset({
    'quote_markup_unreliable', 'quote_source_missing', 'quote_source_invalid',
    'quote_source_identity_invalid', 'quote_range_invalid', 'quote_text_mismatch',
    'quote_policy_invalid',
})


class _Quotes(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.blocks = [], []

    def handle_starttag(self, tag, attrs):
        pairs = list(attrs)
        attrs = dict(pairs)
        if tag == 'details':
            if self.stack:
                self.stack[-1]['nested'] = True
            self.stack.append({'marked': 'complete-verbatim' in (attrs.get('class') or '').split(),
                               'title': '', 'meta': '', 'quotes': [], 'summary': False,
                               'quote_depth': 0, 'nested': False, 'unreliable': False,
                               'formatting_difference': False})
            return
        if not self.stack:
            return
        block = self.stack[-1]
        if tag == 'summary':
            if block['summary']:
                block['unreliable'] = True
            block['summary'] = True
        elif tag == 'blockquote':
            if block['quote_depth']:
                block['unreliable'] = True
            block['quote_depth'] += 1
            block['quotes'].append('')
        elif tag in {'span', 'div'}:
            # Passive wrappers are safe only after the shared resource policy
            # validates their complete tag/attribute set and the visible text
            # passes the same source-range comparison below.
            if any(key != 'class' for key, _value in pairs) or len(pairs) != len(attrs):
                block['unreliable'] = True
            else:
                block['formatting_difference'] = True
            if tag == 'div':
                self.handle_data('\n')
        elif tag not in {'p', 'br', 'strong', 'em', 'code'}:
            block['unreliable'] = True
        if tag == 'br':
            self.handle_data('\n')

    def handle_endtag(self, tag):
        if not self.stack:
            return
        block = self.stack[-1]
        if tag == 'summary':
            block['summary'] = False
        elif tag == 'blockquote':
            block['quote_depth'] = max(0, block['quote_depth'] - 1)
        elif tag == 'p':
            self.handle_data('\n')
        elif tag == 'div':
            self.handle_data('\n')
        elif tag == 'details':
            if block['quote_depth'] or block['summary']:
                block['unreliable'] = True
            self.stack.pop()
            if self._is_quote(block):
                self.blocks.append(block)

    def handle_data(self, data):
        if not self.stack:
            return
        block = self.stack[-1]
        if block['summary']:
            block['title'] += data
        elif block['quote_depth']:
            block['quotes'][-1] += data
        else:
            block['meta'] += data

    @staticmethod
    def _is_quote(block):
        return block['marked'] or bool(re.match(r'\s*(?:清晰逐字记录|原转写节选|逐字记录)', block['title']))

    def close(self):
        super().close()
        for block in self.stack:
            if self._is_quote(block):
                block['unreliable'] = True
                self.blocks.append(block)
        self.stack.clear()


def _source_failure_code(exc):
    code = getattr(exc, 'code', None)
    if code == 'SOURCE_MISSING':
        return 'quote_source_missing'
    if code == 'REF_OUT_OF_RANGE':
        return 'quote_range_invalid'
    if code == 'SOURCE_IDENTITY_INVALID':
        return 'quote_source_identity_invalid'
    if code in {'SOURCE_UNSAFE', 'SOURCE_INVALID', 'SOURCE_CHANGED', 'REF_INVALID',
                'REF_KIND_UNSUPPORTED', 'REF_AMBIGUOUS'}:
        return 'quote_source_invalid'
    if isinstance(exc, KeyError):
        return 'quote_source_missing'
    if isinstance(exc, (FileNotFoundError,)):
        return 'quote_source_missing'
    return 'quote_source_invalid'


# Exactly one transcript range opens the meta paragraph. A short same-line
# annotation may follow after a separator, e.g. "（约 05:30）" or "，课件第3页",
# but it can never name another transcript token or line range. This only
# relaxes the source label; the quoted text comparison below is unchanged.
_SOURCE_META = re.compile(
    r'\s*来源\s*[:：]\s*transcript:L([1-9][0-9]{0,8})-L([1-9][0-9]{0,8})'
    r'(?:[^\S\r\n]*[（(，,；;、·|—–][^\r\n]*|[^\S\r\n]+[^\r\n]*)?\s*')
# Any further transcript token or L<number> line reference makes the label
# ambiguous, so it is rejected rather than guessed.
_SECOND_RANGE = re.compile(r'transcript\s*:|(?<![A-Za-z])L\s*[0-9]', re.I)


def _source_meta(meta):
    match = _SOURCE_META.fullmatch(meta)
    if not match or _SECOND_RANGE.search(meta, match.end(2)):
        return None
    return match


def check_extract_quotes_detailed(text, root, sources):
    """Return errors, warnings, and fixed private diagnostic codes.

    Text in a Complete verbatim block is never rewritten. Only passive span/div
    wrappers may differ, and they become warnings only after a resolvable source,
    valid closed line range, and ordered whitespace-normalized exact excerpt match.
    New signed tasks fail closed; an old task with no declared quote policy keeps
    its established warning-only compatibility behavior.
    """
    checks = sources.get('output_checks') or {}
    if not isinstance(checks, dict):
        return ['无效的 output_checks 配置；请刷新交接任务'], [], ['quote_policy_invalid']
    policy = checks.get('complete_quotes')
    if policy not in (None, QUOTE_POLICY):
        return ['不支持此逐字来源核验规则；请刷新交接任务'], [], ['quote_policy_invalid']
    strict = policy == QUOTE_POLICY
    parser = _Quotes()
    try:
        parser.feed(markdown_parser().render(text))
        parser.close()
    except (AssertionError, RecursionError, ValueError):
        return ['无法可靠读取 Complete 逐字引用结构'], [], ['quote_markup_unreliable']
    if not parser.blocks:
        return [], [], []
    errors, warnings, diagnostics = [], [], []
    if sources.get('transcript', {}).get('origin') == 'generated_note':
        return ['生成讲义不是原始转写，不能提供 Complete 逐字层'], warnings, ['quote_source_identity_invalid']
    try:
        resolve_ref(root, sources, 'transcript:L1-L1', require_transcript=True)
    except (KeyError, ValueError, OSError, UnicodeError) as exc:
        code = _source_failure_code(exc)
        target = errors if strict else warnings
        target.append('无法读取本次转写以核对 Complete 逐字层；不代表原话已验证')
        if strict:
            diagnostics.append(code)
        return errors, warnings, diagnostics
    for number, block in enumerate(parser.blocks, 1):
        prefix = f'Complete 逐字层 {number}：'
        match = _source_meta(block['meta'])
        if block['nested'] or block['unreliable'] or not block['quotes']:
            target = errors if strict else warnings
            target.append(prefix + '无法可靠提取引用文字；须使用闭合且非嵌套的引用结构')
            if strict:
                diagnostics.append('quote_markup_unreliable')
            continue
        if not match:
            target = errors if strict else warnings
            target.append(prefix + '缺少可核验的独立转写来源行')
            if strict:
                diagnostics.append('quote_source_missing')
            continue
        start, end = map(int, match.groups())
        try:
            resolved = resolve_ref(root, sources, f'transcript:L{start}-L{end}', require_transcript=True)
        except SourceRefError as exc:
            code = _source_failure_code(exc)
            target = errors if strict else warnings
            target.append(prefix + ('转写行范围无效' if code == 'quote_range_invalid'
                                    else '转写来源缺失或无法安全读取'))
            if strict:
                diagnostics.append(code)
            continue
        original, cursor = ''.join(resolved.text.split()), 0
        matched = True
        for quote in block['quotes']:
            candidate = ''.join(quote.split())
            position = original.find(candidate, cursor) if candidate else -1
            if position < 0:
                target = errors if strict else warnings
                target.append(prefix + '文字不是所标行范围内顺序一致的原文节选')
                if strict:
                    diagnostics.append('quote_text_mismatch')
                matched = False
                break
            cursor = position + len(candidate)
        if matched and block['formatting_difference']:
            warnings.append(prefix + '存在被动 HTML 排版包装；引用范围与空白归一后的原文顺序已核验')
    if len(errors) > 20:
        errors[:] = errors[:20] + ['逐字层问题较多；请逐块回源，不输出原文或敏感字段到日志']
        diagnostics[:] = diagnostics[:20]
    warnings.append('逐字核对只检查节选字符与来源范围；不认证音频、语义完整性、标题或全稿事实')
    return errors, warnings, list(dict.fromkeys(diagnostics))


def check_extract_quotes(text, root, sources):
    """Backward-compatible two-list API; detailed codes are private metadata."""
    errors, warnings, _codes = check_extract_quotes_detailed(text, root, sources)
    return errors, warnings
