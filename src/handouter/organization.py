"""User-facing course packages around the stable internal workspace protocol."""
from datetime import date
from functools import wraps
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import unicodedata

from .safe_io import safe_path, read_json, publish_bytes, publish_directory, workspace_lock

MATERIALS = '资料与交接'
RESULTS = '成果'
CACHE = '中间产物_可清理'
MARKER = '.handouter-collection.json'
MARKER_VERSION = 3


def lecture_date(value: str | None) -> str | None:
    if value in (None, ''):
        return None
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('授课日期须为真实 YYYY-MM-DD；未知请留空，不使用导出日期')
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError('授课日期不是有效日历日期') from exc


def _safe_title(title: str) -> str:
    from .credentials import scan_credentials
    if scan_credentials(title).errors:
        raise ValueError('课程名称含敏感字段，请使用不含凭证的名称')
    title = unicodedata.normalize('NFC', title.strip())
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', title).strip(' .')[:48].rstrip(' .')
    title = title or '未命名课程'
    return title


def _date_label(day: str | None) -> str:
    parsed = lecture_date(day)
    return parsed[5:] if parsed else '日期待确认'


def course_name(title: str, day: str | None) -> str:
    """Full package label: 课程-MM-DD."""
    title = _safe_title(title)
    name = title + '-' + _date_label(day)
    from .platform_support import is_portable_path_component
    if not is_portable_path_component(name):
        raise ValueError('课程名称不能形成可移植目录名')
    return name


def date_only_name(day: str | None) -> str:
    name = _date_label(day)
    from .platform_support import is_portable_path_component
    if not is_portable_path_component(name):
        raise ValueError('日期不能形成可移植目录名')
    return name


def _label_key(value: str) -> str:
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFC', value).strip()).casefold()


def package_name(title: str, day: str | None, output_root: Path, *, aliases=(), folder_name_mode='always') -> tuple[str, str]:
    """Return (folder name, style); auto avoids duplicating a course-named root."""
    if folder_name_mode not in {'auto', 'always', 'never'}:
        raise ValueError('课程目录命名模式无效')
    labels = {_label_key(_safe_title(title)), *(_label_key(alias) for alias in aliases)}
    root_matches = _label_key(output_root.name) in labels if output_root.name else False
    use_course = folder_name_mode == 'always' or (folder_name_mode == 'auto' and not root_matches)
    return (course_name(title, day), 'course_date') if use_course else (date_only_name(day), 'date_only')


def _legacy_course_name(title: str, day: str | None) -> str:
    title = _safe_title(title)
    name = title + '_' + (lecture_date(day) or '授课日期待确认')
    from .platform_support import is_portable_path_component
    if not is_portable_path_component(name):
        raise ValueError('课程名称不能形成可移植目录名')
    return name


def _artifact_name(title: str, day: str | None) -> str:
    return _legacy_course_name(title, day)


def _marker(package: Path) -> dict:
    from .workspace import validate_lecture_id
    record = read_json(package, MARKER)
    if not isinstance(record, dict) or record.get('version') not in (1, 2, MARKER_VERSION):
        raise ValueError('课程包标记损坏')
    validate_lecture_id(record.get('lecture_id', ''))
    if record['version'] == 1:
        expected = _legacy_course_name(record.get('course_title', ''), record.get('lecture_date'))
    elif record['version'] == 2:
        expected = course_name(record.get('course_title', ''), record.get('lecture_date'))
    else:
        style = record.get('folder_name_style')
        if style not in {'course_date', 'date_only'}:
            raise ValueError('课程包目录命名信息损坏')
        expected = (course_name(record.get('course_title', ''), record.get('lecture_date'))
                    if style == 'course_date' else date_only_name(record.get('lecture_date')))
    if expected != record.get('base_name'):
        raise ValueError('课程包名称信息不一致')
    return record


def resolve_workspace(value) -> Path:
    path = Path(value).expanduser().absolute()
    # Do not resolve away a selected symlink before inspecting it.
    if path.is_symlink():
        raise ValueError('工作区入口不能是符号链接')
    path = path.resolve()
    if (path / MARKER).exists():
        record = _marker(path)
        return safe_path(path, MATERIALS + '/' + record['lecture_id'])
    return path


def find_workspace(output_root, identity: str) -> Path | None:
    from .workspace import validate_lecture_id
    validate_lecture_id(identity)
    root = Path(output_root).expanduser().resolve()
    if not root.exists():
        return None
    old = safe_path(root, identity, missing=True)
    if (old / 'manifest.json').exists():
        if read_json(root, identity + '/manifest.json').get('lecture_id') != identity:
            raise ValueError('既有工作区 ID 不一致')
        return old
    matches = []
    for entry in root.iterdir():
        if entry.is_dir() and (entry / MARKER).exists():
            safe_path(root, entry.name)
            record = _marker(entry)
            if record['lecture_id'] == identity:
                workspace = safe_path(entry, MATERIALS + '/' + identity, missing=True)
                if (workspace / 'manifest.json').exists():
                    matches.append(workspace)
    if len(matches) > 1:
        raise ValueError('多个课程包使用同一讲次 ID，请显式打开工作区')
    return matches[0] if matches else None


