"""Small, deterministic, task-local Markdown composition. No semantic rewriting."""
from html import escape
from html.parser import HTMLParser
import copy
import hashlib
import re
from urllib.parse import quote, unquote, urlsplit
import posixpath

from .resources import markdown_parser
from .safe_io import read_bytes, role_path
from .source_refs import parse_ref, resolve_ref

POLICY = 'host-composed-excerpt-v1'
INSTRUCTIONS = """# Host composer candidate
Write ordinary Markdown prose. Use only the frozen plan's registered objects.
Markers must occupy a standalone top-level Markdown paragraph, with blank lines
before and after: [[excerpt:ID]], [[figure:ID]], [[source:ID]], [[details:ID]],
[[/details:ID]]. Do not put markers in lists, HTML, inline prose or code.
Excerpt markers select SourceRefs; Host writes the original characters. Never
write original-speech blocks yourself. Details markers form balanced Complete
full-note folds; do not nest them. Figure markers remain immediately above the
associated explanation. IDs are task-local; paths, HTML and URLs are forbidden.
Do not handwrite complete-verbatim, complete-full, host-source-ref or ppt-* HTML
structures or slides/images image references. The Host owns those structures
and records their output spans in a composition sidecar. Fenced, indented and
inline code are literal examples; marker-only lists, headings, blockquotes and
HTML containers are invalid contexts.
This is structural/source validation only; semantic review remains required.
In the plan proposal, provide objects with exactly four dictionaries (empty is
allowed): {"excerpts":{"e1":{"ref":"transcript:L1-L2"}},
"figures":{"f1":{"occurrences":["REAL_OCCURRENCE_ID"],"layout":"full",
"caption":"Verified caption"}},"sources":{"s1":{"refs":["transcript:L1-L2"]}},
"details":{"d1":{"title":"完整课堂笔记"}}}.
Keys are ASCII letters followed by at most 63 letters, digits, underscores or
hyphens. SourceRefs also support transcript_span (source_id transcript, start/end
Unicode code-point offsets), segment (source_id segments, id), and
slide_occurrence (source_id slides, id), always with kind and source_id fields.
Excerpt refs must select actual transcript/segments, never generated notes.
Figure layouts full/half/third require one occurrence; pair/triple/quad require
exactly 2/3/4 occurrences, respectively. Occurrence IDs come from the slide index;
never supply image paths. Sources refs retain separate known or unknown times.
"""
_ID = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,63}\Z')
_MARKER = re.compile(r'\[\[(/?details|excerpt|figure|source):([A-Za-z][A-Za-z0-9_-]{0,63})\]\]\Z')
_GROUPS = {'excerpts', 'figures', 'sources', 'details'}
_LAYOUTS = {'full': 1, 'half': 1, 'third': 1, 'pair': 2, 'triple': 3, 'quad': 4}


def _fail():
    raise ValueError('COMPOSER_INVALID_OBJECT_OR_MARKER')


def _resolve(root, sources, ref, *, speech=False):
    hashes = sources.get('authoring', {}).get('source_hashes')
    if not isinstance(hashes, dict):
        _fail()
    return resolve_ref(root, sources, ref, expected_hashes=hashes,
                       require_transcript=speech)


def _plain(value):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= 2000
            and not re.search(r'[\x00-\x1f\x7f]', value))


def _figure_refs(root, sources, obj):
    if not sources.get('options', {}).get('embed_slides'):
        _fail()
    hashes = sources.get('composer', {}).get('image_hashes')
    if not isinstance(hashes, dict):
        _fail()
    result = []
    for identity in obj['occurrences']:
        ref = dict(kind='slide_occurrence', source_id='slides', id=identity)
        resolved = _resolve(root, sources, ref)
        path = role_path(resolved.image_path, 'slide_image')
        if hashlib.sha256(read_bytes(root, path, role='slide_image')).hexdigest() != hashes.get(path):
            raise ValueError('COMPOSER_IMAGE_CHANGED')
        result.append((ref, resolved))
    return result


