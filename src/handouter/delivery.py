"""Publish portable copies, retaining the internal notes as immutable evidence.

The complete 成果 directory can be moved: notes and the resources they use keep
their original relative layout. This is structural delivery, not semantic review.
"""
import hashlib
import os
from pathlib import Path

from .resources import image_resource, parse_resources
from .safe_io import read_bytes, read_json, relative_path, safe_path, publish_bytes, workspace_lock
from .validation import validate_note_output


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def html_requested(delivery: dict, mode: str) -> bool:
    """Only an explicit per-mode opt-out changes legacy HTML requirements."""
    if not isinstance(delivery, dict) or not isinstance(delivery.get('requested', {}), dict):
        raise ValueError('HTML 请求状态格式无效')
    return delivery.get('requested', {}).get(mode) is not False


def publication_current(workspace: Path) -> bool:
    """Read-only check: missing/edited delivery copies require publication only."""
    from .organization import package_for_workspace
    package = package_for_workspace(workspace)
    if package is None:
        return True
    try:
        sources = read_json(workspace, 'handoff/sources.json')
        html = read_json(workspace, 'state.json').get('html_delivery', {})
        for mode, relative in sources['expected_outputs'].items():
            note = read_bytes(workspace, relative, role='note')
            if read_bytes(package, '成果/' + relative) != note:
                return False
            for link in parse_resources(note.decode('utf-8-sig')).images:
                resource = image_resource(workspace, workspace / relative, link, options=sources['options'])
                if resource is None or read_bytes(package, '成果/' + resource) != read_bytes(workspace, resource):
                    return False
            if not html_requested(html, mode):
                continue
            relative_html = html['outputs'][mode]
            if not relative_html.startswith('notes/') or not relative_html.endswith('.html'):
                return False
            if _digest(read_bytes(package, '成果/' + relative_html)) != html['hashes'][mode]:
                return False
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _directories(root, relative):
    """Create destination ancestors without following POSIX directory links."""
    relative_path(relative)
    safe_path(root, relative, missing=True)
    if os.name == 'posix':
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in relative.split('/'):
                try:
                    os.mkdir(part, dir_fd=fd)
                except FileExistsError:
                    pass
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
        finally:
            os.close(fd)
    else:
        prefix = []
        for part in relative.split('/'):
            prefix.append(part)
            path = safe_path(root, '/'.join(prefix), missing=True)
            path.mkdir(exist_ok=True)
            safe_path(root, '/'.join(prefix))


def _copy_snapshot(lecture, relative, data):
    destination = '成果/' + relative
    _directories(lecture, str(Path(destination).parent).replace('\\', '/'))
    try:
        publish_bytes(lecture, destination, data)
    except FileExistsError:
        if read_bytes(lecture, destination) != data:
            raise ValueError('成果中已有同名不同内容文件，已保留，不能覆盖')
    if _digest(read_bytes(lecture, destination)) != _digest(data):
        raise ValueError('成果复制后哈希不一致')
    return str(lecture / destination)


