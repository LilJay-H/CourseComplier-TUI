"""Shared task planning, safe returned-note import and HTML-only completion."""
from dataclasses import dataclass, field, asdict
import hashlib
import json
from pathlib import Path
import re
import tempfile

from .agents.base import expected_outputs, read_handoff
from .agents.manual import selected_files
from .safe_io import safe_path, read_bytes, read_json, publish_bytes, workspace_lock, locked_workspace
from .stage_cache import asset_fingerprint
from .validation import validate_workspace, validate_note_output


@dataclass(frozen=True)
class TaskPlan:
    action: str
    task_id: str = ''
    completed: dict = field(default_factory=dict)
    pending: dict = field(default_factory=dict)
    errors: tuple = ()
    warnings: tuple = ()
    html: dict = field(default_factory=dict)
    authoring_stage: dict | None = None
    quality: dict = field(default_factory=dict)

    def as_dict(self):
        return asdict(self)


def task_identity(root: Path, sources: dict) -> str:
    digest = hashlib.sha256()
    skill = sources.get('skill', {})
    for relative in ['handoff/PROMPT.md', 'handoff/sources.json', skill['entry'], *skill.get('modules', [])]:
        digest.update(relative.encode() + b'\0' + read_bytes(root, relative))
    return digest.hexdigest()[:16]


def plan_task(workspace, *, selection=None, input_fingerprint=None) -> TaskPlan:
    from .organization import resolve_workspace
    root = resolve_workspace(workspace)
    if not root.is_dir():
        return TaskPlan('invalid', errors=('讲次目录不存在；新课程请使用 run',))
    try:
        with workspace_lock(root):
            if (root / '.refresh-transaction').exists():
                return TaskPlan('recovery_required', errors=('Prompt 事务待恢复；使用 continue 重入恢复',))
            report = validate_workspace(root)
            if not report.ok:
                return TaskPlan('invalid', errors=report.errors)
            root, sources, _ = read_handoff(root)
            identity = task_identity(root, sources)
            manifest = read_json(root, 'manifest.json')
            if input_fingerprint is not None and input_fingerprint != manifest.get('input_fingerprint'):
                return TaskPlan('refresh_required', identity, errors=(
                    '输入与已有任务不一致或旧任务缺少指纹；不同材料请使用新讲次 ID，继续旧任务请显式选择工作区。',))
            if selection:
                current = {**sources['options'], 'modes': sources.get('modes', [sources.get('mode')])}
                if any(current.get(key) != value for key, value in selection.items()):
                    return TaskPlan('refresh_required', identity, errors=('配置改变；请显式刷新 Prompt 创建任务版本',))
            warnings = []
            from .handoff import default_skill_path
            if sources.get('skill', {}).get('rules_version') != 'custom':
                from .local_agent_workflow import MODULE as local_workflow_module
                if (not sources.get('authoring_stages')
                        and (sources.get('revision') or {}).get('action') != 'illustrate'
                        and local_workflow_module not in sources['skill'].get('modules', [])):
                    warnings.append('本机来源优先工作流已更新；当前任务继续使用旧快照，使用 prompt 显式刷新可采用新规则')
                installed = default_skill_path().parent
                for relative in [sources['skill']['entry'], *sources['skill'].get('modules', [])]:
                    local = relative.removeprefix('handoff/skill/')
                    try:
                        if local == 'references/execution/authoring-v2.md':
                            from .authoring_v2 import INSTRUCTIONS
                            current_rule = INSTRUCTIONS.encode('utf-8')
                        elif local == 'references/execution/stages.md':
                            from .authoring_stages import INSTRUCTIONS
                            current_rule = INSTRUCTIONS.encode('utf-8')
                        elif local == 'references/execution/composer.md':
                            from .composer import INSTRUCTIONS
                            current_rule = INSTRUCTIONS.encode('utf-8')
                        elif local == 'references/execution/editorial-review.md':
                            from .editorial_review import instructions
                            current_rule = instructions().encode('utf-8')
                        elif relative == local_workflow_module:
                            from .local_agent_workflow import INSTRUCTIONS
                            current_rule = INSTRUCTIONS.encode('utf-8')
                        else:
                            current_rule = read_bytes(installed, local)
                        if read_bytes(root, relative) != current_rule:
                            warnings.append('本机 Skill 已更新；当前任务继续使用旧快照，使用 prompt 显式刷新可采用新规则')
                            break
                    except (OSError, ValueError):
                        warnings.append('无法比较当前 Skill；旧任务快照保持不变')
                        break
            outputs = expected_outputs(sources)
            completed, pending, note_hashes, errors = {}, {}, {}, []
            from .editorial_review import quality_summary
            quality = {}
            for mode, relative in outputs.items():
                path = safe_path(root, relative, role='note', missing=True)
                if not path.exists():
                    pending[mode] = relative
                    quality[mode] = quality_summary(root, sources, mode)
                    continue
                report = validate_note_output(root, output=relative)
                quality[mode] = quality_summary(root, sources, mode, report)
                from .authoring import published_errors
                errors.extend(f'{mode}: {message}' for message in published_errors(root, sources, mode))
                if not report.ok:
                    errors.extend(f'{mode}: {message}' for message in report.errors)
                else:
                    completed[mode] = relative
                    note_hashes[mode] = report.sha256
                    warnings.extend(report.warnings)
            delivery = read_json(root, 'state.json').get('html_delivery', {})
            if errors:
                return TaskPlan('invalid', identity, completed, pending, tuple(errors), tuple(warnings), delivery, quality=quality)
            if pending:
                from .authoring_stages import stage_status
                return TaskPlan('partial' if completed else 'generate', identity, completed, pending,
                                warnings=tuple(warnings), html=delivery, authoring_stage=stage_status(root, sources), quality=quality)
            from .delivery import html_requested, publication_current
            html_ready = True
            for mode in completed:
                if not html_requested(delivery, mode):
                    continue
                try:
                    relative = delivery['outputs'][mode]
                    if not relative.startswith('notes/') or not relative.endswith('.html'):
                        raise ValueError('HTML 路径无效')
                    data = read_bytes(root, relative)
                    if (hashlib.sha256(data).hexdigest() != delivery['hashes'][mode]
                            or delivery['note_hashes'][mode] != note_hashes[mode]):
                        html_ready = False
                except (OSError, ValueError, KeyError, TypeError):
                    html_ready = False
            action = ('complete' if publication_current(root) else 'publish_only') if html_ready else 'html_only'
            return TaskPlan(action, identity, completed, pending,
                            warnings=tuple(warnings), html=delivery, quality=quality)
    except RuntimeError as exc:
        if '另一任务占用' in str(exc):
            return TaskPlan('busy', errors=('讲次正在运行；等待当前任务完成',))
        return TaskPlan('invalid', errors=('任务运行依赖不可用；请运行 doctor 检查',))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return TaskPlan('invalid', errors=('任务结构无效；请检查材料和控制文件，未改动已有成果',))


