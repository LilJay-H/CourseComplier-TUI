"""Export a class-independent PPT layout copy for Typora, never edit the source.

Canonical and legacy PPT layouts and mathematical details are adapted.
Unrelated prose, plain folds and code examples retain their original text.
This is layout adaptation, not content verification or a portable single file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser
import json
import re
from pathlib import Path

from .resources import check_resources
from .safe_io import publish_bytes, read_bytes, safe_path
from .typora_math import expand_math_details, literal_ranges, prepare_math_copy
from .math_markup import math_spans, normalize_table_math


@dataclass
class _Node:
    tag: str
    attrs: dict
    children: list = field(default_factory=list)


def _caption(node) -> str:
    if isinstance(node, str):
        return escape(node)
    if node.tag not in {'strong', 'em', 'code', 'span', 'br', 'small', 's', 'del'}:
        raise ValueError('图注包含不支持的结构，未改写原文件')
    body = ''.join(_caption(child) for child in node.children)
    if node.tag == 'br':
        return '<br>'
    return f'<{node.tag}>{body}</{node.tag}>'


def _elements(node) -> list:
    if any(isinstance(child, str) and child.strip() for child in node.children):
        raise ValueError('图块混入正文，无法安全转换；请先分开图片和讲解')
    return [child for child in node.children if isinstance(child, _Node)]


def _figure(node: _Node, width: str) -> str:
    children = _elements(node)
    images = [child for child in children if child.tag == 'img']
    captions = [child for child in children if child.tag == 'figcaption']
    if len(images) != 1 or len(captions) > 1 or len(children) != len(images) + len(captions):
        raise ValueError('仅转换一图一图注的规范 figure；未丢弃或猜测复杂内容')
    attrs = images[0].attrs
    if not attrs.get('src'):
        raise ValueError('图块缺少图片来源')
    image = '<img src="{}" alt="{}" width="{}">'.format(
        escape(attrs['src'], quote=True), escape(attrs.get('alt') or '', quote=True), width)
    caption = ''.join(_caption(child) for child in captions[0].children) if captions else ''
    return image + ('<br><small>' + caption + '</small>' if caption else '')


def _convert(node: _Node) -> str:
    classes = set((node.attrs.get('class') or '').split())
    if node.tag == 'figure':
        width = '50%' if 'ppt-half' in classes else '33.333333%' if 'ppt-third' in classes else '100%'
        body = _figure(node, width)
        if next(math_spans(body), None) is not None:
            # A mathematical caption cannot stay inside Typora's raw HTML
            # paragraph either. Keep the image sizing, but expose caption math
            # as ordinary Markdown; _figure owns this exact separator.
            image, caption = body.split('<br><small>', 1)
            return '<p align="center">' + image + '</p>\n\n' + caption.removesuffix('</small>')
        return '<p align="center">' + body + '</p>'
    figures = _elements(node)
    choices = classes & {'ppt-cols-2', 'ppt-cols-3', 'ppt-grid-2x2', 'ppt-gallery-2', 'ppt-gallery-3'}
    if len(choices) != 1:
        raise ValueError('图组缺少唯一的布局类型')
    kind = choices.pop()
    columns, count = (3, 3) if kind in {'ppt-cols-3', 'ppt-gallery-3'} else (2, 4) if kind == 'ppt-grid-2x2' else (2, 2)
    if len(figures) != count or any(child.tag != 'figure' for child in figures):
        raise ValueError('图组数量/结构与布局不一致，未改写原文件')
    rows = []
    cell_width = '50%' if columns == 2 else '33.333333%'
    for start in range(0, len(figures), columns):
        cells = ''.join(f'<td width="{cell_width}" align="center" valign="top">'
                        + _figure(child, '100%') + '</td>' for child in figures[start:start + columns])
        rows.append('<tr>' + cells + '</tr>')
    return '<table width="100%"><tbody>' + ''.join(rows) + '</tbody></table>'


class _FigureBlocks(HTMLParser):
    VOID = {'img', 'br', 'hr', 'input', 'meta', 'link', 'source', 'wbr'}

    def __init__(self, source: str):
        super().__init__(convert_charrefs=True)
        self.source = source
        # HTMLParser's getpos counts LF only, not all Unicode splitlines.
        self.offsets = [0, *(match.end() for match in re.finditer('\n', source))]
        self.nodes = []
        self.start = None
        self.replacements = []
        self.literal = 0
        self.protected = literal_ranges(source)

    def source_offset(self):
        line, column = self.getpos()
        return self.offsets[line - 1] + column

    def handle_starttag(self, tag, attrs):
        if any(a <= self.source_offset() < b for a, b in self.protected) and not self.nodes:
            return
        if not self.nodes and tag in {'pre', 'code'}:
            self.literal += 1
        if self.literal:
            return
        attrs = dict(attrs)
        classes = set((attrs.get('class') or '').split())
        begins = (tag == 'figure' and 'ppt-figure' in classes) or (tag == 'div' and bool(classes & {'ppt-grid', 'ppt-gallery'}))
        if not self.nodes and not begins:
            return
        node = _Node(tag, attrs)
        if self.nodes:
            self.nodes[-1].children.append(node)
        else:
            self.start = self.source_offset()
        if tag not in self.VOID:
            self.nodes.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if any(a <= self.source_offset() < b for a, b in self.protected) and not self.nodes:
            return
        if self.literal:
            if tag in {'pre', 'code'}:
                self.literal -= 1
            return
        if not self.nodes:
            return
        if self.nodes[-1].tag != tag:
            raise ValueError('图块标签不闭合，未改写原文件')
        node = self.nodes.pop()
        if not self.nodes:
            end = self.source.find('>', self.source_offset()) + 1
            self.replacements.append((self.start, end, _convert(node)))

    def handle_data(self, data):
        if self.nodes:
            self.nodes[-1].children.append(data)

    def close(self):
        super().close()
        if self.nodes:
            raise ValueError('图块不完整，未改写原文件')


def typora_markdown(text: str) -> str:
    """Adapt complete HTML regions across blank lines; preserve code examples."""
    parser = _FigureBlocks(text)
    parser.feed(text)
    parser.close()
    for start, end, replacement in reversed(parser.replacements):
        text = text[:start] + replacement + text[end:]
    return expand_math_details(text)


def export_typora(source: str | Path, *, repair_escapes: bool = False) -> Path:
    """Publish a sibling .typora.md; relative images retain their original base."""
    source = Path(source).expanduser().absolute()
    if source.suffix.lower() != '.md':
        raise ValueError('请选择一个 Markdown 文件')
    lexical_root = source.parent.parent if source.parent.name == 'notes' else source.parent
    relative = source.relative_to(lexical_root).as_posix()
    root = lexical_root.resolve(strict=True)
    note = safe_path(root, relative)
    text = read_bytes(root, relative).decode('utf-8-sig')
    text = prepare_math_copy(text, repair_escapes=repair_escapes)
    options = None
    if (root / 'handoff/sources.json').exists():
        options = json.loads(read_bytes(root, 'handoff/sources.json')).get('options', {})
    report = check_resources(text, root, note, options=options, offline=True)
    if report.errors:
        raise ValueError('；'.join(report.errors))
    converted = typora_markdown(normalize_table_math(text))
    prepare_math_copy(converted)
    report = check_resources(converted, root, note, options=options, offline=True)
    if report.errors:
        raise ValueError('；'.join(report.errors))
    for number in range(10000):
        suffix = '.typora.md' if number == 0 else f'.typora-{number:03d}.md'
        target = note.with_name(note.stem + suffix)
        try:
            return publish_bytes(root, target.relative_to(root).as_posix(), converted.encode('utf-8'))
        except FileExistsError:
            continue
    raise RuntimeError('Typora 阅读副本版本过多')
