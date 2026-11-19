"""Task-bound editorial records; progress claims are not semantic evidence."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .credentials import scan_credentials
from .safe_io import publish_bytes, read_bytes, safe_path

RECORD_PATH = 'authoring/progress.json'
RESUME_PATH = 'handoff/authoring-resume.json'
MAX_RECORD_BYTES = 512 * 1024


def make_contract(sources: dict, material_fingerprint: list, *, version=1, root=None) -> dict:
    if type(version) is not int or version not in (1, 2):
        raise ValueError('不支持的 authoring 版本')
    extension = {}
    if version == 2:
        from .source_refs import source_hashes
        extension = {'checks': ['source_refs', 'mode_obligations', 'correction_closure'],
                     'task_modes': list(sources['expected_outputs']),
                     'source_hashes': source_hashes(root, sources)}
    binding = json.dumps({'sources': sources, 'materials': material_fingerprint},
                         ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')
    if version == 2:
        binding += json.dumps(extension, sort_keys=True).encode('utf-8')
    return {'schema_version': version, 'task_key': hashlib.sha256(binding).hexdigest(),
            'record_path': RECORD_PATH, 'resume_path': RESUME_PATH,
            'max_bytes': MAX_RECORD_BYTES, 'optional': version == 1, **extension}


def contract(sources: dict) -> dict | None:
    value = sources.get('authoring')
    if value is None:
        return None
    if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
            or value.get('schema_version') not in (1, 2)
            or not isinstance(value.get('task_key'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', value['task_key'])
            or value.get('record_path') != RECORD_PATH or value.get('resume_path') != RESUME_PATH
            or type(value.get('max_bytes')) is not int or value.get('max_bytes') != MAX_RECORD_BYTES
            or value.get('optional') is not (value['schema_version'] == 1)):
        raise ValueError('无效 authoring 协议；请刷新交接任务')
    if value['schema_version'] == 2:
        from .authoring_v2 import validate_contract
        validate_contract(value, sources)
    return value


def _strings(value, limit: int = 2000) -> bool:
    return (isinstance(value, list) and len(value) <= limit
            and all(isinstance(item, str) and len(item) <= 4096 for item in value))


def validate_progress(data: bytes, sources: dict, *, root=None) -> dict:
    policy = contract(sources)
    if policy is None:
        raise ValueError('本任务未授权编辑记录')
    if len(data) > MAX_RECORD_BYTES:
        raise ValueError('编辑记录过大；按单元精简，不复制整套输入')
    if policy['schema_version'] == 2:
        from .authoring_v2 import validate_record
        return validate_record(data, sources, root=root)
    try:
        text = data.decode('utf-8')
        value = json.loads(text)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError('编辑记录必须为 UTF-8 JSON') from exc
    fields = {'schema_version', 'task_key', 'units', 'next_unit', 'pending_corrections', 'completed_modes'}
    if (not isinstance(value, dict) or set(value) != fields
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['task_key'] != policy['task_key']):
        raise ValueError('编辑记录字段或任务身份不匹配')
    modes = set(sources.get('expected_outputs', {}))
    if (not _strings(value['completed_modes'], 5)
            or not set(value['completed_modes']) <= modes
            or not _strings(value['pending_corrections'], 200)
            or not (value['next_unit'] is None or isinstance(value['next_unit'], str)
                    and len(value['next_unit']) <= 256)
            or not isinstance(value['units'], list) or len(value['units']) > 2000):
        raise ValueError('编辑记录的进度字段无效')
    seen = set()
    unit_fields = {'id', 'source_refs', 'goal', 'visuals', 'text_needed', 'status', 'drafts'}
    for unit in value['units']:
        if (not isinstance(unit, dict) or set(unit) != unit_fields
                or not isinstance(unit['id'], str) or not unit['id'] or len(unit['id']) > 128
                or unit['id'] in seen or not _strings(unit['source_refs'], 500)
                or not _strings(unit['visuals'], 500)
                or any(not isinstance(unit[key], str) or len(unit[key]) > 4096
                       for key in ('goal', 'text_needed'))
                or unit['status'] not in ('unread', 'read', 'draft', 'reviewed')
                or not isinstance(unit['drafts'], dict) or not set(unit['drafts']) <= modes
                or any(not isinstance(part, str) or len(part) > 65536 for part in unit['drafts'].values())):
            raise ValueError('编辑记录包含无效或重复的教学单元')
        if unit['status'] != 'unread' and not unit['source_refs']:
            raise ValueError('已处理单元必须保留具体来源定位；不能用空引用声称已读')
        seen.add(unit['id'])
    if len(set(value['completed_modes'])) != len(value['completed_modes']):
        raise ValueError('completed_modes 不允许重复模式')
    if value['next_unit'] is not None and value['next_unit'] not in seen:
        raise ValueError('下一单元必须在 units 中登记；未读单元可使用 unread 状态')
    if modes and set(value['completed_modes']) == modes and value['next_unit'] is not None:
        raise ValueError('整稿全部就绪时 next_unit 必须为 null，不能同时声明仍有下一单元')
    if scan_credentials(text).errors:
        raise ValueError('编辑记录疑似含私有凭证，拒绝保存')
    return value


def latest_progress(root: Path, sources: dict) -> bytes | None:
    policy = contract(sources)
    if policy is None:
        return None
    directory = safe_path(root, 'authoring/' + policy['task_key'], missing=True)
    if not directory.is_dir():
        return None
    for path in sorted(directory.glob('progress-*.json'), reverse=True):
        if not re.fullmatch(r'progress-\d{4}\.json', path.name):
            continue
        checked = safe_path(root, path.relative_to(root).as_posix())
        if checked.stat().st_size > MAX_RECORD_BYTES:
            raise ValueError('历史编辑记录超过大小限制')
        data = read_bytes(root, checked.relative_to(root).as_posix())
        validate_progress(data, sources, root=root)
        return data
    return None


def save_progress(root: Path, sources: dict, data: bytes) -> Path:
    value = validate_progress(data, sources, root=root)
    if contract(sources)['schema_version'] == 2:
        from .authoring_v2 import plan_hash, completion_errors
        published = [mode for mode, path in sources['expected_outputs'].items()
                     if safe_path(root, path, role='note', missing=True).exists()]
        previous = latest_progress(root, sources)
        if published:
            if previous is None or plan_hash(value) != plan_hash(validate_progress(previous, sources, root=root)):
                raise ValueError('AUTHORING_PLAN_CHANGED_REFRESH_REQUIRED')
            if any(completion_errors(value, mode) for mode in published):
                raise ValueError('AUTHORING_PUBLISHED_MODE_REGRESSION')
    prefix = 'authoring/' + contract(sources)['task_key']
    safe_path(root, prefix, missing=True).mkdir(parents=True, exist_ok=True)
    safe_path(root, prefix)
    for number in range(1, 10000):
        relative = f'{prefix}/progress-{number:04d}.json'
        if not safe_path(root, relative, missing=True).exists():
            try:
                return publish_bytes(root, relative, data)
            except FileExistsError:
                continue
    raise RuntimeError('编辑记录版本过多')


def published_errors(root: Path, sources: dict, mode: str) -> list[str]:
    policy = contract(sources)
    if not policy or policy['schema_version'] == 1:
        return []
    data = latest_progress(root, sources)
    if data is None:
        return ['AUTHORING_RECORD_REQUIRED']
    from .authoring_v2 import completion_errors
    errors = completion_errors(validate_progress(data, sources, root=root), mode)
    digest = hashlib.sha256(read_bytes(root, sources['expected_outputs'][mode], role='note')).hexdigest()
    relative = f"authoring/{policy['task_key']}/accepted-{mode}-{digest}.json"
    try:
        from .authoring_v2 import strict_json
        receipt = strict_json(read_bytes(root, relative))
        fields = {'task_key', 'mode', 'output_sha256', 'record_path', 'record_sha256'}
        if sources.get('authoring_stages'):
            fields.add('stage_plan')
        if sources.get('composer'):
            fields.add('composition_sha256')
        if (not isinstance(receipt, dict) or set(receipt) != fields
                or receipt['task_key'] != policy['task_key'] or receipt['mode'] != mode
                or receipt['output_sha256'] != digest or not isinstance(receipt['record_path'], str)
                or not re.fullmatch('authoring/' + policy['task_key'] + r'/progress-\d{4}\.json', receipt['record_path'])):
            raise ValueError('AUTHORING_RECEIPT_INVALID')
        accepted = read_bytes(root, receipt['record_path'])
        if hashlib.sha256(accepted).hexdigest() != receipt['record_sha256']:
            raise ValueError('AUTHORING_RECEIPT_CHANGED')
        errors.extend(completion_errors(validate_progress(accepted, sources, root=root), mode))
        if sources.get('authoring_stages'):
            from .authoring_stages import latest_plan
            from .authoring_v2 import plan_hash
            plan = latest_plan(root, sources)
            if (plan is None or receipt['stage_plan'] != {'revision': plan['revision'], 'hash': plan['plan_hash']}
                    or plan_hash(validate_progress(accepted, sources, root=root)) != plan_hash(plan['record'])):
                raise ValueError('STAGE_ACCEPTANCE_PLAN_CHANGED')
            if sources.get('composer'):
                from .composition_store import checked_composition
                from .authoring_stages import encoded, sha
                composition = checked_composition(root, sources,
                    read_bytes(root, sources['expected_outputs'][mode], role='note'), mode, plan)
                if receipt['composition_sha256'] != sha(encoded(composition)):
                    raise ValueError('COMPOSITION_RECEIPT_CHANGED')
    except (OSError, ValueError):
        errors.append('AUTHORING_ACCEPTANCE_MISSING_OR_CHANGED')
    return errors


def accept_output(root: Path, sources: dict, mode: str, data: bytes, record_path: str, *, stage_plan=None):
    """Immutable Host receipt binds actual candidate bytes to checked declarations."""
    policy = contract(sources)
    if not policy or policy['schema_version'] == 1:
        return
    from .authoring_v2 import completion_errors
    record = read_bytes(root, record_path)
    if completion_errors(validate_progress(record, sources, root=root), mode):
        raise ValueError('AUTHORING_MODE_INCOMPLETE')
    digest = hashlib.sha256(data).hexdigest()
    receipt = {'task_key': policy['task_key'], 'mode': mode, 'output_sha256': digest,
               'record_path': record_path, 'record_sha256': hashlib.sha256(record).hexdigest()}
    if sources.get('authoring_stages'):
        from .authoring_stages import latest_plan
        from .authoring_v2 import plan_hash
        plan = latest_plan(root, sources)
        if (plan is None or stage_plan != {'revision': plan['revision'], 'hash': plan['plan_hash']}
                or plan_hash(validate_progress(record, sources, root=root)) != plan_hash(plan['record'])):
            raise ValueError('STAGE_ACCEPTANCE_PLAN_INVALID')
        receipt['stage_plan'] = stage_plan
        if sources.get('composer'):
            from .composition_store import checked_composition
            from .authoring_stages import encoded, sha
            receipt['composition_sha256'] = sha(encoded(checked_composition(root, sources, data, mode, plan)))
    relative = f"authoring/{policy['task_key']}/accepted-{mode}-{digest}.json"
    encoded = json.dumps(receipt, sort_keys=True).encode('utf-8')
    try:
        publish_bytes(root, relative, encoded)
    except FileExistsError:
        # An interrupted publication may have an older equivalent checked record.
        from .authoring_v2 import strict_json, plan_hash
        previous = strict_json(read_bytes(root, relative))
        if (not isinstance(previous, dict) or set(previous) != set(receipt)
                or any(previous[key] != receipt[key] for key in ('task_key', 'mode', 'output_sha256'))
                or previous.get('stage_plan') != receipt.get('stage_plan')
                or previous.get('composition_sha256') != receipt.get('composition_sha256')
                or not isinstance(previous['record_path'], str)
                or not re.fullmatch('authoring/' + policy['task_key'] + r'/progress-\d{4}\.json', previous['record_path'])):
            raise ValueError('AUTHORING_RECEIPT_CONFLICT')
        old = read_bytes(root, previous['record_path'])
        checked = validate_progress(old, sources, root=root)
        if (hashlib.sha256(old).hexdigest() != previous['record_sha256']
                or completion_errors(checked, mode)
                or plan_hash(checked) != plan_hash(validate_progress(record, sources, root=root))):
            raise ValueError('AUTHORING_RECEIPT_CONFLICT')