def validate_objects(root, sources, objects):
    """Validate the whole registry, including unused entries; never accept paths."""
    if not isinstance(objects, dict) or set(objects) != _GROUPS:
        _fail()
    for group, entries in objects.items():
        if not isinstance(entries, dict):
            _fail()
        for identity, obj in entries.items():
            if not isinstance(identity, str) or not _ID.fullmatch(identity) or not isinstance(obj, dict):
                _fail()
            if group == 'excerpts':
                if set(obj) != {'ref'}:
                    _fail()
                resolved = _resolve(root, sources, obj['ref'], speech=True)
                if not resolved.text:
                    _fail()
            elif group == 'sources':
                if set(obj) != {'refs'} or not isinstance(obj['refs'], list) or not obj['refs']:
                    _fail()
                for ref in obj['refs']:
                    _resolve(root, sources, ref)
            elif group == 'details':
                if set(obj) != {'title'} or not _plain(obj['title']):
                    _fail()
            else:
                if (set(obj) != {'occurrences', 'layout', 'caption'}
                        or not isinstance(obj['layout'], str) or obj['layout'] not in _LAYOUTS
                        or not isinstance(obj['occurrences'], list)
                        or len(obj['occurrences']) != _LAYOUTS[obj['layout']]
                        or any(not isinstance(i, str) or not i or len(i) > 256 for i in obj['occurrences'])
                        or len(set(obj['occurrences'])) != len(obj['occurrences'])
                        or not _plain(obj['caption'])):
                    _fail()
                _figure_refs(root, sources, obj)


def _location(ref, resolved):
    ref = parse_ref(ref)
    if ref['kind'] == 'transcript_lines':
        label = f"transcript:L{ref['start']}-L{ref['end']}"
    elif ref['kind'] == 'transcript_span':
        label = f"transcript 字符 [{ref['start']},{ref['end']})"
    else:
        label = ref['source_id'] + ':' + ref['id']
    intervals = ', '.join(f'{a}–{b} ms' for a, b in resolved.time_intervals) or '未知时间'
    return escape(label + '；' + intervals)


def _excerpt(root, sources, obj):
    resolved = _resolve(root, sources, obj['ref'], speech=True)
    return ('<details class="complete-verbatim">\n<summary>原转写节选</summary>\n'
            '<p>来源：' + _location(obj['ref'], resolved) + '</p>\n'
            '<blockquote><pre><code>' + escape(resolved.text).replace('\r', '&#13;').replace('\n', '&#10;') +
            '</code></pre></blockquote>\n</details>')


def _figure(root, sources, obj):
    refs = _figure_refs(root, sources, obj)
    panels = []
    for ref, resolved in refs:
        width = ' ppt-' + obj['layout'] if len(refs) == 1 else ''
        panels.append('<figure class="ppt-figure' + width + '">\n'
                      '<img src="../' + quote(resolved.image_path, safe='/') + '" alt="' +
                      escape(obj['caption'], quote=True) + '">\n<figcaption>' +
                      escape(obj['caption']) + '；' + _location(ref, resolved) +
                      '</figcaption>\n</figure>')
    if len(refs) == 1:
        return panels[0]
    css = 'ppt-grid-2x2' if len(refs) == 4 else 'ppt-cols-' + str(len(refs))
    return '<div class="ppt-grid ' + css + '">\n' + '\n'.join(panels) + '\n</div>'


class _Ownership(HTMLParser):
    def handle_starttag(self, tag, attrs):
        classes = [item for key, value in attrs if key == 'class' for item in (value or '').split()]
        owned = {'complete-verbatim', 'complete-full', 'host-source-ref',
                 'ppt-figure', 'ppt-grid', 'ppt-full', 'ppt-half', 'ppt-third', 'ppt-grid-2x2'}
        if any(c in owned or c.startswith('ppt-cols-') for c in classes):
            raise ValueError('COMPOSER_RAW_OWNERSHIP_VIOLATION')
        if tag == 'img':
            for key, value in attrs:
                if key != 'src':
                    continue
                path = posixpath.normpath(unquote(urlsplit(value or '').path).replace('\\', '/'))
                if path.startswith('slides/images/') or '/slides/images/' in '/' + path:
                    raise ValueError('COMPOSER_RAW_OWNERSHIP_VIOLATION')

    handle_startendtag = handle_starttag


class _HTMLMarkers(HTMLParser):
    def handle_data(self, data):
        if any(_MARKER.fullmatch(line.strip()) for line in data.splitlines()):
            raise ValueError('COMPOSER_MARKER_CONTEXT_INVALID')


