"""Optional local editorial declarations; never a publication or API capability.

Bindings/locations are Host-checked. Semantic judgments remain attributed Agent
reports. Nothing here dispatches a model, modifies notes, or grants repair rights.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from pathlib import Path

from .authoring_v2 import strict_json
from .credentials import scan_credentials
from .output_refs import resolve_output_ref
from .safe_io import publish_bytes, read_bytes, safe_path, workspace_lock
from .source_refs import resolve_ref, source_hashes

RECORD_PATH = 'authoring/editorial-review.json'
MODULE = 'handoff/skill/references/execution/editorial-review.md'
MAX_BYTES = 128 * 1024
DIMENSIONS = ('source_preservation', 'claim_support', 'technical', 'rendering')
STATUSES = ('not_run', 'partial', 'completed')
CATEGORIES = ('omission', 'example_replacement', 'unfinished_example', 'distortion',
              'condition_loss', 'unsupported_attribution', 'quantity_or_unit',
              'technical_error', 'slide_semantic_mismatch', 'cross_layer_inconsistency')


def _object(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties),
            'additionalProperties': False}


def _array(items, limit):
    return {'type': 'array', 'items': items, 'maxItems': limit}


def review_schema():
    """Shared structural contract; integer wire fields also require JSON int tokens."""
    text = {'type': 'string', 'maxLength': 2048}
    nonempty = {**text, 'pattern': r'\S'}
    digest = {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}
    source_variants = [{'type': 'string', 'pattern': '^transcript:L[1-9][0-9]{0,8}-L[1-9][0-9]{0,8}$'}]
    output_variants = []
    for kind, lower in (('transcript_lines', 1), ('transcript_span', 0)):
        source_variants.append(_object({'kind': {'enum': [kind]}, 'source_id': {'enum': ['transcript']},
            'start': {'type': 'integer', 'minimum': lower}, 'end': {'type': 'integer', 'minimum': lower}}))
    for kind, identity in (('segment', 'segments'), ('slide_occurrence', 'slides')):
        source_variants.append(_object({'kind': {'enum': [kind]}, 'source_id': {'enum': [identity]},
            'id': {'type': 'string', 'minLength': 1, 'maxLength': 256}}))
    for kind, lower in (('output_lines', 1), ('output_span', 0)):
        output_variants.append(_object({'kind': {'enum': [kind]}, 'output_sha256': digest,
            'start': {'type': 'integer', 'minimum': lower}, 'end': {'type': 'integer', 'minimum': lower}}))
    source_refs = _array({'$ref': '#/$defs/source_ref'}, 64)
    output_refs = _array({'$ref': '#/$defs/output_ref'}, 64)
    finding = _object({'id': {'type': 'string', 'pattern': '^[A-Za-z0-9_-]{1,64}$'},
        'category': {'enum': list(CATEGORIES)}, 'severity': {'enum': ['critical', 'high', 'medium', 'low']},
        'problem': nonempty, 'repair_condition': nonempty,
        'layer': {'enum': ['visible', 'detail', 'verbatim', 'unknown']},
        'source_refs': source_refs, 'output_refs': output_refs})
    located = {'anyOf': [{'type': 'object', 'properties': {key: {'type': 'array', 'minItems': 1}}}
                         for key in ('source_refs', 'output_refs')]}
    finding['allOf'] = [located, {'if': {'type': 'object', 'properties': {'category': {
        'enum': ['omission', 'example_replacement', 'unfinished_example']}}},
        'then': {'type': 'object', 'properties': {'source_refs': {'type': 'array', 'minItems': 1}}}}]
    check = _object({'status': {'enum': list(STATUSES)}, 'method': text,
        'source_refs': source_refs, 'output_refs': output_refs,
        'limitations': _array(nonempty, 32), 'findings': _array({'$ref': '#/$defs/finding'}, 100)})
    check['allOf'] = [{'if': {'type': 'object', 'properties': {'status': {'enum': ['not_run']}}},
        'then': {'type': 'object', 'properties': {key: {'type': 'array', 'maxItems': 0}
                  for key in ('source_refs', 'output_refs', 'findings')}},
        'else': {'allOf': [located, {'type': 'object', 'properties': {'method': nonempty}}]}}]
    report = _object({'mode': {'enum': ['complete', 'deep', 'full', 'summary', 'verbatim']},
        'output_sha256': digest, 'html_sha256': {'anyOf': [digest, {'type': 'null'}]},
        'checks': _object({name: {'$ref': '#/$defs/check'} for name in DIMENSIONS})})
    report['allOf'] = [{'if': {'type': 'object', 'properties': {'checks': {'type': 'object',
        'properties': {'rendering': {'type': 'object', 'properties': {'status': {'enum': ['partial', 'completed']}}}}}}},
        'then': {'type': 'object', 'properties': {'html_sha256': digest}}}]
    schema = _object({'schema_version': {'type': 'integer', 'enum': [1]},
        'task_key': digest, 'source_hashes': {'type': 'object', 'additionalProperties': digest},
        'reports': {**_array({'$ref': '#/$defs/report'}, 5), 'minItems': 1}})
    schema['$defs'] = {'finding': finding, 'check': check, 'report': report,
                       'source_ref': {'anyOf': source_variants}, 'output_ref': {'anyOf': output_variants}}
    return schema


def _shape(value, schema, definitions):
    """Validate only our emitted subset. Schema is code-owned, never user input."""
    for child in schema.get('allOf', []):
        _shape(value, child, definitions)
    if 'if' in schema:
        try:
            _shape(value, schema['if'], definitions)
            branch = 'then'
        except ValueError:
            branch = 'else'
        _shape(value, schema.get(branch, {}), definitions)
    if '$ref' in schema:
        return _shape(value, definitions[schema['$ref'].rsplit('/', 1)[-1]], definitions)
    if 'anyOf' in schema:
        for candidate in schema['anyOf']:
            try:
                _shape(value, candidate, definitions)
                return
            except ValueError:
                pass
        raise ValueError('EDITORIAL_SCHEMA_INVALID')
    kind = schema.get('type')
    valid = {'object': lambda: isinstance(value, dict), 'array': lambda: isinstance(value, list),
             'string': lambda: isinstance(value, str), 'integer': lambda: type(value) is int,
             'null': lambda: value is None}
    if kind and not valid[kind]():
        raise ValueError('EDITORIAL_SCHEMA_INVALID')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError('EDITORIAL_SCHEMA_INVALID')
    if kind == 'object':
        props = schema.get('properties', {})
        if not set(schema.get('required', [])) <= set(value):
            raise ValueError('EDITORIAL_SCHEMA_INVALID')
        for key, item in value.items():
            if key in props:
                _shape(item, props[key], definitions)
            elif isinstance(schema.get('additionalProperties'), dict):
                _shape(item, schema['additionalProperties'], definitions)
            elif schema.get('additionalProperties') is False:
                raise ValueError('EDITORIAL_SCHEMA_INVALID')
    elif kind == 'array':
        if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', MAX_BYTES):
            raise ValueError('EDITORIAL_SCHEMA_INVALID')
        for item in value:
            _shape(item, schema.get('items', {}), definitions)
    elif kind == 'string':
        if (not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', MAX_BYTES)
                or ('pattern' in schema and not re.search(schema['pattern'], value))):
            raise ValueError('EDITORIAL_SCHEMA_INVALID')
    elif kind == 'integer' and 'minimum' in schema and value < schema['minimum']:
        raise ValueError('EDITORIAL_SCHEMA_INVALID')


def instructions():
    return ('# Local editorial review · 可选分项声明\n\n'
        '只在 sources.editorial_review 存在时写其 record_path；schema与字段如下。顶层 task_key 使用 sources.authoring.task_key，'
        'source_hashes 逐字使用 sources.editorial_review.source_hashes。reports 只列实际检查的当前模式；绑定最终 Markdown 原始 UTF-8 字节摘要。'
        'html_sha256 未实际查看对应 HTML 时为 null，rendering 为 not_run。未知方法/范围保持空，不造调用或身份。'
        'integer 字段使用 JSON 整数字面量（如 1）；不使用 true/false、1.0 或 1e0。这是严格解析的表示要求，原返回字节不自动改写。\n\n'
        '各维度 status 表示声明的检查过程，不是正确性。source_preservation 从原文找遗漏；claim_support 从成品找新增事实、归属与条件；'
        'technical 从题设复算；rendering 仅记录实际 HTML 阅读。来源沿用 SourceRef；正文用 {kind:output_span,output_sha256,start,end} 零基 Unicode 范围。'
        '每个问题有具体 repair_condition；同一mode和该mode内的发现ID不能重复。遗漏可以无正文引句，但必须有原始来源；其他问题至少有一个来源或正文定位。'
        'completed 需非空 method 和实际定位，无检查时 status=not_run 且空范围/发现。局部检查列出 limitations。'
        '字段合法不证明读完、无遗漏或技术正确，所有结论均显示 Agent 声明；不要输出 passed/semantic_passed 或伪造独立评审。'
        'schema之外的约束是当前任务/源/稿身份、合法定位及HTML实际存在；同一稿的旧发现不得被后续报告静默清除；异议留限制说明，正式裁定用显式复核流程。旧报告不授权新调用，改稿后重新检查，不覆盖旧稿。\n\n```json\n'
        + json.dumps(review_schema(), ensure_ascii=False, separators=(',', ':')) + '\n```\n')


def make_policy(root, sources):
    return {'schema_version': 1, 'record_path': RECORD_PATH, 'max_bytes': MAX_BYTES,
            'source_hashes': source_hashes(root, sources)}


def policy(sources):
    value = sources.get('editorial_review')
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'record_path', 'max_bytes', 'source_hashes'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['record_path'] != RECORD_PATH or value['max_bytes'] != MAX_BYTES
            or not isinstance(value['source_hashes'], dict) or not sources.get('authoring', {}).get('task_key')):
        raise ValueError('EDITORIAL_POLICY_INVALID')
    return value


def html_digest(root, sources, mode, note_digest):
    """Export existence/hash is not proof of browser rendering."""
    try:
        state = strict_json(read_bytes(root, 'state.json'))
        delivery = state.get('html_delivery', {})
        if delivery.get('note_hashes', {}).get(mode) != note_digest:
            return None
        path = delivery['outputs'][mode]
        if not path.startswith('notes/') or not path.endswith('.html'):
            return None
        raw = read_bytes(root, path)
        digest = hashlib.sha256(raw).hexdigest()
        return digest if digest == delivery['hashes'][mode] else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def template(root, sources, mode):
    current = policy(sources)
    if current is None or mode not in sources['expected_outputs']:
        raise ValueError('EDITORIAL_NOT_ENABLED')
    raw = read_bytes(root, sources['expected_outputs'][mode], role='note')
    check = {'status': 'not_run', 'method': '', 'source_refs': [], 'output_refs': [],
             'limitations': [], 'findings': []}
    return {'schema_version': 1, 'task_key': sources['authoring']['task_key'],
        'source_hashes': deepcopy(current['source_hashes']), 'reports': [{'mode': mode,
        'output_sha256': hashlib.sha256(raw).hexdigest(), 'html_sha256': None,
        'checks': {name: deepcopy(check) for name in DIMENSIONS}}]}


def validate_review(data, root, sources):
    current = policy(sources)
    if current is None:
        raise ValueError('EDITORIAL_NOT_ENABLED')
    if len(data) > MAX_BYTES:
        raise ValueError('EDITORIAL_TOO_LARGE')
    value = strict_json(data)
    schema = review_schema()
    _shape(value, schema, schema['$defs'])
    if (value['task_key'] != sources['authoring']['task_key']
            or value['source_hashes'] != current['source_hashes']
            or value['source_hashes'] != source_hashes(root, sources)):
        raise ValueError('EDITORIAL_SOURCE_OR_TASK_CHANGED')
    if scan_credentials(data.decode('utf-8')).errors:
        raise ValueError('EDITORIAL_PRIVATE_DATA')
    seen_modes = set()
    for report in value['reports']:
        mode = report['mode']
        if mode in seen_modes or mode not in sources['expected_outputs']:
            raise ValueError('EDITORIAL_MODE_INVALID')
        seen_modes.add(mode)
        raw = read_bytes(root, sources['expected_outputs'][mode], role='note')
        if report['output_sha256'] != hashlib.sha256(raw).hexdigest():
            raise ValueError('EDITORIAL_OUTPUT_CHANGED')
        ids = set()
        for name, check in report['checks'].items():
            if check['status'] == 'not_run' and any(check[k] for k in ('source_refs', 'output_refs', 'findings')):
                raise ValueError('EDITORIAL_NOT_RUN_HAS_EVIDENCE')
            if check['status'] != 'not_run' and (not check['method'].strip()
                    or not (check['source_refs'] or check['output_refs'])):
                raise ValueError('EDITORIAL_CHECK_EVIDENCE_REQUIRED')
            if name == 'rendering' and check['status'] != 'not_run':
                if not report['html_sha256'] or report['html_sha256'] != html_digest(root, sources, mode, report['output_sha256']):
                    raise ValueError('EDITORIAL_HTML_EVIDENCE_MISSING')
            for item in [check, *check['findings']]:
                for ref in item['source_refs']:
                    resolved = resolve_ref(root, sources, ref, expected_hashes=current['source_hashes'])
                    if resolved.image_path:
                        read_bytes(root, resolved.image_path, role='slide_image')
                for ref in item['output_refs']:
                    located = resolve_output_ref(raw, ref)
                    if not located.text.strip():
                        raise ValueError('EDITORIAL_EMPTY_OUTPUT_ANCHOR')
            for issue in check['findings']:
                if issue['id'] in ids:
                    raise ValueError('EDITORIAL_DUPLICATE_FINDING')
                ids.add(issue['id'])
                if not (issue['source_refs'] or issue['output_refs']):
                    raise ValueError('EDITORIAL_FINDING_LOCATION_REQUIRED')
                if issue['category'] in ('omission', 'example_replacement', 'unfinished_example') and not issue['source_refs']:
                    raise ValueError('EDITORIAL_OMISSION_SOURCE_REQUIRED')
    return value


def _directory(root, sources):
    return safe_path(root, 'authoring/' + sources['authoring']['task_key'], missing=True)


def save_review(root, sources, data):
    root = Path(root).resolve(strict=True)
    # No sandbox evidence can authorize a different published note.
    value = validate_review(data, root, sources)
    for report in value['reports']:
        previous = _latest(root, sources, report['mode'])
        if previous and previous[1]['output_sha256'] == report['output_sha256']:
            old = {issue['id']: issue for check in previous[1]['checks'].values() for issue in check['findings']}
            new = {issue['id']: issue for check in report['checks'].values() for issue in check['findings']}
            if any(new.get(key) != issue for key, issue in old.items()):
                raise ValueError('EDITORIAL_FINDINGS_REQUIRE_REVISION_OR_ADJUDICATION')
    directory = _directory(root, sources)
    directory.mkdir(parents=True, exist_ok=True)
    for number in range(1, 10000):
        relative = directory.relative_to(root).as_posix() + f'/editorial-{number:04d}.json'
        try:
            return publish_bytes(root, relative, data)
        except FileExistsError:
            continue
    raise ValueError('EDITORIAL_HISTORY_FULL')


def import_review(workspace, returned):
    from .agents.base import read_handoff
    from .validation import validate_workspace
    root, sources, _ = read_handoff(workspace)
    path = Path(returned)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError('EDITORIAL_IMPORT_UNSAFE')
    data = path.read_bytes()
    with workspace_lock(root):
        if not validate_workspace(root).ok:
            raise ValueError('EDITORIAL_WORKSPACE_INVALID')
        saved = save_review(root, sources, data)
    return {'status': 'declaration_recorded', 'record': saved.relative_to(root).as_posix(),
            'semantic_acceptance': 'not_certified'}


def _latest(root, sources, mode):
    root = Path(root).resolve(strict=True)
    directory = _directory(root, sources)
    if not directory.is_dir():
        return None
    for file in sorted(directory.glob('editorial-*.json'), reverse=True):
        if not re.fullmatch(r'editorial-\d{4}\.json', file.name):
            continue
        path = safe_path(root, file.relative_to(root).as_posix())
        if path.stat().st_size > MAX_BYTES:
            raise ValueError('EDITORIAL_TOO_LARGE')
        data = read_bytes(root, path.relative_to(root).as_posix())
        value = strict_json(data)
        schema = review_schema(); _shape(value, schema, schema['$defs'])
        for report in value['reports']:
            if report['mode'] == mode:
                return value, report, data
    return None


def quality_summary(root, sources, mode, note_report=None):
    """No overall pass score. Static validation never fills semantic fields."""
    checks = {name: {'status': 'not_run', 'basis': 'agent_declaration', 'findings': 0}
              for name in DIMENSIONS}
    host_status = 'not_run' if note_report is None or note_report.sha256 is None else (
        'completed_no_findings' if note_report.ok and not note_report.warnings else 'completed_with_findings')
    result = {'schema_version': 1, 'planned_route': 'local_handoff' if policy(sources) else 'legacy_or_other',
        'semantic_acceptance': 'not_certified', 'editorial_state': 'not_assessed',
        'structure_resources': {'status': host_status, 'basis': 'host_deterministic'},
        'quote_locations': {'status': ('completed_with_findings' if note_report and any('quote' in code for code in note_report.diagnostic_codes)
            else 'completed_no_findings') if mode == 'complete' and sources.get('output_checks', {}).get('complete_quotes')
            and note_report and note_report.sha256 else 'not_run', 'basis': 'host_location_only'},
        **checks}
    if not policy(sources):
        return result
    try:
        found = _latest(root, sources, mode)
        if found is None:
            return result
        value, report, data = found
        if note_report is None or report['output_sha256'] != note_report.sha256:
            result['editorial_state'] = 'stale'; return result
        # Validate one mode without requiring already-completed sibling outputs
        # in a partial-run sandbox. The full report was checked at save time.
        selected = {**value, 'reports': [report]}
        validate_review(json.dumps(selected, ensure_ascii=False).encode(), root, sources)
        findings = 0
        incomplete = False
        for name, check in report['checks'].items():
            count = len(check['findings']); findings += count
            incomplete |= check['status'] != 'completed' or bool(check['limitations'])
            result[name] = {'status': 'reported_' + check['status'], 'basis': 'agent_declaration',
                'findings': count, 'limitations': len(check['limitations']), 'scope': 'declared_only'}
        result['editorial_state'] = 'revision_required' if findings else 'review_incomplete' if incomplete else 'review_recorded'
    except (OSError, ValueError, KeyError, TypeError):
        result['editorial_state'] = 'invalid_or_stale_report'
    return result
