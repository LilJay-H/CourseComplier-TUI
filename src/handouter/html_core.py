"""Portable, offline HTML notes with embedded slide images and native MathML."""

from __future__ import annotations

import base64
import json
from html import escape, unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from .safe_io_core import read_bytes, safe_path
from .resources import HTML_TAGS, HTML_ATTRS, markdown_parser, check_resources, image_resource
from .math_markup import math_diagnostics, math_spans, normalize_table_math, replace_math


STYLE = """
body{font-family:system-ui,sans-serif;line-height:1.8;color:#243044;background:#f5f7fa;margin:0}
main{max-width:920px;margin:30px auto;padding:36px;background:white;border-radius:12px}
h1,h2,h3{line-height:1.35;color:#152844}h2{border-bottom:1px solid #dde3ec;padding-bottom:12px}
img{display:block;max-width:100%;height:auto;margin:20px auto}pre{overflow:auto;padding:16px;background:#f1f4f8}
code{font-family:ui-monospace,monospace}table{border-collapse:collapse;display:block;overflow:auto}
th,td{border:1px solid #dce2eb;padding:8px 12px}blockquote{margin:20px 0;padding:12px 20px;border-left:5px solid #d49b21;background:#fff8e5}
details{margin:14px 0;padding:10px 16px;background:#f1f4f8;border-radius:6px}summary{cursor:pointer;font-weight:600}
.math.block{display:block;overflow-x:auto;padding:12px 0}math[display=block]{margin:12px 0}a{color:#2263ad}
.ppt-figure{display:block;box-sizing:border-box;width:100%;max-width:100%;min-width:0;margin:20px auto;break-inside:avoid}
.ppt-full{width:100%}.ppt-half{width:50%;min-width:min(100%,280px)}.ppt-third{width:33.333333%;min-width:min(100%,260px)}
.ppt-figure img{display:block;max-width:100%;height:auto;margin:0 auto}
.ppt-figure figcaption{margin:8px 0 0;text-align:center;line-height:1.5;font-size:.92em;overflow-wrap:anywhere}
.ppt-grid,.ppt-gallery{display:grid;gap:20px;align-items:start;max-width:100%;margin:20px auto}
.ppt-cols-2,.ppt-grid-2x2,.ppt-gallery-2{grid-template-columns:repeat(2,minmax(0,1fr))}
.ppt-cols-3,.ppt-gallery-3{grid-template-columns:repeat(3,minmax(0,1fr))}
.ppt-grid>.ppt-figure,.ppt-gallery>.ppt-figure{width:100%;min-width:0;margin:0}
@media(max-width:940px){.ppt-cols-3,.ppt-gallery-3{grid-template-columns:1fr}}
@media(max-width:720px){.ppt-cols-2,.ppt-cols-3,.ppt-grid-2x2,.ppt-gallery-2,.ppt-gallery-3{grid-template-columns:1fr}.ppt-half,.ppt-third{width:100%}}
@media(max-width:600px){main{margin:0;padding:20px;border-radius:0}}
@media(prefers-color-scheme:dark){
body{color:#e2e8f0;background:#0f172a}main{background:#1e293b}
h1,h2,h3{color:#f8fafc}h2{border-bottom:1px solid #334155}
pre{background:#0f172a}code{background:#334155;color:#f1f5f9;padding:2px 4px;border-radius:4px}
pre code{background:transparent;padding:0}th,td{border:1px solid #334155}
blockquote{border-left:5px solid #d97706;background:#292524;color:#fde68a}
details{background:#0f172a}summary{color:#f1f5f9}a{color:#60a5fa}
.ppt-figure figcaption{color:#94a3b8}
}
:root[data-theme=dark] body,body[data-theme=dark]{color:#e2e8f0;background:#0f172a}
:root[data-theme=dark] main,body[data-theme=dark] main{background:#1e293b}
:root[data-theme=dark] h1,:root[data-theme=dark] h2,:root[data-theme=dark] h3,body[data-theme=dark] h1,body[data-theme=dark] h2,body[data-theme=dark] h3{color:#f8fafc}
:root[data-theme=dark] h2,body[data-theme=dark] h2{border-bottom:1px solid #334155}
:root[data-theme=dark] pre,body[data-theme=dark] pre{background:#0f172a}
:root[data-theme=dark] code,body[data-theme=dark] code{background:#334155;color:#f1f5f9;padding:2px 4px;border-radius:4px}
:root[data-theme=dark] pre code,body[data-theme=dark] pre code{background:transparent;padding:0}
:root[data-theme=dark] th,:root[data-theme=dark] td,body[data-theme=dark] th,body[data-theme=dark] td{border:1px solid #334155}
:root[data-theme=dark] blockquote,body[data-theme=dark] blockquote{border-left:5px solid #d97706;background:#292524;color:#fde68a}
:root[data-theme=dark] details,body[data-theme=dark] details{background:#0f172a}
:root[data-theme=dark] summary,body[data-theme=dark] summary{color:#f1f5f9}
:root[data-theme=dark] a,body[data-theme=dark] a{color:#60a5fa}
:root[data-theme=dark] .ppt-figure figcaption,body[data-theme=dark] .ppt-figure figcaption{color:#94a3b8}
@media print{body{background:white!important;color:#243044!important}main{margin:0;padding:0;background:white!important}details{break-inside:avoid}.ppt-grid,.ppt-gallery{grid-template-columns:1fr}.ppt-half,.ppt-third{width:100%}}
"""