def _select_package(root: Path, identity: str, title: str, day: str | None, *, aliases=(), folder_name_mode='always') -> tuple[Path, dict, bool]:
    base, style = package_name(title, day, root, aliases=aliases, folder_name_mode=folder_name_mode)
    occupied = set()
    for entry in root.iterdir():
        occupied.add(unicodedata.normalize('NFC', entry.name).casefold())
        if entry.is_dir() and (entry / MARKER).exists():
            safe_path(root, entry.name)
            record = _marker(entry)
            if record['lecture_id'] == identity:
                if record['course_title'] != title or record['lecture_date'] != day:
                    raise ValueError('既有讲次的名称/日期不同；请打开已有工作区，或使用新的讲次 ID')
                return entry, record, False
    name = base
    if unicodedata.normalize('NFC', name).casefold() in occupied:
        name += '_' + hashlib.sha256(identity.encode()).hexdigest()[:12]
    if unicodedata.normalize('NFC', name).casefold() in occupied:
        raise FileExistsError('课程目录冲突；保留既有目录，请使用新讲次 ID')
    record = dict(version=MARKER_VERSION, lecture_id=identity, course_title=title,
                  lecture_date=day, base_name=base, name_prefix=_artifact_name(title, day),
                  folder_name_style=style)
    staging = Path(tempfile.mkdtemp(prefix='.organize-', dir=root))
    target = safe_path(root, name, missing=True)
    try:
        for directory in (MATERIALS, RESULTS, CACHE):
            (staging / directory).mkdir()
        publish_bytes(staging, MARKER, (json.dumps(record, ensure_ascii=False, indent=2) + '\n').encode())
        publish_directory(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return target, record, True


def _remove_empty_failed_package(package: Path) -> None:
    """Remove only our marker/idle locks and empty directories, never cached data."""
    paths = list(package.rglob('*'))
    for path in paths:
        safe_path(package, path.relative_to(package).as_posix())
        if path.is_file() and path.name not in {MARKER, '.handouter.lock'}:
            return
    for path in paths:
        if path.is_file():
            path.unlink()
    for path in sorted(paths, key=lambda item: len(item.parts), reverse=True):
        if path.is_dir():
            path.rmdir()  # Any unexpected concurrent file prevents removal.
    package.rmdir()


def organized_operation(function):
    """Opt-in product layout; existing Python service callers keep their layout."""
    parse_date = lecture_date
    @wraps(function)
    def run(output_root, *args, organized=False, lecture_date=None, course_aliases=(), folder_name_mode='always', **kwargs):
        if not organized:
            if lecture_date is not None:
                raise ValueError('授课日期命名需要新课程包布局')
            return function(output_root, *args, **kwargs)
        from .workspace import validate_lecture_id
        identity = validate_lecture_id(kwargs['lecture_id'])
        day = parse_date(lecture_date)
        title = kwargs['course_title'].strip()
        root = Path(output_root).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        root = root.resolve()
        with workspace_lock(root):
            if find_workspace(root, identity) is not None:
                raise FileExistsError('讲次已存在；请继续已有任务，不覆盖原材料')
            package, record, created = _select_package(root, identity, title, day,
                aliases=course_aliases, folder_name_mode=folder_name_mode)
            safe_path(package, MATERIALS)
            safe_path(package, CACHE)
            try:
                with workspace_lock(package):
                    return function(package / MATERIALS, *args, output_organization=record,
                                    cache_root=package / CACHE, **kwargs)
            except BaseException:
                if created:
                    try:
                        _remove_empty_failed_package(package)
                    except (OSError, ValueError):
                        pass  # Preserve an unexpected/racing file and original error.
                raise
    return run


def artifact_prefix(workspace: Path) -> str:
    manifest = read_json(workspace, 'manifest.json')
    info = manifest.get('output_organization')
    if not info:
        return ''
    from .safe_io import relative_path
    prefix = relative_path(info['name_prefix'])
    if '/' in prefix or info.get('version') not in (1, 2, MARKER_VERSION):
        raise ValueError('成果命名协议无效')
    return prefix + '_'


def package_for_workspace(workspace) -> Path | None:
    root = resolve_workspace(workspace)
    if root.parent.name != MATERIALS:
        return None
    package = root.parent.parent
    if not (package / MARKER).exists():
        return None
    record = _marker(package)
    if resolve_workspace(package) != root or read_json(root, 'manifest.json').get('output_organization') != record:
        raise ValueError('课程包与内部工作区身份不一致')
    return package


def cache_root_for(value, *, require_ready=False) -> Path:
    path = Path(value).expanduser().resolve()
    if path.name == CACHE and (path.parent / MARKER).exists():
        path = path.parent
    if (path / MARKER).exists():
        record = _marker(path)
        workspace = safe_path(path, MATERIALS + '/' + record['lecture_id'], missing=True)
        # Until preparation commits, cached audio/ASR may be the only local
        # copy; do not offer it as disposable course data.
        from .validation import validate_workspace
        if require_ready and not validate_workspace(workspace).ok:
            raise ValueError('正式资料尚未完整保存，不能清理本讲次中间产物')
        return safe_path(path, CACHE)
    return path


def verify_disposable_cache(cache: Path, target: Path) -> None:
    if cache.name != CACHE or not (cache.parent / MARKER).exists():
        return  # Existing explicitly selected legacy caches retain their API.
    cache_root_for(cache, require_ready=True)
    workspace = resolve_workspace(cache.parent)
    recorded = {item['sha256'] for item in read_json(workspace, 'manifest.json')['files']}
    from .safe_io import read_bytes
    for path in target.rglob('*'):
        if path.is_file() and path.name != 'stages.json':
            content = read_bytes(cache, path.relative_to(cache).as_posix())
            if hashlib.sha256(content).hexdigest() not in recorded:
                raise ValueError('缓存含尚未保存在正式资料中的文件，拒绝清理唯一材料')
