"""Task-local source references. Resolution proves location, never meaning."""
from dataclasses import dataclass
from bisect import bisect_right
import hashlib
import json
import re

from .safe_io_core import read_bytes


class SourceRefError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)  # No source text or private paths in diagnostics.


class TranscriptIndex:
    """Canonical decoded code points and Python splitlines() source lines.

    Decode bytes with from_bytes(): only the leading UTF-8 BOM is removed.
    Spans preserve physical newlines and Unicode exactly. Line selection retains
    the established normalized-LF, no-line-terminator SourceRef representation.
    Empty spans are exact empty text; they have no containing source lines.
    """

    def __init__(self, text):
        self.text = text
        self.lines = tuple(text.splitlines())
        starts = []
        cursor = 0
        for line in text.splitlines(keepends=True):
            starts.append(cursor)
            cursor += len(line)
        self.line_starts = tuple(starts)

    @classmethod
    def from_bytes(cls, data):
        return cls(data.decode('utf-8-sig'))

    def _validate(self, start, end, *, lines=False):
        lower, upper = (1, len(self.lines)) if lines else (0, len(self.text))
        if (type(start) is not int or type(end) is not int or start < lower
                or end < start or end > upper):
            raise SourceRefError('REF_OUT_OF_RANGE')

    def span_text(self, start, end):
        """Exact zero-based Unicode code-point [start, end), including empty."""
        self._validate(start, end)
        return self.text[start:end]

    def line_text(self, start, end):
        """One-based closed source lines, joined with LF as in SourceRef v1."""
        self._validate(start, end, lines=True)
        return '\n'.join(self.lines[start - 1:end])

    def containing_lines(self, start, end):
        """Containing lines for a nonempty exact span; never an exact substring."""
        self._validate(start, end)
        if start == end:
            return None
        return (bisect_right(self.line_starts, start),
                bisect_right(self.line_starts, end - 1))


@dataclass(frozen=True)
class ResolvedRef:
    source_id: str
    sha256: str
    text: str | None
    origin: str
    time_intervals: tuple = ()
    image_path: str | None = None


def source_registry(sources):
    """Only Host-selected, role-constrained sources; refs cannot contain paths."""
    transcript = sources.get('transcript', {})
    registry = {}
    for identity, path, role in (
        ('transcript', transcript.get('path'), 'transcript'),
        ('segments', transcript.get('segments_path'), 'transcript_segments'),
        ('slides', sources.get('slides', {}).get('index_path')
         if sources.get('options', {}).get('use_slides_as_source') else None, 'slide_index'),
    ):
        if path:
            registry[identity] = (path, role)
    return registry


def source_hashes(root, sources):
    return {identity: hashlib.sha256(read_bytes(root, path, role=role)).hexdigest()
            for identity, (path, role) in source_registry(sources).items()}


def parse_ref(ref):
    if isinstance(ref, str):
        match = re.fullmatch(r'transcript:L([1-9][0-9]{0,8})-L([1-9][0-9]{0,8})', ref)
        if not match:
            raise SourceRefError('REF_KIND_UNSUPPORTED')
        return dict(kind='transcript_lines', source_id='transcript',
                    start=int(match[1]), end=int(match[2]))
    if not isinstance(ref, dict):
        raise SourceRefError('REF_KIND_UNSUPPORTED')
    return ref


def resolve_ref(root, sources, ref, *, expected_hashes=None, require_transcript=False):
    ref = parse_ref(ref)
    kind = ref.get('kind')
    kinds = {'transcript_lines': 'transcript', 'transcript_span': 'transcript',
             'segment': 'segments', 'slide_occurrence': 'slides'}
    if not isinstance(kind, str) or kind not in kinds:
        raise SourceRefError('REF_KIND_UNSUPPORTED')
    fields = {'kind', 'source_id', 'start', 'end'} if kind.startswith('transcript_') else {'kind', 'source_id', 'id'}
    if set(ref) != fields or ref.get('source_id') != kinds[kind]:
        raise SourceRefError('REF_INVALID')
    identity = ref['source_id']
    origin = sources.get('transcript', {}).get('origin', 'provided_transcript') if identity != 'slides' else 'slide'
    if require_transcript and (origin == 'generated_note' or identity == 'slides'):
        raise SourceRefError('SOURCE_IDENTITY_INVALID')
    registry = source_registry(sources)
    if identity not in registry:
        raise SourceRefError('SOURCE_MISSING')
    path, role = registry[identity]
    try:
        data = read_bytes(root, path, role=role)
    except FileNotFoundError:
        raise SourceRefError('SOURCE_MISSING') from None
    except (ValueError, OSError):
        raise SourceRefError('SOURCE_UNSAFE') from None
    digest = hashlib.sha256(data).hexdigest()
    if expected_hashes is not None and expected_hashes.get(identity) != digest:
        raise SourceRefError('SOURCE_CHANGED')
    try:
        if kind.startswith('transcript_'):
            index = TranscriptIndex.from_bytes(data)
            start, end = ref['start'], ref['end']
            selected = (index.line_text(start, end) if kind == 'transcript_lines'
                        else index.span_text(start, end))
            return ResolvedRef(identity, digest, selected, origin)
        text = data.decode('utf-8-sig')
        if not isinstance(ref['id'], str) or not ref['id'] or len(ref['id']) > 256:
            raise SourceRefError('REF_INVALID')
        items = json.loads(text)
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise SourceRefError('SOURCE_INVALID')
        key = 'id' if kind == 'segment' else 'occurrence_id'
        matches = [item for item in items if item.get(key) == ref['id']]
        if not matches:
            raise SourceRefError('SOURCE_MISSING')
        if len(matches) != 1:
            raise SourceRefError('REF_AMBIGUOUS')
        item = matches[0]
        start, end = item.get('start_ms'), item.get('end_ms')
        intervals = ((start, end),) if type(start) is int and type(end) is int and 0 <= start <= end else ()
        if kind == 'segment' and not isinstance(item.get('text'), str):
            raise SourceRefError('SOURCE_INVALID')
        return ResolvedRef(identity, digest, item.get('text') if kind == 'segment' else None,
                           origin, intervals, item.get('image_path') if kind == 'slide_occurrence' else None)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise SourceRefError('SOURCE_INVALID') from None