def _preflight(text, tokens):
    # Rendered Markdown preserves executable HTML/images, but escapes literal
    # fenced, indented and inline code. Inspect only real structure, never text.
    rendered = markdown_parser().render(text)
    ownership = _Ownership()
    ownership.feed(rendered)
    ownership.close()
    quotes = _QuoteBlocks(rendered)
    quotes.feed(rendered)
    quotes.close()
    if quotes.blocks or any(quotes.is_quote(block) for block in quotes.stack):
        raise ValueError('COMPOSER_RAW_OWNERSHIP_VIOLATION')
    for index, token in enumerate(tokens):
        if token.type == 'html_block':
            parser = _HTMLMarkers()
            parser.feed(token.content)
            parser.close()
        elif token.type == 'inline':
            children = token.children or []
            plain = ''.join(c.content for c in children if c.type == 'text').strip()
            has_code = any(c.type == 'code_inline' for c in children)
            top_paragraph = (index > 0 and tokens[index - 1].type == 'paragraph_open'
                             and tokens[index - 1].level == 0)
            if (_MARKER.fullmatch(plain) and not has_code
                    and (not top_paragraph or token.content.strip() != plain)):
                raise ValueError('COMPOSER_MARKER_CONTEXT_INVALID')
            if any(c.type == 'html_inline' for c in children) and _MARKER.fullmatch(plain) and not has_code:
                raise ValueError('COMPOSER_MARKER_CONTEXT_INVALID')


def _hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def compose(text, root, sources, objects, mode='complete'):
    return _compose(text, root, sources, objects, mode)[0]


def _compose(text, root, sources, objects, mode):
    validate_objects(root, sources, objects)
    # Match Markdown's physical CR/LF lines without treating Unicode paragraph
    # separators as line boundaries, and retain the original newline bytes.
    lines = re.findall(r'[^\r\n]*(?:\r\n|\r|\n|$)', text)
    if lines and lines[-1] == '':
        lines.pop()
    edits, stack = {}, []
    tokens = markdown_parser().parse(text)
    _preflight(text, tokens)
    entries = {}
    for index, token in enumerate(tokens):
        if (token.type != 'inline' or not token.map or index == 0
                or tokens[index - 1].type != 'paragraph_open' or tokens[index - 1].level != 0):
            continue
        start, end = token.map
        raw = ''.join(lines[start:end]).strip()
        if end != start + 1 or not raw.startswith('[[') or not raw.endswith(']]'):
            continue
        match = _MARKER.fullmatch(raw)
        if not match:
            _fail()
        kind, identity = match.groups()
        group = {'excerpt': 'excerpts', 'figure': 'figures', 'source': 'sources',
                 'details': 'details', '/details': 'details'}[kind]
        if identity not in objects[group]:
            _fail()
        obj = objects[group][identity]
        if kind == 'excerpt':
            if mode != 'complete':
                _fail()
            value = _excerpt(root, sources, obj)
        elif kind == 'figure':
            value = _figure(root, sources, obj)
        elif kind == 'source':
            value = '<details class="host-source-ref"><summary>来源</summary>\n' + ''.join(
                '<p>' + _location(ref, _resolve(root, sources, ref)) + '</p>\n' for ref in obj['refs']) + '</details>'
        elif kind == 'details':
            if mode != 'complete' or stack:
                _fail()
            stack.append(identity)
            value = '<details class="complete-full">\n<summary>' + escape(obj['title']) + '</summary>\n'
        else:
            if not stack or stack.pop() != identity:
                _fail()
            value = '</details>'
        edits[start] = value + ('\n' if lines[start].endswith(('\r', '\n')) else '')
        entry = {'id': identity, 'kind': kind, 'value_length': len(value)}
        if kind == 'excerpt':
            entry['source_refs'] = [copy.deepcopy(obj['ref'])]
        elif kind == 'source':
            entry['source_refs'] = copy.deepcopy(obj['refs'])
        elif kind == 'figure':
            refs = _figure_refs(root, sources, obj)
            entry.update(source_refs=[ref for ref, _ in refs], occurrences=list(obj['occurrences']),
                         image_paths=[resolved.image_path for _, resolved in refs],
                         image_sha256=[sources['composer']['image_hashes'][resolved.image_path] for _, resolved in refs])
        entries[start] = entry
    if stack:
        _fail()
    chunks, manifest_entries, opened = [], [], None
    offset = 0
    for index, line in enumerate(lines):
        chunk = edits.get(index, line)
        if index in entries:
            entry = entries[index]
            end = offset + entry.pop('value_length')
            if entry['kind'] == '/details':
                opened.update(close_span=[offset, end], content_span=[opened['open_span'][1], offset],
                              output_span=[opened['open_span'][0], end])
                opened = None
            else:
                entry['output_span'] = [offset, end]
                manifest_entries.append(entry)
                if entry['kind'] == 'details':
                    entry['open_span'] = [offset, end]
                    opened = entry
        chunks.append(chunk)
        offset += len(chunk)
    result = ''.join(chunks)
    for entry in manifest_entries:
        start, end = entry['output_span']
        entry['block_sha256'] = _hash(result[start:end])
    errors = check_composed_quotes(result, root, sources, objects)
    if errors:
        raise ValueError(errors[0])
    return result, manifest_entries


