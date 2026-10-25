"""Small Host checks shared by staged authoring and private Full auditing.

These prove bindings and edit scope. They never claim semantic correctness.
"""
import hashlib
import json
import math


def strict_document(data, *, max_bytes=512 * 1024):
    if not isinstance(data, bytes) or len(data) > max_bytes:
        raise ValueError('AUDIT_SCHEMA_INVALID')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('AUDIT_DUPLICATE_KEY')
            result[key] = value
        return result
    def constant(_):
        raise ValueError('AUDIT_NONFINITE')
    try:
        result = json.loads(data.decode('utf-8'), object_pairs_hook=pairs,
                            parse_constant=constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError('AUDIT_SCHEMA_INVALID') from exc
    pending = [(result, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 20 or isinstance(value, float) and not math.isfinite(value):
            raise ValueError('AUDIT_SCHEMA_INVALID')
        if isinstance(value, dict):
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
    return result


def exact_replacement(data, ref, replacement):
    """Apply one hash-bound Unicode span, retaining every byte outside it."""
    if not isinstance(replacement, str) or len(replacement.encode('utf-8')) > 65536:
        raise ValueError('REPAIR_SCOPE_INVALID')
    selected = resolve_output_span(data, ref)
    original = data.decode('utf-8')
    changed = original[:selected[0]] + replacement + original[selected[1]:]
    if changed == original:
        raise ValueError('REPAIR_NO_CHANGE')
    result = changed.encode('utf-8')
    return result, {'kind': 'output_span', 'output_sha256': hashlib.sha256(result).hexdigest(),
                    'start': selected[0], 'end': selected[0] + len(replacement)}


def resolve_output_span(data, ref):
    """Exact hash-bound Unicode output span, shared with P5 OutputRef."""
    if (not isinstance(data,bytes) or not isinstance(ref,dict)
            or set(ref)!={'kind','output_sha256','start','end'}
            or ref.get('kind')!='output_span'
            or ref.get('output_sha256')!=hashlib.sha256(data).hexdigest()):
        raise ValueError('OUTPUT_REF_INVALID')
    start,end=ref['start'],ref['end']
    text=data.decode('utf-8')
    if type(start) is not int or type(end) is not int or not 0<=start<=end<=len(text):
        raise ValueError('OUTPUT_REF_OUT_OF_RANGE')
    return start,end,text[start:end]


def exact_partition(expected, checked, unassessed):
    """Host accounting for finite source IDs: no duplicates or dropped scopes."""
    if not all(isinstance(part, list) for part in (expected, checked, unassessed)):
        raise ValueError('AUDIT_SCOPE_INVALID')
    if (len(expected) != len(set(expected)) or len(checked) != len(set(checked))
            or len(unassessed) != len(set(unassessed))
            or set(checked) & set(unassessed)
            or set(expected) != set(checked) | set(unassessed)):
        raise ValueError('AUDIT_SCOPE_INCOMPLETE')


def unicode_spans(text, max_bytes):
    """P5 and cloud use identical bounded UTF-8 view boundaries."""
    if not isinstance(text, str) or type(max_bytes) is not int or max_bytes < 4:
        raise ValueError('AUDIT_CHUNK_BUDGET_INVALID')
    start, size = 0, 0
    for index, char in enumerate(text):
        width = len(char.encode('utf-8'))
        if size + width > max_bytes:
            yield start, index
            start, size = index, 0
        size += width
    yield start, len(text)