def _save_state(root: Path, state: dict):
    publish_bytes(root, 'state.json', (json.dumps(state, ensure_ascii=False, indent=2) + '\n').encode(), replace=True)


def _publish_results(root: Path) -> dict:
    from .delivery import publish_deliverables
    result = publish_deliverables(root)
    if result['applicable']:
        state = read_json(root, 'state.json')
        state['publication'] = result
        _save_state(root, state)
    return result


@locked_workspace
def record_html_exports(workspace, pairs) -> None:
    """Connect the generic H/export-html action to the current task's result view."""
    root, sources, _ = read_handoff(workspace)
    expected = expected_outputs(sources)
    state = read_json(root, 'state.json')
    delivery = state.get('html_delivery', {})
    for key in ('outputs', 'hashes', 'note_hashes'):
        delivery.setdefault(key, {})
    matched = False
    for note, html in pairs:
        relative = note.relative_to(root).as_posix()
        for mode, target in expected.items():
            if relative != target:
                continue
            matched = True
            delivery.setdefault('requested', {})[mode] = True
            report = validate_note_output(root, output=relative)
            if not report.ok:
                continue
            html_relative = html.relative_to(root).as_posix()
            delivery['outputs'][mode] = html_relative
            delivery['hashes'][mode] = hashlib.sha256(read_bytes(root, html_relative)).hexdigest()
            delivery['note_hashes'][mode] = report.sha256
    if not matched:
        # Exporting an independently named reading copy does not change this
        # task's requested formats, receipts or existing publication state.
        return
    delivery.update(status='recorded', errors=[])
    state['html_delivery'] = delivery
    _save_state(root, state)
    publication = _publish_results(root)
    if publication['errors']:
        raise ValueError('HTML 已保存，但成果复制失败；请查看 task-status 并重试 continue')