def publish_deliverables(workspace) -> dict:
    """Return JSON-ready status/outputs/errors; never modify existing notes.

    outputs maps mode to {markdown, html?}; each path is absolute. Missing
    expected notes are pending {mode: relative_path}, not copy errors. Legacy
    workspaces return ready with applicable=False and no filesystem changes.
    A remote image is retained verbatim but makes offline_complete=False and
    status partial. HTML requires the saved Markdown/HTML hash provenance.
    """
    result = {'status': 'ready', 'applicable': False, 'outputs': {}, 'pending': {}, 'errors': [],
              'offline_complete': True, 'semantic_review': 'required'}
    workspace = Path(workspace).absolute()
    if workspace.parent.name != '资料与交接':
        return result
    try:
        lecture = workspace.parent.parent.resolve(strict=True)
        workspace = safe_path(lecture, '资料与交接/' + workspace.name)
        manifest = read_json(workspace, 'manifest.json')
        organization = manifest.get('output_organization', {})
        if organization.get('version') not in (1, 2, 3) or not organization.get('name_prefix'):
            return result
        relative_path(organization['name_prefix'])
        from .organization import package_for_workspace
        if (package_for_workspace(workspace) != lecture
                or manifest.get('lecture_id') != workspace.name):
            raise ValueError('课程包标记与工作区不匹配')
        result['applicable'] = True
        sources = read_json(workspace, 'handoff/sources.json')
        state = read_json(workspace, 'state.json')
        outputs = sources.get('expected_outputs') or {sources.get('mode'): sources.get('expected_output')}
        if not isinstance(outputs, dict):
            raise ValueError('讲义输出计划无效')
        safe_path(lecture, '成果', missing=True)
    except (OSError, ValueError, TypeError, AttributeError):
        result.update(status='failed', offline_complete=False)
        result['errors'].append('成果目录或交接元数据不安全；原讲义已保留')
        return result
    try:
        with workspace_lock(workspace):
            if package_for_workspace(workspace) != lecture:
                raise ValueError('课程包标记与工作区不匹配')
            # Read controls again while holding the shared task lock.
            sources = read_json(workspace, 'handoff/sources.json')
            state = read_json(workspace, 'state.json')
            outputs = sources.get('expected_outputs') or {sources.get('mode'): sources.get('expected_output')}
            if not isinstance(outputs, dict):
                raise ValueError('讲义输出计划无效')
            return _publish_outputs(workspace, lecture, sources, state, outputs, result)
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
        result.update(status='failed', offline_complete=False)
        result['errors'].append('任务正在使用或交接文件不可读；原讲义已保留')
        return result


def _publish_outputs(workspace, lecture, sources, state, outputs, result):
    for mode, relative in outputs.items():
        try:
            target = safe_path(workspace, relative, role='note', missing=True)
            if not target.exists():
                result['pending'][str(mode)] = relative
                continue
            report = validate_note_output(workspace, output=relative)
            if not report.ok:
                raise ValueError('讲义尚未通过结构校验')
            data = read_bytes(workspace, relative, role='note')
            if _digest(data) != report.sha256:
                raise ValueError('讲义在校验后发生变化')
            resources = parse_resources(data.decode('utf-8'))
            snapshots = {}
            for link in resources.images:
                resource = image_resource(workspace, workspace / relative, link, options=sources.get('options', {}))
                if resource is None:
                    result['offline_complete'] = False
                    result['errors'].append('讲义含远程图片，成果不能保证完整离线阅读')
                    continue
                snapshots[resource] = read_bytes(workspace, resource)
            snapshots[relative] = data
            # Detect conflicts before publishing the note or any resource.
            for path, snapshot in snapshots.items():
                target = safe_path(lecture, '成果/' + path, missing=True)
                if target.exists() and read_bytes(lecture, '成果/' + path) != snapshot:
                    raise ValueError('成果中已有同名不同内容文件，已保留，不能覆盖')
                if read_bytes(workspace, path) != snapshot:
                    raise ValueError('资源在校验后发生变化')
            for path, snapshot in snapshots.items():
                copied = _copy_snapshot(lecture, path, snapshot)
            result['outputs'][str(mode)] = {'markdown': copied}
            html = state.get('html_delivery', {})
            if not html_requested(html, mode):
                continue
            html_relative = html.get('outputs', {}).get(mode)
            if html_relative:
                if not html_relative.startswith('notes/') or not html_relative.endswith('.html'):
                    raise ValueError('HTML 成果路径不安全')
                html_data = read_bytes(workspace, html_relative)
                if (html.get('note_hashes', {}).get(mode) != report.sha256
                        or html.get('hashes', {}).get(mode) != _digest(html_data)):
                    raise ValueError('HTML 与当前讲义的哈希记录不匹配，请重新导出')
                result['outputs'][str(mode)]['html'] = _copy_snapshot(lecture, html_relative, html_data)
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
            result['errors'].append('成果复制未完成：讲义校验、路径、哈希或同名文件冲突；原讲义已保留')
    if result['errors']:
        result['status'] = 'partial' if result['outputs'] else 'failed'
        result['offline_complete'] = False
    elif result['pending']:
        result['status'] = 'partial'
    return result
