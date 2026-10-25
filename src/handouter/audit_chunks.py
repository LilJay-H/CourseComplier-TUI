"""Bounded exact-source audit views and deterministic coverage accounting."""
from copy import deepcopy
import hashlib
import json

from .authoring_audit import source_scope, validate_scan, _source_ranges
from .authoring_eval import encoded
from .output_refs import resolve_output_ref
from .safe_io import read_bytes
from .source_refs import parse_ref, resolve_ref
from .quality_host import unicode_spans

SOURCE_VIEW = 'handoff/source-chunk.json'
MAX_IMAGE_BYTES = 6 * 1024 * 1024


def _spans(text, max_bytes):
    yield from unicode_spans(text, max_bytes)


def source_payload(root, sources, refs):
    result, entries = {}, []
    for ref in refs:
        resolved = resolve_ref(root, sources, ref, expected_hashes=sources['authoring']['source_hashes'])
        entries.append(dict(ref=deepcopy(ref), text=resolved.text, origin=resolved.origin,
                            image_path=resolved.image_path, time_intervals=resolved.time_intervals))
        if resolved.image_path:
            data = read_bytes(root, resolved.image_path, role='slide_image')
            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError('AUDIT_CHUNK_IMAGE_TOO_LARGE')
            expected = sources.get('composer', {}).get('image_hashes', {}).get(resolved.image_path)
            if expected is not None and hashlib.sha256(data).hexdigest() != expected:
                raise ValueError('COMPOSER_IMAGE_CHANGED')
            result[resolved.image_path] = data
    result[SOURCE_VIEW] = encoded({'entries': entries})
    return result


def source_chunks(root, sources, max_bytes=512 * 1024):
    if type(max_bytes) is not int or max_bytes < 1024:
        raise ValueError('AUDIT_CHUNK_BUDGET_INVALID')
    atoms = []
    for ref in source_scope(root, sources):
        if ref['kind'] == 'transcript_span':
            text = resolve_ref(root, sources, ref, expected_hashes=sources['authoring']['source_hashes']).text
            # Leave room for escaped JSON and locator metadata even with tiny
            # configured budgets and control-character-heavy input.
            limit = min(128 * 1024, max(4, (max_bytes - 512) // 6))
            atoms.extend(dict(ref, start=start, end=end) for start, end in _spans(text, limit))
        else:
            atoms.append(ref)
    chunks, pending = [], []
    def finish(refs):
        payload = source_payload(root, sources, refs)
        source_bytes = len(payload[SOURCE_VIEW])
        image_bytes = sum(len(data) for path, data in payload.items() if path != SOURCE_VIEW)
        return dict(chunk_id=f'source-{len(chunks) + 1:04d}', source_refs=refs,
                    visible_bytes=source_bytes + image_bytes, image_bytes=image_bytes, source_bytes=source_bytes)
    for ref in atoms:
        candidate = finish(pending + [ref])
        if candidate['visible_bytes'] > max_bytes and pending:
            chunks.append(finish(pending))
            pending = []
            candidate = finish([ref])
        if candidate['visible_bytes'] > max_bytes:
            # A real single image can occupy its own bounded view, never silently
            # resize/truncate it or multiply it across unrelated source chunks.
            if not candidate['image_bytes'] or candidate['source_bytes'] > max_bytes:
                raise ValueError('AUDIT_CHUNK_SOURCE_TOO_LARGE')
            chunks.append(candidate)
        else:
            pending.append(ref)
    if pending:
        chunks.append(finish(pending))
    return chunks


def output_chunks(data, max_bytes=256 * 1024):
    text = data.decode('utf-8')
    digest = hashlib.sha256(data).hexdigest()
    return [dict(kind='output_span', output_sha256=digest, start=start, end=end)
            for start, end in _spans(text, max_bytes)]


def _union(ranges):
    result = []
    for key, start, end in sorted(ranges):
        if result and result[-1][0] == key and start <= result[-1][2]:
            result[-1] = (key, result[-1][1], max(end, result[-1][2]))
        else:
            result.append((key, start, end))
    return result


def _difference(full, checked):
    full, checked = _union(full), _union(checked)
    for key, start, end in checked:
        if not any(other == key and left <= start <= end <= right for other, left, right in full):
            raise ValueError('AUDIT_CHECKED_OUTSIDE_SCOPE')
    result = []
    for key, start, end in full:
        cursor = start
        for other, left, right in checked:
            if other != key or right < start or left > end:
                continue
            if left > cursor:
                result.append((key, cursor, left))
            cursor = max(cursor, right)
        if cursor < end or start == end and not any(other == key and left <= start <= right for other, left, right in checked):
            result.append((key, cursor, end))
    return result


def _source_refs(ranges):
    refs = []
    for key, start, end in ranges:
        if key == 'transcript':
            refs.append(dict(kind='transcript_span', source_id='transcript', start=start, end=end))
        else:
            source, identity = key.split(':', 1)
            refs.append(dict(kind='segment' if source == 'segments' else 'slide_occurrence', source_id=source, id=identity))
    return refs


def merge_source_refs(refs, root, sources):
    return _source_refs(_union(_source_ranges(refs, root, sources)))


def source_complement(full_scope, checked, root, sources):
    return _source_refs(_difference(_source_ranges(full_scope, root, sources), _source_ranges(checked, root, sources)))


def _outputs(refs, data):
    return [('output', value.start, value.end) for ref in refs for value in [resolve_output_ref(data, ref)]]


def _output_refs(ranges, data):
    digest = hashlib.sha256(data).hexdigest()
    return [dict(kind='output_span', output_sha256=digest, start=start, end=end) for _, start, end in ranges]


def merge_output_refs(refs, data):
    return _output_refs(_union(_outputs(refs, data)), data)


def output_complement(full_scope, checked, data):
    return _output_refs(_difference(_outputs(full_scope, data), _outputs(checked, data)), data)


def merge_scans(values, binding, full_scope, root, sources):
    """Aggregate prevalidated chunk results; missing/failed chunks stay visible.

    Caller retains each source chunk/result and validates its Host-assigned scope
    before this aggregation. No union can erase an explicit failed/partial job.
    """
    checked, candidates, reviewers = [], {}, []
    complete = bool(values)
    for value in values:
        if value is None:
            complete = False
            continue
        scope = value['source_refs_checked'] + value['source_refs_unassessed']
        value = validate_scan(value, binding, root, sources, scope)
        complete &= value['status'] == 'completed'
        checked.extend(value['source_refs_checked'])
        if value['reviewer'] not in reviewers:
            reviewers.append(value['reviewer'])
        for candidate in value['candidates']:
            refs = sorted((parse_ref(ref) for ref in candidate['source_refs']), key=lambda ref: json.dumps(ref, sort_keys=True))
            body = {'source_refs': refs, 'kind': candidate['kind'], 'reason': candidate['reason']}
            key = json.dumps(body, sort_keys=True, ensure_ascii=False)
            candidates[key] = {'id': 'candidate-' + hashlib.sha256(key.encode('utf-8')).hexdigest()[:24], **body}
    checked = merge_source_refs(checked, root, sources)
    unassessed = source_complement(full_scope, checked, root, sources)
    result = dict(binding, status='completed' if complete and not unassessed and candidates else ('partial' if reviewers else 'not_run'),
                  reviewer={'kind': 'model', 'id': 'aggregate:see-chunk-claims' if reviewers else 'not_run'},
                  source_refs_checked=checked, source_refs_unassessed=unassessed,
                  candidates=[candidates[key] for key in sorted(candidates)])
    return validate_scan(result, binding, root, sources, full_scope)