@locked_workspace
def retry_html(workspace, *, modes=None, requested_only: bool = False) -> dict:
    """Explicit retry opts selected modes in; automatic callers preserve policy.

    Automatic continuation must pass requested_only=True. Without a policy,
    all modes remain requested, preserving the legacy default.
    """
    root, sources, _ = read_handoff(workspace)
    outputs = expected_outputs(sources)
    from .delivery import html_requested
    from .doctor import require_features
    from .html_export import export_html
    state = read_json(root, 'state.json')
    previous = state.get('html_delivery', {})
    delivery = {**previous, 'status': 'ready', 'outputs': dict(previous.get('outputs', {})),
                'hashes': dict(previous.get('hashes', {})), 'note_hashes': dict(previous.get('note_hashes', {})),
                'requested': dict(previous.get('requested', {})), 'errors': []}
    selected = {mode: relative for mode, relative in outputs.items()
                if (modes is None or mode in modes)
                and (not requested_only or html_requested(previous, mode))}
    for mode in selected:
        delivery['requested'][mode] = True
    state['html_delivery'] = delivery
    _save_state(root, state)
    if not selected:
        if not any(html_requested(delivery, mode) for mode in outputs):
            delivery['status'] = 'not_requested'
            _save_state(root, state)
        delivery['publication'] = _publish_results(root)
        if delivery['publication']['errors']:
            delivery.update(status='failed', errors=['成果目录发布失败；请检查冲突后 continue'])
        return delivery
    try:
        require_features(['core', 'html'])
    except RuntimeError:
        delivery.update(status='failed', errors=[
            'HTML 依赖不可用；Markdown 已保留。运行 doctor --require core html，补齐依赖后 retry-html。'])
        state['html_delivery'] = delivery
        _save_state(root, state)
        delivery['publication'] = _publish_results(root)
        return delivery
    for mode, relative in selected.items():
        report = validate_note_output(root, output=relative)
        if not report.ok:
            delivery['errors'].append(f'{mode}: Markdown 尚未完成或校验失败；保留现有成果')
            continue
        try:
            old = previous.get('outputs', {}).get(mode)
            if old and previous.get('note_hashes', {}).get(mode) == report.sha256:
                try:
                    old_data = read_bytes(root, old) if old.startswith('notes/') and old.endswith('.html') else b''
                except (OSError, ValueError):
                    old_data = b''
                if old_data and hashlib.sha256(old_data).hexdigest() == previous.get('hashes', {}).get(mode):
                    for key in ('outputs', 'hashes', 'note_hashes'):
                        delivery[key][mode] = previous[key][mode]
                    continue
            output = export_html(safe_path(root, relative, role='note'))[0]
            html_relative = output.relative_to(root).as_posix()
            delivery['outputs'][mode] = html_relative
            delivery['hashes'][mode] = hashlib.sha256(read_bytes(root, html_relative)).hexdigest()
            delivery['note_hashes'][mode] = report.sha256
        except (OSError, ValueError, RuntimeError):
            delivery['errors'].append(f'{mode}: HTML 导出失败；Markdown 已保留，可 retry-html 重试')
    if delivery['errors']:
        delivery['status'] = 'failed'
    state['html_delivery'] = delivery
    _save_state(root, state)
    delivery['publication'] = _publish_results(root)
    if delivery['publication']['errors']:
        delivery['status'] = 'failed'
        delivery['errors'].append('内部成果已保存，但成果目录发布失败；请检查冲突后 continue')
    return delivery


