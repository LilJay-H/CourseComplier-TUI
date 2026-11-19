"""Read-only, task-scoped context tools for local Handoff editors.

Results are evidence/navigation, not instructions or semantic/visual attestations.
Byte limits bound text; cursors always count Unicode code points, not UTF-8 bytes.
"""
from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path

from .agents.base import read_handoff
from .authoring import latest_progress, validate_progress
from .safe_io import read_bytes, workspace_lock
from .source_refs import TranscriptIndex, parse_ref, resolve_ref, source_registry
from .validation import validate_workspace


def page(text, start=0, max_bytes=8192):
    if (type(start) is not int or not 0 <= start <= len(text)
            or type(max_bytes) is not int or not 4 <= max_bytes <= 32768):
        raise ValueError('EDITORIAL_PAGE_RANGE_INVALID')
    # Avoid encoding the entire remaining document for each small page.
    end, size = start, 0
    while end < len(text):
        amount = len(text[end].encode('utf-8'))
        if size + amount > max_bytes:
            break
        size += amount
        end += 1
    return {'text': text[start:end], 'start': start, 'end': end,
            'bytes': size, 'next_start': end if end < len(text) else None,
            'total_characters': len(text), 'truncated': end < len(text)}


def calculate(expression, *, domain):
    """Reuse the existing exact, bounded decimal grammar; never eval Python."""
    from .numeric_review import calculate_decimal_expression
    return calculate_decimal_expression(expression, domain=domain)


def context(workspace, *, source_ref=None, mode=None, query=None, start=0,
            max_bytes=8192, quote=False):
    root, sources, _ = read_handoff(workspace)
    with workspace_lock(root):
        if not validate_workspace(root).ok:
            raise ValueError('EDITORIAL_WORKSPACE_INVALID')
        if sum(item is not None for item in (source_ref, mode, query)) > 1:
            raise ValueError('EDITORIAL_SELECT_ONE_SCOPE')
        if quote and source_ref is None:
            raise ValueError('EDITORIAL_QUOTE_SOURCE_REQUIRED')
        page('', 0, max_bytes)  # One byte-budget contract for every branch, even empty searches/images.
        result = {'data_only': True, 'semantic_assessment': 'not_assessed',
                  'visual_assessment': 'not_assessed'}
        if source_ref is not None:
            ref = parse_ref(source_ref)
            resolved = resolve_ref(root, sources, ref, require_transcript=quote)
            result.update(source_ref=ref, source_sha256=resolved.sha256,
                          origin=resolved.origin, time_intervals=resolved.time_intervals)
            if resolved.image_path:
                raw = read_bytes(root, resolved.image_path, role='slide_image')
                result.update(image_path=resolved.image_path, image_sha256=hashlib.sha256(raw).hexdigest(),
                              bytes=len(raw), note='Open this exact image with a visual tool; path/hash is not visual review.')
                return result
            if resolved.text is None:
                raise ValueError('EDITORIAL_SOURCE_NOT_TEXT')
            if quote:
                if ref['kind'] != 'transcript_lines' or start != 0:
                    raise ValueError('EDITORIAL_QUOTE_REQUIRES_WHOLE_LINE_RANGE')
                selected = page(resolved.text, 0, max_bytes)
                if selected['truncated']:
                    raise ValueError('EDITORIAL_QUOTE_RANGE_TOO_LARGE')
                label = f"transcript:L{ref['start']}-L{ref['end']}"
                result.update(text=resolved.text, quote_markdown=(
                    '<details class="complete-verbatim">\n<summary>原转写节选</summary>\n'
                    f'<p>来源：{label}</p>\n<blockquote><p>{html.escape(resolved.text)}</p></blockquote>\n</details>'))
            else:
                result.update(page(resolved.text, start, max_bytes))
            return result
        if mode is not None:
            path = sources.get('expected_outputs', {}).get(mode)
            if not path:
                raise ValueError('EDITORIAL_MODE_INVALID')
            raw = read_bytes(root, path, role='note')
            selected = page(raw.decode('utf-8'), start, max_bytes)
            digest = hashlib.sha256(raw).hexdigest()
            return {**result, **selected, 'mode': mode, 'output_sha256': digest,
                'output_ref': {'kind': 'output_span', 'output_sha256': digest,
                               'start': selected['start'], 'end': selected['end']}}
        registry = source_registry(sources)
        if query is not None:
            if not isinstance(query, str) or not 1 <= len(query) <= 256 or 'transcript' not in registry:
                raise ValueError('EDITORIAL_SEARCH_INVALID')
            if type(start) is not int or start < 0:
                raise ValueError('EDITORIAL_PAGE_RANGE_INVALID')
            path, role = registry['transcript']
            index = TranscriptIndex.from_bytes(read_bytes(root, path, role=role))
            matches = [(n, line) for n, line in enumerate(index.lines, 1) if query in line]
            if start > len(matches):
                raise ValueError('EDITORIAL_PAGE_RANGE_INVALID')
            hits, used = [], 0
            cursor = start
            for number, line in matches[start:start + 32]:
                preview = page(line, 0, min(max_bytes, 1024))
                if hits and used + preview['bytes'] > max_bytes:
                    break
                hits.append({'source_ref': f'transcript:L{number}-L{number}', 'preview': preview['text'],
                             'preview_truncated': preview['truncated']})
                used += preview['bytes']; cursor += 1
            return {**result, 'navigation_only': True, 'matches': hits, 'match_count': len(matches),
                    'next_start': cursor if cursor < len(matches) else None,
                    'notice': 'A hit is not a support judgment; read its exact source_ref and neighboring context.'}
        record = latest_progress(root, sources)
        units = validate_progress(record, sources, root=root)['units'] if record else []
        if type(start) is not int or not 0 <= start <= len(units):
            raise ValueError('EDITORIAL_PAGE_RANGE_INVALID')
        selected, size, cursor = [], 0, start
        for unit in units[start:start + 32]:
            row = {key: unit[key] for key in ('id', 'goal', 'source_refs', 'status', 'text_needed')}
            raw = json.dumps(row, ensure_ascii=False).encode()
            if size + len(raw) > max_bytes:
                if not selected:
                    row = {'id': unit['id'], 'goal_preview': page(unit['goal'], 0, max_bytes)['text'],
                           'detail_omitted': True, 'notice': 'Read the authorized progress record for full refs/dependencies.'}
                    selected.append(row); cursor += 1
                break
            selected.append(row); cursor += 1; size += len(raw)
        return {**result, 'navigation_only': True, 'sources': list(registry), 'units': selected,
                'next_start': cursor if cursor < len(units) else None,
                'outputs': sources.get('expected_outputs', {}),
                'notice': 'Inventory is an editable declaration, not proof that the entire source is covered.'}
