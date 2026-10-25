"""Hash-bound output locations; resolution never establishes semantic support."""
from dataclasses import dataclass
import hashlib

from .source_refs import TranscriptIndex
from .quality_host import resolve_output_span


@dataclass(frozen=True)
class ResolvedOutputRef:
    text: str
    start: int
    end: int
    sha256: str


def resolve_output_ref(data, ref, *, root=None, sources=None, mode=None, plan=None):
    if not isinstance(data, bytes) or not isinstance(ref, dict):
        raise ValueError('OUTPUT_REF_INVALID')
    digest = hashlib.sha256(data).hexdigest()
    if ref.get('output_sha256') != digest:
        raise ValueError('OUTPUT_CHANGED')
    text = data.decode('utf-8')
    kind = ref.get('kind')
    if kind in ('output_span', 'output_lines'):
        if set(ref) != {'kind', 'output_sha256', 'start', 'end'}:
            raise ValueError('OUTPUT_REF_INVALID')
        start, end = ref['start'], ref['end']
        if kind == 'output_span':
            start,end,_ = resolve_output_span(data,ref)
        if type(start) is not int or type(end) is not int:
            raise ValueError('OUTPUT_REF_OUT_OF_RANGE')
        if kind == 'output_lines':
            index = TranscriptIndex(text)
            if not 1 <= start <= end <= len(index.lines):
                raise ValueError('OUTPUT_REF_OUT_OF_RANGE')
            start, end = index.line_starts[start - 1], index.line_starts[end - 1] + len(index.lines[end - 1])
    elif kind == 'composer_object':
        if (set(ref) not in ({'kind', 'output_sha256', 'object_id'},
                             {'kind', 'output_sha256', 'object_id', 'object_kind'})
                or not isinstance(ref['object_id'], str) or not ref['object_id']
                or root is None or sources is None or plan is None or mode is None):
            raise ValueError('OUTPUT_REF_INVALID')
        from .composition_store import checked_composition
        manifest = checked_composition(root, sources, data, mode, plan)
        matches = [obj for obj in manifest['objects'] if obj['id'] == ref['object_id']
                   and ('object_kind' not in ref or obj['kind'] == ref['object_kind'])]
        if len(matches) != 1:
            raise ValueError('OUTPUT_OBJECT_AMBIGUOUS_OR_MISSING')
        start, end = matches[0]['output_span']
    else:
        raise ValueError('OUTPUT_REF_KIND_UNSUPPORTED')
    if not 0 <= start <= end <= len(text):
        raise ValueError('OUTPUT_REF_OUT_OF_RANGE')
    return ResolvedOutputRef(text[start:end], start, end, digest)