class SafeHTML(HTMLParser):
    TAGS = HTML_TAGS
    ATTRS = HTML_ATTRS

    def __init__(self, note: Path, root: Path, options: dict | None = None, *, write=None):
        super().__init__(convert_charrefs=True)
        self.note, self.root = note, root
        self.parts: list[str] = []
        self.write = write or self.parts.append
        self.options = options

    def image(self, src: str) -> str:
        relative = image_resource(self.root, self.note, src, options=self.options, offline=True)
        path = safe_path(self.root, relative)
        if path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("单张图片超过 20 MiB，无法导出")
        data = read_bytes(self.root, relative)
        mime = None
        if data.startswith(b'\x89PNG\r\n\x1a\n'):
            mime = 'image/png'
        elif data.startswith(b'\xff\xd8\xff'):
            mime = 'image/jpeg'
        elif data.startswith((b'GIF87a', b'GIF89a')):
            mime = 'image/gif'
        elif data.startswith(b'RIFF') and data[8:12] == b'WEBP':
            mime = 'image/webp'
        if mime is None:
            raise ValueError("HTML 图片仅支持 PNG/JPEG/GIF/WebP")
        return f"data:{mime};base64," + base64.b64encode(data).decode('ascii')

    def handle_starttag(self, tag, attrs):
        if tag not in self.TAGS:
            return
        clean = []
        for key, value in attrs:
            value = value or ''
            if tag == 'img' and key == 'src':
                clean.append(('src', self.image(value)))
            elif tag == 'img' and key == 'alt':
                clean.append((key, value))
            elif tag == 'a' and key == 'href':
                if value.startswith('#') or urlsplit(value).scheme in {'https', 'http', 'mailto'}:
                    clean.append((key, value))
            elif key in self.ATTRS and not (tag == 'details' and key == 'open'):
                clean.append((key, value))
        self.write('<' + tag + ''.join(f' {k}="{escape(v, quote=True)}"' for k, v in clean) + '>')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in {'img', 'br', 'hr'}:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self.TAGS and tag not in {'img', 'br', 'hr'}:
            self.write(f'</{tag}>')

    def handle_data(self, data):
        self.write(escape(data))


def render_note(note: Path, root: Path, theme: str | None = None, *, math_warnings=False,
                output_stream=None) -> str | None:
    try:
        from mdit_py_plugins.dollarmath import dollarmath_plugin
        from latex2mathml.converter import convert
    except ImportError as exc:
        raise RuntimeError('HTML 导出依赖未安装，请重新运行 setup-handouter 安装脚本') from exc

    def math_renderer(content, options):
        content = (content or '').strip()
        if not content:
            return ''
        try:
            return convert(content, display='block' if options.get('display_mode') else 'inline')
        except Exception as exc:
            if math_warnings:
                return '<code>公式未转换：' + escape(content) + '</code>'
            raise ValueError(f'公式无法转换，请检查 LaTeX：{content[:100]}') from exc

    text = read_bytes(root, note.relative_to(root).as_posix()).decode('utf-8-sig')
    issues = math_diagnostics(text)
    if issues and not math_warnings:
        raise ValueError('；'.join(issues))
    # Protect pipes while the original table row still contains every cell.
    text = normalize_table_math(text)
    options = None
    if (root / 'handoff/sources.json').exists():
        options = json.loads(read_bytes(root, 'handoff/sources.json')).get('options', {})
    resources = check_resources(text, root, note, options=options, offline=True)
    if resources.errors:
        raise ValueError('；'.join(resources.errors))
    md = markdown_parser()
    md.use(dollarmath_plugin, renderer=math_renderer, double_inline=True,
           allow_space=False, allow_digits=False)
    def render_html_math(self, tokens, index, options, env):
        # Raw HTML is not passed through Markdown's math rule. Scan text only:
        # never transform attributes, comments, pre/code or existing MathML.
        # Convert before HTMLParser so a TeX '<' cannot become an HTML tag.
        def replacement(span):
            mode = 'block' if span.display else 'inline'
            value = math_renderer(unescape(span.content), {'display_mode': span.display})
            return f'<span class="math {mode}">{value}</span>'
        return replace_math(tokens[index].content, replacement)

    md.add_render_rule('html_block', render_html_math)
    parser = SafeHTML(note, root, options, write=output_stream.write if output_stream else None)
    rendered = md.render(text)
    if not math_warnings and next(math_spans(rendered), None) is not None:
        raise ValueError('HTML 仍含未转换的数学公式；未发布不完整的阅读版')
    rendered = rendered.replace('<p>[!IMPORTANT]', '<p><strong>重点提醒</strong>')
    html_attrs = ' lang="zh-CN"'
    if theme == 'dark' or (options and (options.get('theme') == 'dark' or options.get('html_theme') == 'dark')):
        html_attrs += ' data-theme="dark"'
    header = ('<!doctype html><html' + html_attrs + '><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
            'img-src data:; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">'
            f'<title>{escape(note.stem)}</title><style>{STYLE}</style></head><body><main>')
    if output_stream:
        output_stream.write(header)
    parser.feed(rendered)
    parser.close()
    footer = '</main></body></html>'
    if output_stream:
        output_stream.write(footer)
        return None
    return header + ''.join(parser.parts) + footer
