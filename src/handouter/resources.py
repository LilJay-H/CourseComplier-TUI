"""One Markdown parser and resource policy for validation and offline HTML."""
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from .safe_io_core import read_bytes, resource_path


HTML_TAGS = set('p br hr h1 h2 h3 h4 h5 h6 ul ol li strong em s del blockquote pre code table thead tbody tr th td details summary figure figcaption div span a img math mrow mi mn mo mtext mspace ms mfrac msqrt mroot mstyle merror mpadded mphantom mfenced menclose msub msup msubsup munder mover munderover mmultiscripts mprescripts none mtable mtr mtd mlabeledtr semantics'.split())
HTML_ATTRS = set('class display xmlns mathvariant stretchy fence separator accent accentunder columnalign rowalign columnspacing rowspacing columnspan rowspan linethickness notation width height depth lspace rspace scriptlevel displaystyle open close separators encoding start'.split())
_TEMPLATE_FIELDS = {
    'skill_entry', 'skill_modules_one_per_line', 'course_title_and_lecture', 'clean_or_traceable',
    'true_or_false', 'true_or_false_default_false', 'expected_outputs_one_per_line',
    'transcript_path', 'segments_path_or_none', 'slides_index_path_or_none',
    'alignment_path_or_none', 'slides_directory_or_none', 'limitations_or_none',
}
_PLACEHOLDER = re.compile(r'\{\{\s*(' + '|'.join(sorted(_TEMPLATE_FIELDS)) + r')\s*\}\}')

# HTML5 permits these end tags to be omitted in their documented contexts.
# Keep this list deliberately small: all other container tags still require
# explicit, correctly ordered closing tags in generated output.
_P_BLOCK_START = {
    'address', 'article', 'aside', 'blockquote', 'div', 'dl', 'fieldset',
    'footer', 'form', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'header',
    'details', 'figure', 'hr', 'main', 'nav', 'ol', 'p', 'pre', 'section', 'table', 'ul',
}
_CELL_CLOSE_START = {'td', 'th', 'tr', 'thead', 'tbody', 'tfoot'}
_TR_CLOSE_START = {'tr', 'thead', 'tbody', 'tfoot'}
_SECTION_CLOSE_START = {'thead', 'tbody', 'tfoot', 'table'}


def markdown_parser():
    try:
        from markdown_it import MarkdownIt
    except ImportError as exc:
        raise RuntimeError('Markdown 解析依赖 markdown-it-py 未安装，请重新运行安装脚本') from exc
    parser = MarkdownIt('commonmark', {'html': True}).enable('table').enable('strikethrough')
    # Retain unsafe destinations in the token stream so they are rejected
    # explicitly instead of silently becoming literal Markdown text.
    parser.validateLink = lambda _url: True
    return parser


def _safe_link(link: str) -> bool:
    try:
        return urlsplit(unquote(link)).scheme.lower() in {'', 'http', 'https', 'mailto'}
    except ValueError:
        return False


def has_task_placeholders(text: str, *, prompt: bool = False) -> bool:
    if prompt:
        return bool(_PLACEHOLDER.search(text))
    for token in markdown_parser().parse(text):
        if token.type == 'inline':
            teaching = re.search(r'(?i)示例|模板语法|jinja|template example', token.content)
            for child in token.children or []:
                if child.type == 'text' or (child.type == 'code_inline' and not teaching):
                    if _PLACEHOLDER.search(child.content):
                        return True
        elif token.type in {'html_block', 'html_inline'} and _PLACEHOLDER.search(token.content):
            return True
    return False


def has_pending_images(text: str) -> bool:
    pattern = re.compile(r'\[插图位置[^\]]*Slides\.pdf', re.I)
    for token in markdown_parser().parse(text):
        if token.type == 'html_block' and pattern.search(token.content):
            return True
        for child in token.children or []:
            if child.type == 'text' and pattern.search(child.content):
                return True
    return False


@dataclass(frozen=True)
class ResourceReport:
    images: tuple[str, ...]
    errors: tuple[str, ...]


class ResourcePolicyError(ValueError):
    """A fixed diagnostic without untrusted resource paths or values."""


