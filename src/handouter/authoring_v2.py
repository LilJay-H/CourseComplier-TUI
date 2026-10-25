"""Opt-in editorial contract. Agent declarations and Host results stay separate."""
import hashlib
import json
import math
import re

from .credentials import scan_credentials
from .source_refs import resolve_ref, source_hashes, source_registry

MODES = {'verbatim', 'full', 'deep', 'summary', 'complete'}
CHECKS = ['source_refs', 'mode_obligations', 'correction_closure']


def validate_contract(value, sources):
    fields = {'schema_version', 'task_key', 'record_path', 'resume_path', 'max_bytes',
              'optional', 'checks', 'task_modes', 'source_hashes'}
    modes = value.get('task_modes')
    hashes = value.get('source_hashes')
    if (set(value) != fields or value.get('checks') != CHECKS
            or not isinstance(modes, list) or not modes or len(modes) > 5
            or any(not isinstance(mode, str) or mode not in MODES for mode in modes)
            or len(set(modes)) != len(modes)
            or not set(sources.get('expected_outputs', {})) <= set(modes)
            or not isinstance(hashes, dict) or set(hashes) != set(source_registry(sources))
            or any(not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest)
                   for digest in hashes.values())):
        raise ValueError('AUTHORING_CONTRACT_INVALID')


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('AUTHORING_DUPLICATE_KEY')
            result[key] = value
        return result
    def constant(_):
        raise ValueError('AUTHORING_NONFINITE')
    try:
        value = json.loads(data.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
        stack = [(value, 0)]
        while stack:
            item, depth = stack.pop()
            if depth > 16:
                raise ValueError('AUTHORING_TOO_DEEP')
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError('AUTHORING_NONFINITE')
            if isinstance(item, dict):
                stack.extend((part, depth + 1) for part in item.values())
            elif isinstance(item, list):
                stack.extend((part, depth + 1) for part in item)
        return value
    except (UnicodeError, RecursionError, json.JSONDecodeError):
        raise ValueError('AUTHORING_JSON_INVALID') from None


def _text(value, limit=4096, *, empty=True):
    return isinstance(value, str) and len(value) <= limit and (empty or bool(value.strip()))


def _strings(value, allowed=None, limit=2000):
    return (isinstance(value, list) and len(value) <= limit
            and all(_text(item, 128, empty=False) for item in value)
            and len(set(value)) == len(value)
            and (allowed is None or set(value) <= set(allowed)))


def validate_record(data, sources, *, root):
    policy = sources['authoring']
    value = strict_json(data)
    fields = {'schema_version', 'task_key', 'units', 'facts', 'next_unit',
              'pending_corrections', 'completed_modes'}
    if (not isinstance(value, dict) or set(value) != fields
            or type(value['schema_version']) is not int or value['schema_version'] != 2
            or value['task_key'] != policy['task_key']):
        raise ValueError('AUTHORING_TASK_INVALID')
    if root is None:
        raise ValueError('AUTHORING_SOURCE_ROOT_REQUIRED')
    if source_hashes(root, sources) != policy['source_hashes']:
        raise ValueError('SOURCE_CHANGED')
    modes = set(policy['task_modes'])
    if (not _strings(value['completed_modes'], modes, 5)
            or not _strings(value['pending_corrections'], limit=200)
            or not isinstance(value['units'], list) or not 1 <= len(value['units']) <= 2000
            or not isinstance(value['facts'], list) or len(value['facts']) > 200):
        raise ValueError('AUTHORING_PROGRESS_INVALID')

    def refs(items, required):
        if not isinstance(items, list) or len(items) > 500 or (required and not items):
            raise ValueError('AUTHORING_REFS_REQUIRED')
        for ref in items:
            resolve_ref(root, sources, ref, expected_hashes=policy['source_hashes'])

    seen = set()
    for unit in value['units']:
        keys = {'id', 'source_refs', 'goal', 'visuals', 'text_needed', 'status', 'drafts', 'obligations'}
        if (not isinstance(unit, dict) or set(unit) != keys or not _text(unit['id'], 128, empty=False)
                or unit['id'] in seen or not _text(unit['status'], 16)
                or unit['status'] not in ('unread', 'read', 'draft', 'reviewed')
                or not all(_text(unit[key]) for key in ('goal', 'text_needed'))
                or not _strings(unit['visuals'], limit=500)
                or not isinstance(unit['drafts'], dict) or not set(unit['drafts']) <= modes
                or any(not _text(part, 65536) for part in unit['drafts'].values())
                or not isinstance(unit['obligations'], dict) or set(unit['obligations']) != modes):
            raise ValueError('AUTHORING_UNIT_INVALID')
        seen.add(unit['id'])
        refs(unit['source_refs'], unit['status'] != 'unread')
        for mode, obligation in unit['obligations'].items():
            if (not isinstance(obligation, dict) or set(obligation) != {'required', 'status', 'reason'}
                    or type(obligation['required']) is not bool
                    or not _text(obligation['status'], 16)
                    or obligation['status'] not in ('pending', 'done', 'not_required')
                    or not _text(obligation['reason'])
                    or (not obligation['required'] and (mode != 'summary'
                        or obligation['status'] != 'not_required' or not obligation['reason'].strip()))
                    or (obligation['required'] and obligation['status'] == 'not_required')):
                raise ValueError('AUTHORING_OBLIGATION_INVALID')
    if value['next_unit'] is not None and (not isinstance(value['next_unit'], str) or value['next_unit'] not in seen):
        raise ValueError('AUTHORING_NEXT_UNIT_INVALID')
    facts = {}
    for fact in value['facts']:
        keys = {'id', 'kind', 'statement', 'source_refs', 'scope', 'supersedes',
                'affected_modes', 'resolution', 'propagated_modes'}
        if (not isinstance(fact, dict) or set(fact) != keys or not _text(fact['id'], 128, empty=False)
                or fact['id'] in facts or not _text(fact['kind'], 32)
                or fact['kind'] not in ('course_action', 'quantity', 'condition', 'definition', 'formula', 'correction')
                or not _text(fact['statement'], empty=False) or not _text(fact['scope'], empty=False)
                or not _strings(fact['supersedes'], limit=200)
                or not _strings(fact['affected_modes'], modes, 5) or not fact['affected_modes']
                or not _strings(fact['propagated_modes'], fact['affected_modes'], 5)
                or not _text(fact['resolution'], 16) or fact['resolution'] not in ('applied', 'unresolved')):
            raise ValueError('AUTHORING_FACT_INVALID')
        refs(fact['source_refs'], True)
        facts[fact['id']] = fact
    if any(not set(fact['supersedes']) <= set(facts) for fact in facts.values()):
        raise ValueError('AUTHORING_CORRECTION_TARGET_MISSING')
    for fact in facts.values():
        # At most 200 facts; explicit ancestry traversal avoids recursive limits.
        todo = list(fact['supersedes'])
        visited = set()
        while todo:
            target = todo.pop()
            if target == fact['id']:
                raise ValueError('AUTHORING_CORRECTION_CYCLE')
            if target not in visited:
                visited.add(target)
                todo.extend(facts[target]['supersedes'])
    pending = {fact['id'] for fact in facts.values()
               if set(fact['affected_modes']) - set(fact['propagated_modes'])}
    if set(value['pending_corrections']) != pending:
        raise ValueError('AUTHORING_PENDING_MISMATCH')
    if scan_credentials(data.decode('utf-8')).errors:
        raise ValueError('AUTHORING_PRIVATE_DATA')
    return value


def completion_errors(record, mode):
    """Host checks declared obligations only, not undeclared omissions or prose."""
    if mode not in record['completed_modes']:
        return ['AUTHORING_MODE_INCOMPLETE']
    errors = []
    required = [unit for unit in record['units'] if unit['obligations'][mode]['required']]
    if not required:
        errors.append('AUTHORING_REQUIRED_UNIT_MISSING')
    for unit in required:
        if (unit['status'] == 'unread' or not unit['source_refs']
                or unit['obligations'][mode]['status'] != 'done'
                or not unit['drafts'].get(mode, '').strip()):
            errors.append('AUTHORING_UNIT_INCOMPLETE')
    if any(mode in fact['affected_modes'] and mode not in fact['propagated_modes'] for fact in record['facts']):
        errors.append('AUTHORING_CORRECTION_PENDING')
    if set(record['completed_modes']) == set(record['units'][0]['obligations']) and record['next_unit'] is not None:
        errors.append('AUTHORING_FALSE_CLOSURE')
    return sorted(set(errors))


def plan_hash(record):
    """Conservative plan identity: edits after partial publication require refresh."""
    units = [{key: unit[key] for key in ('id', 'source_refs', 'goal', 'visuals', 'text_needed')}
             | {'required': {mode: ob['required'] for mode, ob in unit['obligations'].items()}}
             for unit in record['units']]
    facts = [{key: value for key, value in fact.items() if key != 'propagated_modes'} for fact in record['facts']]
    return hashlib.sha256(json.dumps({'units': units, 'facts': facts}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


INSTRUCTIONS = """
## Authoring v2 候选契约（优先于旧 v1 编辑记录示例）

sources.authoring.schema_version=2 时必须返回 authoring/progress.json。这是编辑声明，不是 Host 检查结果。
顶层仅允许 schema_version=2、task_key（沿用整个任务）、units、facts、next_unit、pending_corrections、completed_modes。
完整任务模式由 authoring.task_modes 决定；expected_outputs 仅为本次待写子集。续作保留之前的单元和共享事实。
每个 unit 仅含 id、source_refs、goal、visuals（字符串列表）、text_needed、status（unread/read/draft/reviewed）、drafts（模式到正文）、obligations。
obligations 对每个 task_modes 模式含 {required:布尔,status:pending/done/not_required,reason:字符串}。
非 Summary 单元均 required=true；Summary 可有理由地省略支持细节，但须保留至少一个核心单元及当前行动/条件。
声明模式完成前，必需单元已读且引用有效、义务 done、drafts 有正文，并生成本次指定成果。next_unit=null 本身不代表完成。
source_refs 接受 transcript:Lx-Ly，或 {kind:transcript_lines,source_id:transcript,start:1,end:2}（1基闭区间）；
transcript_span 是零基 Unicode code point [start,end)，UTF-8-sig 解码，不归一化换行/组合字符；
真实 segment 使用 {kind:segment,source_id:segments,id:真实ID}，课件事件使用 {kind:slide_occurrence,source_id:slides,id:真实occurrence_id}。
不得提供路径或URL，不造时间、段ID；generated_note 不能成为原话证据。来源变动需刷新任务。
facts 只登记行动、数字/单位、条件/否定、定义、公式前提及明确更正；普通叙述不逐句结构化。可以为空但不能隐瞒已发现更正。
每个 fact 仅含 id、kind（course_action/quantity/condition/definition/formula/correction）、statement、source_refs、scope、supersedes（旧fact ID列表）、affected_modes、resolution（applied/unresolved）、propagated_modes。
更正须回源确认同对象/范围，supersedes 不得成环。历史原说法可保留并注明作废，不以旧词出现即判错。
来源未决可 resolution=unresolved；在相应模式准确保留未决和已知边界后才登记 propagated_modes。不要为了完成制造答案。
pending_corrections 必须列出 affected_modes 尚未全部 propagated 的 fact ID；阻断尚未传播的模式。
记录最多512KiB、单元草稿每模式最多65536字符，不写Host结论。引用/声明检查不证明正文含义、遗漏率、音频或语义准确。
已经发布部分成果后修改计划/共享事实需显式刷新新任务；不修改旧成果。仅完成状态、草稿和传播进度可续写。
CLI 自动校验；manual/Web 返回时，增强路径须连同独立 JSON 记录导入。普通 Markdown 单独导入不具备 v2 检查能力。
"""
