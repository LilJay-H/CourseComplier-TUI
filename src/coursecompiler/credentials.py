"""Shared, redacted credential triage; text scanning is not a secrecy guarantee."""
from dataclasses import dataclass
from html import unescape
import re
from urllib.parse import unquote, urlsplit, parse_qsl


@dataclass(frozen=True)
class CredentialReport:
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


_ASSIGNMENT = re.compile(
    r'''(?ix)\b(auth_key|cookie|api[_ -]?key|signature|token|access_token|refresh_token|authorization)
    ["']?\s*[:=]\s*(?:["']([^"'\r\n]*)["']|([^\s&,;`]+))''')
_PLACEHOLDERS = re.compile(
    r'(?i)^(?:[+-]?\d+(?:\.\d+)?|true|false|none|null|undefined|'
    r'your[_-](?:api[_-]?key|token|key|secret)|example|placeholder|redacted|'
    r'<[A-Z_ -]+>|\$\{[A-Z_]+\}|\{\{\s*[A-Z_]+\s*\}\}|x{3,}|\*{3,}|\.\.\.)$')
_SECRET_SHAPE = re.compile(r'(?i)^(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|'
                           r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)$')


def scan_credentials(text: str) -> CredentialReport:
    """Block explicit secrets, allow obvious lessons, flag ambiguous values.

    Inspect fenced code as well as prose, JSON, headers and encoded URLs. Never
    include matched text, secret values or credential-bearing URLs in findings.
    """
    decoded = unquote(unescape(text))
    blocked = bool(re.search(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', decoded))
    uncertain = False
    for match in re.finditer(r'''(?i)https?://[^\s<>"'`]+''', decoded):
        try:
            pairs = parse_qsl(urlsplit(match.group()).query, keep_blank_values=True)
        except ValueError:
            # Malformed URLs still receive the field-based scan below.
            continue
        if any(re.sub(r'[_ -]', '', key.lower()) in {
            'authkey', 'signature', 'token', 'accesstoken', 'refreshtoken', 'apikey', 'cookie',
            'xamzsignature', 'xgoogsignature',
        } and value for key, value in pairs):
            blocked = True
    if re.search(r'(?i)\bauthorization["\']?\s*[:=]\s*["\']?\s*(?:bearer|basic)\s+\S+', decoded):
        blocked = True
    for match in _ASSIGNMENT.finditer(decoded):
        key = re.sub(r'[_ -]', '', match.group(1).lower())
        value = (match.group(2) if match.group(2) is not None else match.group(3)).strip().rstrip('。')
        if not value or _PLACEHOLDERS.fullmatch(value):
            continue
        secret_shape = _SECRET_SHAPE.fullmatch(value) or (
            len(value) >= 20 and bool(re.search('[a-zA-Z]', value)) and bool(re.search('[0-9]', value)))
        explicitly_secret = bool(re.search(r'(?i)(?:^|[-_])secret(?:$|[-_])', value))
        if key in {'authkey', 'cookie', 'signature', 'authorization'} or secret_shape or explicitly_secret:
            blocked = True
        else:
            uncertain = True
    # Common provider key forms are sensitive even without an assignment.
    if re.search(r'\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b', decoded):
        blocked = True
    return CredentialReport(
        ('材料包含高置信度凭证或签名参数；请移除后重试（内容已隐藏）',) if blocked else (),
        ('材料含含义不确定的凭证字段；请人工复核（字段值已隐藏）',) if uncertain else (),
    )