@locked_workspace
def import_note(workspace, returned, *, mode: str, task_id: str, html: bool = True,
                authoring_record: str | Path | None = None) -> dict:
    root, sources, _ = read_handoff(workspace)
    if sources.get('authoring_stages'):
        raise ValueError('STAGE_TRANSPORT_UNSUPPORTED：分阶段任务需通过 CLI runner 续作；不把普通返回稿当阶段成果')
    plan = plan_task(root)
    if task_id != plan.task_id or not task_id:
        raise ValueError('返回稿任务 ID 不匹配；请用 task-status 确认讲次和任务')
    outputs = expected_outputs(sources)
    if mode not in outputs:
        raise ValueError('该模式不属于本次任务')
    relative = outputs[mode]
    target = safe_path(root, relative, role='note', missing=True)
    if target.exists():
        raise FileExistsError('该模式成果已存在；请显式刷新新任务后导入，不能覆盖旧成果')
    source = Path(returned).expanduser().absolute()
    if source.suffix.lower() != '.md':
        raise ValueError('网页返回成果需为 Markdown .md 文件')
    data = read_bytes(source.parent, source.name)
    if len(data) > 4 * 1024 * 1024:
        raise ValueError('返回稿超过 4 MiB，请分讲次处理')
    text = data.decode('utf-8-sig')
    from .authoring import contract, MAX_RECORD_BYTES, validate_progress, save_progress
    policy = contract(sources)
    record_data = None
    if policy and policy['schema_version'] == 2:
        if authoring_record is None:
            raise ValueError('AUTHORING_RECORD_REQUIRED：增强任务须同时导入独立编辑记录')
        record_file = Path(authoring_record).expanduser().absolute()
        if safe_path(record_file.parent, record_file.name).stat().st_size > MAX_RECORD_BYTES:
            raise ValueError('编辑记录过大')
        record_data = read_bytes(record_file.parent, record_file.name)
        progress = validate_progress(record_data, sources, root=root)
        from .authoring_v2 import completion_errors
        gate = completion_errors(progress, mode)
        if gate:
            save_progress(root, sources, record_data)
            raise ValueError('返回稿仍未完成：' + ', '.join(gate))
    from .credentials import scan_credentials
    if scan_credentials(text).errors:
        raise ValueError('返回稿包含高置信度凭证（内容已隐藏）')
    from .resources import has_pending_images
    if has_pending_images(text):
        # Keep pending material separately; it never occupies the final target.
        pending_dir = safe_path(root, 'handoff/returned', missing=True)
        pending_dir.mkdir(exist_ok=True)
        import uuid
        draft = 'handoff/returned/' + uuid.uuid4().hex + '.md'
        publish_bytes(root, draft, data)
        return {'status': 'needs_images', 'draft': draft, 'mode': mode,
                'next_step': '补齐真实图片后重新提交；当前不算完成嵌图', 'semantic_review': 'required'}
    with tempfile.TemporaryDirectory(prefix='handouter-import-') as temporary:
        task = Path(temporary).resolve()
        snapshot = selected_files(root, sources)
        snapshot['handoff/sources.json'] = json.dumps(sources, ensure_ascii=False).encode()
        snapshot['state.json'] = b'{}'
        for name, content in snapshot.items():
            path = task / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (task / relative).parent.mkdir(parents=True, exist_ok=True)
        (task / relative).write_bytes(data)
        report = validate_note_output(task, output=relative)
        if not report.ok:
            raise ValueError('返回稿未通过校验: ' + '; '.join(report.errors))
    if record_data is not None:
        record_path = save_progress(root, sources, record_data).relative_to(root).as_posix()
        from .authoring import accept_output
        accept_output(root, sources, mode, data, record_path)
    publish_bytes(root, relative, data)
    state = read_json(root, 'state.json')
    state['agent_output'] = {'status': 'partial' if len(plan.pending) > 1 else 'validated',
                             'outputs': outputs, 'semantic_review': 'required', 'source': 'web_import'}
    delivery = state.setdefault('html_delivery', {})
    delivery.setdefault('requested', {})[mode] = html
    if not html:
        from .delivery import html_requested
        if not any(html_requested(delivery, name) for name in outputs):
            delivery.update(status='not_requested', errors=[])
    _save_state(root, state)
    delivery = retry_html(root, modes=[mode]) if html else {
        **delivery, 'status': 'not_requested', 'errors': []}
    return {'status': 'imported', 'output': relative, 'task_id': task_id, 'mode': mode,
            'html_delivery': delivery, 'publication': delivery.get('publication') or _publish_results(root),
            'semantic_review': 'required'}


def inspect_cache(output_dir) -> dict:
    from .organization import cache_root_for
    root = cache_root_for(output_dir)
    entries = []
    cache = safe_path(root, '.handouter-cache', missing=True)
    if cache.exists():
        for path in sorted(cache.glob('*/*')):
            relative = path.relative_to(root).as_posix()
            safe_path(root, relative)
            if not path.is_dir():
                continue
            files = [p for p in path.rglob('*') if p.is_file()]
            for file in files:
                safe_path(root, file.relative_to(root).as_posix())
            stages = {}
            if (path / 'stages.json').exists():
                record = read_json(root, relative + '/stages.json')
                stages = {key: value.get('status', 'unknown') for key, value in record.items()}
            entries.append({'entry': path.relative_to(cache).as_posix(), 'stages': stages,
                            'files': len(files), 'bytes': sum(p.stat().st_size for p in files)})
    return {'entries': entries, 'deleted': False,
            'instruction': 'cache-clean OUTPUT --entry LECTURE/HASH 先预览；加 --confirm 才删除该精确缓存，不影响正式成果'}


def clean_cache(output_dir, *, entry: str, confirm: bool = False) -> dict:
    from .workspace import validate_lecture_id
    from .safe_io import relative_path
    import shutil
    parts = relative_path(entry).split('/')
    if len(parts) != 2 or not re.fullmatch('[a-f0-9]{64}', parts[1]):
        raise ValueError('只接受 cache-status 列出的 lecture-id/hash 精确条目')
    validate_lecture_id(parts[0])
    from .organization import cache_root_for, verify_disposable_cache
    root = cache_root_for(output_dir)
    with workspace_lock(root):
        target = safe_path(root, '.handouter-cache/' + entry)
        for path in target.rglob('*'):
            safe_path(root, path.relative_to(root).as_posix())
        if confirm:
            verify_disposable_cache(root, target)
            shutil.rmtree(target)
        return {'entry': entry, 'deleted': confirm, 'preview': not confirm, 'formal_outputs_untouched': True}
