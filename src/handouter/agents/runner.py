"""Run supported CLI agents and validate their outputs."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import hashlib
from pathlib import Path
from typing import Any

from . import claude, codex
from .base import AgentAvailability, AgentRunResult, expected_outputs, read_handoff, scope_pending
from ..validation import validate_note_output, validate_workspace
from ..workspace import read_json, sha256_file
from ..safe_io import workspace_lock, safe_path, read_bytes, publish_bytes, sha256_file
from .manual import selected_files
from ..authoring import contract, RECORD_PATH, MAX_RECORD_BYTES, validate_progress, save_progress, published_errors, accept_output


_ADAPTERS = {
    "codex": codex,
    "claude": claude,
}

CLI_FOOTER = '\n输入与控制文件只读；只写指定 Markdown、sources.authoring 和 sources.editorial_review 授权记录（如有）。HTML 由 Course Compiler 导出。\n'


def audit_agent_run(task, snapshot, allowed, root, formal_before):
    """Shared input/history/output inventory gate for single and staged calls."""
    errors = []
    for relative, data in snapshot.items():
        try:
            if read_bytes(task, relative) != data:
                errors.append(f'workspace: input/control modified: {relative}')
        except (OSError, ValueError):
            errors.append(f'workspace: input/control removed or replaced: {relative}')
    for path in task.rglob('*'):
        relative = path.relative_to(task).as_posix()
        try:
            safe_path(task, relative)
            if not path.is_dir() and relative not in snapshot and relative not in allowed:
                errors.append(f'Agent wrote an unexpected file: {relative}')
        except (OSError, ValueError):
            errors.append('Agent wrote an unsafe path')
    try:
        if _formal_inventory(root) != formal_before:
            errors.append('正式控制文件或历史成果在运行期间发生变化；拒绝发布，请检查外部写入')
        if not validate_workspace(root).ok:
            errors.append('正式材料哈希在运行期间发生变化；拒绝发布')
    except (OSError, ValueError):
        errors.append('正式工作区在运行期间变得不可安全读取；拒绝发布')
    return errors


def invoke_stage_process(root, snapshot, prompt, adapter, model, *, observation=None):
    """One bounded stage, using the same adapter and inventory protection."""
    from ..authoring_stages import RESULT
    formal_before = _formal_inventory(root)
    with tempfile.TemporaryDirectory(prefix='handouter-stage-') as temporary:
        task = Path(temporary).resolve()
        for relative, data in snapshot.items():
            path = task / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (task / 'authoring').mkdir(exist_ok=True)
        command = adapter.build_command(task, model=model)
        from ..invocation_receipts import begin_observation, finish_observation
        started = begin_observation(command, snapshot, prompt, adapter.__name__.rsplit('.', 1)[-1], model)
        completed = subprocess.run(command, input=prompt,
            text=True, cwd=task, stdout=sys.stderr, stderr=sys.stderr,
            encoding='utf-8', errors='replace', check=False)
        errors = audit_agent_run(task, snapshot, {RESULT}, root, formal_before)
        data = None
        if not errors:
            try:
                path = safe_path(task, RESULT)
                if path.stat().st_size > MAX_RECORD_BYTES:
                    raise ValueError('STAGE_ARTIFACT_TOO_LARGE')
                data = read_bytes(task, RESULT)
            except (OSError, ValueError):
                errors.append('STAGE_RESULT_MISSING_OR_UNSAFE')
        finished_observation = finish_observation(started, completed.returncode, data, errors)
        if observation is not None:
            observation.update(finished_observation)
        return completed.returncode, data, errors


def available_cli_agents() -> list[AgentAvailability]:
    return [adapter.availability() for adapter in _ADAPTERS.values()]


def _formal_inventory(root: Path) -> dict[str, str]:
    paths = [root / 'manifest.json', root / 'state.json']
    for directory in ('handoff', 'notes', 'authoring'):
        for path in (root / directory).rglob('*'):
            relative = path.relative_to(root).as_posix()
            safe_path(root, relative)
            if path.is_file():
                paths.append(path)
    inventory = {p.relative_to(root).as_posix(): sha256_file(root, p.relative_to(root).as_posix())
                 for p in paths}
    from ..organization import package_for_workspace
    package = package_for_workspace(root)
    if package is not None:
        for path in (package / '成果').rglob('*'):
            relative = path.relative_to(package).as_posix()
            safe_path(package, relative)
            if path.is_file():
                inventory['published/' + relative] = hashlib.sha256(read_bytes(package, relative)).hexdigest()
    return inventory


def run_cli_agent(workspace: str | Path, *, agent: str, model: str | None = None) -> AgentRunResult:
    with workspace_lock(Path(workspace)):
        from ..transactions import recover_refresh
        recover_refresh(Path(workspace))
        return _run_cli_agent(workspace, agent=agent, model=model)


def _run_cli_agent(
    workspace: str | Path,
    *,
    agent: str,
    model: str | None = None,
    _stage_context: dict | None = None,
) -> AgentRunResult:
    """Run one CLI agent non-interactively against a prepared workspace.

    Child output is forwarded to stderr so Handouter's stdout can remain a
    machine-readable JSON result when called through the CLI.
    """
    name = agent.strip().lower()
    adapter = _ADAPTERS.get(name)
    if adapter is None:
        raise ValueError(f"unsupported CLI agent: {agent}")

    root, sources, prompt = read_handoff(workspace)
    from ..authoring_stages import policy as stage_policy
    if stage_policy(sources) and _stage_context is None:
        from ..authoring_stages import run_staged
        return run_staged(root, sources, prompt, agent=name, model=model)
    all_outputs = expected_outputs(sources)
    authoring_policy = contract(sources)
    from .. import editorial_review
    review_policy = editorial_review.policy(sources)
    review_path = None
    review_diagnostic = 'not_provided'
    progress_path = None
    host_checks = {}
    candidate_paths = []
    outputs = {}
    preflight = validate_workspace(root)
    if not preflight.ok:
        raise ValueError('Agent 输入验证失败: ' + '; '.join(preflight.errors))
    for mode_name, relative in all_outputs.items():
        if safe_path(root, relative, role='note', missing=True).exists():
            report = validate_note_output(root, output=relative)
            existing_errors = published_errors(root, sources, mode_name)
            if not report.ok or existing_errors:
                return AgentRunResult(name, 0, all_outputs, False,
                    (f'{mode_name}: 已有成果无效，保留原件；请显式创建新任务版本', *report.errors, *existing_errors))
        else:
            outputs[mode_name] = relative
    if not outputs:
        return AgentRunResult(name, 0, all_outputs, True, ())
    info = adapter.availability()
    if not info.available:
        raise FileNotFoundError(f"{name} CLI is not available on PATH")
    formal_before = _formal_inventory(root)

    errors: list[str] = []
    snapshot = selected_files(root, sources)
    sanitized, prompt = scope_pending(sources, prompt, outputs)
    sanitized['transcript']['raw_asr_path'] = None
    snapshot['handoff/PROMPT.md'] = prompt.encode('utf-8')
    snapshot['handoff/sources.json'] = (json.dumps(sanitized, ensure_ascii=False) + '\n').encode('utf-8')
    snapshot['state.json'] = b'{}\n'
    if _stage_context is not None:
        snapshot = _stage_context['snapshot']
        prompt = _stage_context['prompt']
    # No hard links, original paths, audio, raw ASR, history or old notes are
    # supplied. This is write isolation by copying, not an OS security sandbox.
    with tempfile.TemporaryDirectory(prefix='handouter-agent-') as temporary:
        task = Path(temporary).resolve()
        for relative, data in snapshot.items():
            path = task / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        for relative in outputs.values():
            (task / relative).parent.mkdir(parents=True, exist_ok=True)
        command = adapter.build_command(task, model=model)
        if authoring_policy:
            (task / 'authoring').mkdir(exist_ok=True)
        from ..invocation_receipts import begin_observation, finish_observation, encoded as receipt_encoded
        started_observation = begin_observation(command, snapshot, prompt + CLI_FOOTER, name, model)
        completed = subprocess.run(command, input=prompt + CLI_FOOTER,
            text=True, cwd=task, stdout=sys.stderr, stderr=sys.stderr,
            encoding='utf-8', errors='replace', check=False)
        allowed = set(outputs.values()) | ({RECORD_PATH} if authoring_policy else set())
        if review_policy:
            allowed.add(editorial_review.RECORD_PATH)
        if _stage_context is not None:
            from ..authoring_stages import RESULT
            allowed.add(RESULT)
        errors.extend(audit_agent_run(task, snapshot, allowed, root, formal_before))
        safe_candidate_inputs = not errors
        progress_data = None
        progress = None
        if authoring_policy and (task / RECORD_PATH).exists():
            try:
                record = safe_path(task, RECORD_PATH)
                if record.stat().st_size > MAX_RECORD_BYTES:
                    raise ValueError('记录超过大小限制')
                progress_data = read_bytes(task, RECORD_PATH)
                progress = validate_progress(progress_data, sources, root=task)
                if _stage_context is not None:
                    from ..authoring_stages import validate_review_record
                    validate_review_record(task, sources, progress, _stage_context)
            except (OSError, ValueError):
                errors.append('编辑记录无效；拒绝发布本轮文件')
        elif authoring_policy and authoring_policy['schema_version'] == 2:
            errors.append('AUTHORING_RECORD_REQUIRED')
        integrity_ok = not errors
        record_save_failed = False
        if integrity_ok and progress_data is not None:
            try:
                progress_path = save_progress(root, sources, progress_data).relative_to(root).as_posix()
            except (OSError, ValueError):
                errors.append('无法保存编辑记录；不宣称可恢复')
                integrity_ok = False
                record_save_failed = True
        if completed.returncode != 0:
            errors.append(f'{name} exited with code {completed.returncode}')
        verified_outputs = {}
        for mode, relative in outputs.items():
            if authoring_policy and authoring_policy['schema_version'] == 2:
                from ..authoring_v2 import completion_errors
                gate = completion_errors(progress, mode) if progress is not None else ['AUTHORING_RECORD_INVALID_OR_MISSING']
                host_checks[mode] = {'status': 'blocked' if gate or not integrity_ok else 'pending',
                                     'errors': list(gate) + ([] if integrity_ok else ['AUTHORING_INTEGRITY_REJECTED']),
                                     'semantic_review': 'required'}
                if gate:
                    errors.extend(f'{mode}: {message}' for message in gate)
                    # Keep only structurally safe candidate Markdown, never arbitrary Agent files.
                    if safe_candidate_inputs and not record_save_failed:
                        candidate_report = validate_note_output(task, output=relative)
                        if candidate_report.ok:
                            import uuid
                            candidate = 'authoring/' + authoring_policy['task_key'] + '/candidate-' + uuid.uuid4().hex + '.md'
                            data = read_bytes(task, relative, role='note')
                            if hashlib.sha256(data).hexdigest() == candidate_report.sha256:
                                directory = safe_path(root, 'authoring/' + authoring_policy['task_key'], missing=True)
                                directory.mkdir(parents=True, exist_ok=True)
                                try:
                                    candidate_paths.append(publish_bytes(root, candidate, data).relative_to(root).as_posix())
                                except (OSError, ValueError):
                                    errors.append('无法保存安全候选稿；不宣称可恢复')
                    continue
            if progress is not None and mode not in progress['completed_modes']:
                errors.append(f'{mode}: 仅保存编辑草稿，尚未声明整稿就绪')
                continue
            if _stage_context is not None and sources.get('composer'):
                try:
                    from ..composer import compose_with_manifest
                    from ..composition_store import save_composition, checked_composition
                    from ..credentials import scan_credentials
                    from ..authoring_stages import prefix, sha
                    if not integrity_ok:
                        raise ValueError('COMPOSER_INPUT_NOT_ACCEPTED')
                    raw = read_bytes(task, relative, role='note')
                    if len(raw) > 4 * 1024 * 1024 or scan_credentials(raw.decode('utf-8')).errors:
                        raise ValueError('COMPOSER_DRAFT_UNSAFE')
                    draft_path = prefix(sources) + '/draft-' + sha(raw) + '.txt'
                    try:
                        publish_bytes(root, draft_path, raw)
                    except FileExistsError:
                        if read_bytes(root, draft_path) != raw:
                            raise ValueError('COMPOSER_DRAFT_CHANGED')
                    candidate_paths.append(draft_path)
                    plan = _stage_context['plan']
                    composed, composition = compose_with_manifest(raw.decode('utf-8'), task, sources,
                        plan['objects'], mode=mode, plan_revision=plan['revision'], plan_hash=plan['plan_hash'])
                    save_composition(task, sources, raw, composition)
                    checked_composition(task, sources, composed.encode('utf-8'), mode, plan)
                    save_composition(root, sources, raw, composition)
                    publish_bytes(task, relative, composed.encode('utf-8'), replace=True)
                except (OSError, ValueError, UnicodeError):
                    errors.append(f'{mode}: COMPOSER_FAILED；原始草稿若安全则保留，不删除标记或猜来源')
                    host_checks[mode]['status'] = 'blocked'
                    host_checks[mode]['errors'].append('COMPOSER_FAILED')
                    continue
            report = validate_note_output(task, output=relative)
            if not report.ok:
                errors.extend(f'{mode}: {message}' for message in report.errors)
                if mode in host_checks:
                    host_checks[mode]['status'] = 'blocked'
                    host_checks[mode]['errors'].append('AUTHORING_OUTPUT_INVALID_OR_MISSING')
            else:
                try:
                    data = read_bytes(task, relative, role='note')
                    if hashlib.sha256(data).hexdigest() != report.sha256:
                        raise ValueError('输出在校验后发生变化')
                    verified_outputs[relative] = data
                    if mode in host_checks:
                        host_checks[mode]['output_sha256'] = report.sha256
                except (OSError, ValueError):
                    errors.append(f'{mode}: 输出在校验后发生变化')
        if integrity_ok and completed.returncode == 0:
            # Inputs, control files, output inventory and process status must
            # all pass. Valid individual outputs may survive a missing sibling;
            # aggregate failure remains explicit and the next run fills gaps.
            for relative in verified_outputs:
                target = safe_path(root, relative, role='note', missing=True)
                try:
                    mode = next(mode for mode, path in outputs.items() if path == relative)
                    if _stage_context is not None and not validate_workspace(root).ok:
                        raise ValueError('STAGE_SOURCE_CHANGED_BEFORE_PUBLICATION')
                    stage_binding = ({'revision': _stage_context['plan']['revision'], 'hash': _stage_context['plan']['plan_hash']}
                                     if _stage_context else None)
                    accept_output(root, sources, mode, verified_outputs[relative], progress_path, stage_plan=stage_binding)
                    publish_bytes(root, relative, verified_outputs[relative])
                    if _stage_context is not None:
                        from ..authoring_stages import latest_plan
                        latest_plan(root, sources)
                        if not validate_workspace(root).ok:
                            raise ValueError('STAGE_SOURCE_CHANGED_DURING_PUBLICATION')
                    if mode in host_checks:
                        host_checks[mode]['status'] = 'passed'
                except (OSError, ValueError):
                    errors.append((f'STAGE_PUBLICATION_NOT_ACCEPTED：发布期间变化或失败，已落盘文件保留；请检查并显式刷新: {relative}'
                                   if _stage_context is not None else
                                   f'发布失败；已发布成果保留，可继续缺失项: {relative}'))
                    break
        # Optional review findings do not turn a valid process into a failure.
        # Save only declarations bound to actual published outputs; never promote
        # a malformed/missing report to a semantic pass or discard safe notes.
        if review_policy and (task / editorial_review.RECORD_PATH).exists():
            review_diagnostic = 'not_accepted'
            if integrity_ok and completed.returncode == 0:
                try:
                    candidate = safe_path(task, editorial_review.RECORD_PATH)
                    if candidate.stat().st_size > editorial_review.MAX_BYTES:
                        raise ValueError('EDITORIAL_TOO_LARGE')
                    review_data = read_bytes(task, editorial_review.RECORD_PATH)
                    editorial_review.validate_review(review_data, task, sanitized)
                    review_path = editorial_review.save_review(root, sources, review_data).relative_to(root).as_posix()
                    review_diagnostic = 'declaration_recorded'
                except (OSError, ValueError, KeyError, TypeError):
                    review_diagnostic = 'invalid_or_incomplete'
        for checked in host_checks.values():
            if checked['status'] != 'passed':
                checked['status'] = 'blocked'
                if completed.returncode != 0:
                    checked['errors'].append('AUTHORING_PROCESS_FAILED')
                if not checked['errors']:
                    checked['errors'].append('AUTHORING_OUTPUT_NOT_PUBLISHED')

    state_path = root / "state.json"
    try:
        state: dict[str, Any] = json.loads(read_bytes(root, 'state.json'))
        present = [relative for relative in all_outputs.values()
                   if safe_path(root, relative, role='note', missing=True).is_file()]
        state["agent_output"] = {
            "status": "validated" if not errors else ('partial' if present else 'failed'),
            "agent": name,
            "returncode": completed.returncode,
            "outputs": all_outputs,
            "errors": errors,
            "semantic_review": "required",
            "authoring_record": progress_path,
            "authoring_checks": host_checks,
            "authoring_candidates": candidate_paths,
            "editorial_review_record": review_path,
            "editorial_review_diagnostic": review_diagnostic,
        }
        publish_bytes(root, 'state.json', (json.dumps(state, ensure_ascii=False, indent=2) + '\n').encode('utf-8'), replace=True)
    except (OSError, ValueError, json.JSONDecodeError):
        errors.append('无法安全提交任务状态；已发布成果保留，请重新核验')

    finish_observation(started_observation, completed.returncode,
        receipt_encoded({relative: hashlib.sha256(data).hexdigest() for relative, data in verified_outputs.items()}), errors)

    return AgentRunResult(
        agent=name,
        returncode=completed.returncode,
        outputs=all_outputs,
        validation_ok=not errors,
        validation_errors=tuple(errors),
    )