class _HTMLResources(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.images: list[str] = []
        self.errors: list[str] = []
        self.stack: list[str] = []

    def _close_optional_before_start(self, tag):
        while self.stack:
            current = self.stack[-1]
            if current == 'p' and tag in _P_BLOCK_START:
                self.stack.pop()
            elif (current == 'p' and tag in _CELL_CLOSE_START
                    and any(item in {'td', 'th'} for item in self.stack[:-1])):
                # The paragraph end tag may be omitted when its table-cell
                # parent is implicitly closed by the next cell/row boundary.
                self.stack.pop()
            elif current == 'li' and tag == 'li':
                self.stack.pop()
            elif current in {'td', 'th'} and tag in _CELL_CLOSE_START:
                self.stack.pop()
            elif current == 'tr' and tag in _TR_CLOSE_START:
                self.stack.pop()
            elif current in {'thead', 'tbody', 'tfoot'} and tag in _SECTION_CLOSE_START:
                self.stack.pop()
            else:
                break

    def _close_optional_before_end(self, tag):
        # A cell can end implicitly at its row/section/table boundary, and a
        # row can end implicitly at its section/table boundary. List items end
        # implicitly when their list closes. Never auto-close figure/details or
        # arbitrary descendants, so genuine mismatches remain errors.
        while self.stack:
            current = self.stack[-1]
            if current in {'td', 'th'} and tag in {'tr', 'thead', 'tbody', 'tfoot', 'table'}:
                self.stack.pop()
            elif current == 'tr' and tag in {'thead', 'tbody', 'tfoot', 'table'}:
                self.stack.pop()
            elif current in {'thead', 'tbody', 'tfoot'} and tag == 'table':
                self.stack.pop()
            elif current == 'li' and tag in {'ul', 'ol'}:
                self.stack.pop()
            elif current == 'p' and tag in _P_BLOCK_START and tag != 'p':
                self.stack.pop()
            elif (current == 'p' and tag in {'td', 'th', 'tr', 'thead', 'tbody', 'tfoot', 'table'}
                    and any(item in {'td', 'th'} for item in self.stack[:-1])):
                self.stack.pop()
            else:
                break

    def handle_starttag(self, tag, attrs):
        self._close_optional_before_start(tag)
        if tag in HTML_TAGS and tag not in {'img', 'br', 'hr'}:
            if tag == 'figcaption' and (not self.stack or self.stack[-1] != 'figure'):
                self.errors.append('HTML 图片说明必须直接位于 figure 内')
            if tag == 'summary' and (not self.stack or self.stack[-1] != 'details'):
                self.errors.append('HTML summary 必须直接位于 details 内')
            if tag == 'figure' and 'figure' in self.stack:
                self.errors.append('HTML figure 不允许互相嵌套')
            self.stack.append(tag)
        if tag in {'script', 'style', 'iframe', 'frame', 'object', 'embed', 'svg', 'image', 'link', 'base',
                   'meta', 'audio', 'video', 'source', 'track', 'form', 'input', 'button', 'textarea', 'select'}:
            self.errors.append('HTML 含不支持的活动或资源元素')
            return
        names = [key for key, _value in attrs]
        if len(names) != len(set(names)):
            self.errors.append('HTML 含重复属性，无法可靠解释资源')
        for key, value in attrs:
            if key.startswith('on') or key in {'style', 'srcset', 'poster', 'background', 'xlink:href'}:
                self.errors.append('HTML 含不支持的活动属性或资源属性')
            elif key in {'src', 'href'}:
                if tag == 'img' and key == 'src':
                    self.images.append(value or '')
                elif tag == 'a' and key == 'href':
                    if not _safe_link(value or ''):
                        self.errors.append('HTML 链接使用不支持的协议')
                else:
                    self.errors.append('HTML 元素使用不支持的资源属性')
        if tag == 'img' and not any(key == 'src' and value for key, value in attrs):
            self.errors.append('HTML 图片缺少 src')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag in HTML_TAGS and tag not in {'img', 'br', 'hr'}:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag not in HTML_TAGS:
            return
        self._close_optional_before_end(tag)
        if not self.stack or self.stack[-1] != tag:
            self.errors.append('HTML 容器闭合顺序不匹配；请修复原始结构后重新校验')
            return
        self.stack.pop()

    def close(self):
        super().close()
        # A paragraph at the end of an HTML fragment may omit </p>. Other
        # unclosed containers (including lists, rows, details, and figures)
        # remain errors.
        if self.stack and self.stack[-1] == 'p':
            self.stack.pop()
        if self.stack:
            self.errors.append('HTML 存在未闭合容器；未发布不完整阅读版')


def parse_resources(text: str) -> ResourceReport:
    # Use the same preserved table cells as reading exports. Otherwise an image
    # or unsafe link after a formula pipe could disappear from validation/copying.
    from .math_markup import normalize_table_math, replace_math
    from html import escape
    text = normalize_table_math(text)
    # TeX comparisons such as $a<b$ are text, not opening HTML tags.
    # Follow the renderer's math scanner so they cannot swallow a real closer.
    text = replace_math(text, lambda span: escape(span.content))
    html = _HTMLResources()
    images = []
    for token in markdown_parser().parse(text):
        if token.type == 'html_block':
            html.feed(token.content)
        for child in token.children or []:
            if child.type == 'image':
                images.append(child.attrGet('src') or '')
            elif child.type == 'html_inline':
                html.feed(child.content)
            elif child.type == 'link_open':
                if not _safe_link(child.attrGet('href') or ''):
                    html.errors.append('Markdown 链接使用不支持的协议')
    html.close()
    return ResourceReport(tuple(images + html.images), tuple(dict.fromkeys(html.errors)))


def image_resource(root: Path, note: Path, link: str, *, options: dict | None = None, offline: bool = False) -> str | None:
    """Return a safe local relative path, or None for explicitly allowed web images."""
    decoded = unquote(link)
    parsed = urlsplit(decoded)
    if parsed.scheme or parsed.netloc or decoded.startswith(('//', '\\')):
        if not offline and parsed.scheme.lower() in {'http', 'https'} and parsed.netloc and options and options.get('allow_web'):
            return None
        raise ResourcePolicyError('图片远程资源或协议未获允许；离线 HTML 仅接受本地图片')
    literal = urlsplit(link)
    if literal.query or literal.fragment or not decoded:
        raise ResourcePolicyError('本地图片路径为空或带有查询/片段')
    relative = resource_path(root, note, link)
    if relative.startswith('slides/images/') and options is not None:
        if not options.get('use_slides_as_source') or not options.get('embed_slides'):
            raise ResourcePolicyError('配置禁止嵌入或引用 PPT 图片')
    read_bytes(root, relative)  # Same safe opening/regular-file policy as HTML.
    return relative


def check_resources(text: str, root: Path, note: Path, *, options: dict | None = None, offline: bool = False) -> ResourceReport:
    report = parse_resources(text)
    errors = list(report.errors)
    for link in report.images:
        try:
            image_resource(root, note, link, options=options, offline=offline)
        except ResourcePolicyError as exc:
            errors.append(str(exc))
        except (OSError, ValueError):
            # Links may carry secrets. Do not echo them or filesystem exceptions.
            errors.append('图片缺失、路径不安全或不符合 PPT/联网/离线资源配置')
    return ResourceReport(report.images, tuple(dict.fromkeys(errors)))