def compose_with_manifest(text, root, sources, objects, mode='complete', *, plan_revision, plan_hash):
    """Host-only deterministic provenance; offsets index final Unicode text."""
    result, entries = _compose(text, root, sources, objects, mode)
    manifest = dict(schema_version=1, task_key=sources['authoring'].get('task_key'),
                    plan_revision=plan_revision, plan_hash=plan_hash, mode=mode,
                    input_draft_sha256=_hash(text), output_sha256=_hash(result),
                    span_semantics='unicode-codepoint-half-open', objects=entries)
    return result, manifest


def validate_composition(text, raw, manifest, root, sources, objects, mode='complete', *, plan_revision, plan_hash):
    """Replay proves both directions, including absence of unregistered blocks."""
    try:
        expected, receipt = compose_with_manifest(raw, root, sources, objects, mode,
                                                  plan_revision=plan_revision, plan_hash=plan_hash)
        # Canonical JSON comparison also rejects bool-for-int and extra fields.
        import json
        if text != expected or json.dumps(manifest, sort_keys=True, allow_nan=False) != json.dumps(receipt, sort_keys=True, allow_nan=False):
            return ['COMPOSER_COMPOSITION_MISMATCH']
    except (ValueError, OSError, TypeError, KeyError):
        return ['COMPOSER_INVALID_COMPOSITION']
    return []


class _QuoteBlocks(HTMLParser):
    """Capture raw rendered subtrees so even whitespace edits are detectable."""
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.text, self.stack, self.blocks = text, [], []
        self.offsets = [0]
        for line in text.split('\n'):
            self.offsets.append(self.offsets[-1] + len(line) + 1)

    def position(self):
        line, col = self.getpos()
        return self.offsets[line - 1] + col

    def handle_starttag(self, tag, attrs):
        if tag == 'details':
            self.stack.append(dict(start=self.position(), marked='complete-verbatim' in
                                   (dict(attrs).get('class') or '').split(), title='', summary=False))
        elif self.stack and tag == 'summary':
            self.stack[-1]['summary'] = True

    def handle_data(self, data):
        if self.stack and self.stack[-1]['summary']:
            self.stack[-1]['title'] += data

    def handle_endtag(self, tag):
        if self.stack and tag == 'summary':
            self.stack[-1]['summary'] = False
        elif self.stack and tag == 'details':
            block = self.stack.pop()
            if self.is_quote(block):
                end = self.text.find('>', self.position()) + 1
                self.blocks.append(self.text[block['start']:end])

    @staticmethod
    def is_quote(block):
        return block['marked'] or bool(re.match(r'\s*(?:清晰逐字记录|原转写节选|逐字记录)', block['title']))


def check_composed_quotes(text, root, sources, objects):
    """Only registered canonical Host blocks count as original-speech blocks."""
    try:
        validate_objects(root, sources, objects)
        renderer = markdown_parser()
        allowed = {renderer.render(_excerpt(root, sources, obj)).strip()
                   for obj in objects['excerpts'].values()}
        parser = _QuoteBlocks(renderer.render(text))
        parser.feed(parser.text)
        parser.close()
        if (any(parser.is_quote(block) for block in parser.stack)
                or any(block not in allowed for block in parser.blocks)):
            return ['COMPOSER_UNREGISTERED_OR_CHANGED_QUOTE']
    except (ValueError, OSError, TypeError):
        return ['COMPOSER_INVALID_SOURCE_OR_OBJECT']
    return []
