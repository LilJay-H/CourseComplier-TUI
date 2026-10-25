"""User-selected directory defaults and per-course naming preferences."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import unicodedata

from .credentials import scan_credentials
from .platform_support import desktop_directory


SETTINGS_VERSION = 3
FOLDER_NAME_MODES = ('auto', 'always', 'never')
_UNSET = object()


def directory_settings_path() -> Path:
    home = Path.home()
    if sys.platform == 'win32':
        base = Path(os.environ.get('LOCALAPPDATA', str(home / 'AppData/Local')))
    elif sys.platform == 'darwin':
        base = home / 'Library/Application Support'
    else:
        base = Path(os.environ.get('XDG_CONFIG_HOME', str(home / '.config')))
        if not base.is_absolute():
            base = home / '.config'
    if not base.is_absolute():
        raise ValueError('应用配置目录必须是绝对路径')
    return base / 'Handouter' / 'directories.json'


def _check_config_path(path: Path) -> None:
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('目录设置路径不能是符号链接或重解析点')
    if path.exists() and not path.is_file():
        raise ValueError('目录设置必须是普通文件')


def _validated_path(value: str | Path, *, label: str) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError(f'{label}不能为空')
    text = str(value)
    report = scan_credentials(text)
    if report.errors or report.warnings or any(ord(c) < 32 for c in text):
        raise ValueError('目录名称含凭证字段或控制字符；请使用普通目录名称')
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f'{label}必须是绝对路径')
    path = path.resolve()
    if path.exists() and not path.is_dir():
        raise ValueError(f'{label}必须是目录')
    return path


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _validated(input_dir: str | Path, output_dir: str | Path) -> dict[str, str]:
    incoming = _validated_path(input_dir, label='输入目录')
    outgoing = _validated_path(output_dir, label='输出目录')
    if _overlaps(incoming, outgoing):
        raise ValueError('输入和输出目录不能相同或互相嵌套')
    return {'input_dir': str(incoming), 'output_dir': str(outgoing)}


def _course_title(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError('课程名称不能为空')
    title = unicodedata.normalize('NFC', value).strip()
    if not title or len(title) > 512 or any(ord(c) < 32 for c in title):
        raise ValueError('课程名称不能为空或含控制字符')
    report = scan_credentials(title)
    if report.errors or report.warnings:
        raise ValueError('课程名称含凭证字段；请使用普通课程名称')
    return title


def normalize_course_label(value: str) -> str:
    return re.sub(r'\s+', ' ', _course_title(value)).casefold()


def course_settings_key(course_title: str) -> str:
    return hashlib.sha256(normalize_course_label(course_title).encode('utf-8')).hexdigest()[:32]


def _validated_course_outputs(value, input_dir: str | Path) -> dict[str, dict[str, str]]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 10000:
        raise ValueError('课程输出目录设置无效')
    incoming = _validated_path(input_dir, label='输入目录')
    result = {}
    for key, entry in value.items():
        if (not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{32}', key)
                or not isinstance(entry, dict) or set(entry) != {'course_title', 'output_dir'}):
            raise ValueError('课程输出目录设置无效')
        title = _course_title(entry['course_title'])
        if course_settings_key(title) != key:
            raise ValueError('课程输出目录身份不一致')
        outgoing = _validated_path(entry['output_dir'], label='课程输出目录')
        if _overlaps(incoming, outgoing):
            raise ValueError('课程输出目录不能与输入目录相同或互相嵌套')
        result[key] = {'course_title': title, 'output_dir': str(outgoing)}
    return result


def _validated_aliases(value) -> dict[str, dict]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 10000:
        raise ValueError('课程别名设置无效')
    result, owners = {}, {}
    for key, entry in value.items():
        if (not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{32}', key)
                or not isinstance(entry, dict) or set(entry) != {'course_title', 'aliases'}):
            raise ValueError('课程别名设置无效')
        title = _course_title(entry['course_title'])
        if course_settings_key(title) != key or not isinstance(entry['aliases'], list) or len(entry['aliases']) > 100:
            raise ValueError('课程别名设置无效')
        aliases, seen = [], set()
        canonical = normalize_course_label(title)
        owners.setdefault(canonical, key)
        if owners[canonical] != key:
            raise ValueError('课程名称/别名冲突')
        for alias in entry['aliases']:
            alias = _course_title(alias)
            normalized = normalize_course_label(alias)
            if normalized == canonical:
                continue
            if normalized in seen:
                continue
            if normalized in owners and owners[normalized] != key:
                raise ValueError('课程别名已属于另一门课程')
            owners[normalized] = key
            seen.add(normalized)
            aliases.append(alias)
        result[key] = {'course_title': title, 'aliases': aliases}
    return result


def _validated_folder_modes(value) -> dict[str, dict]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 10000:
        raise ValueError('课程目录命名设置无效')
    result = {}
    for key, entry in value.items():
        if (not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{32}', key)
                or not isinstance(entry, dict) or set(entry) != {'course_title', 'mode'}):
            raise ValueError('课程目录命名设置无效')
        title = _course_title(entry['course_title'])
        if course_settings_key(title) != key or entry['mode'] not in FOLDER_NAME_MODES:
            raise ValueError('课程目录命名设置无效')
        result[key] = {'course_title': title, 'mode': entry['mode']}
    return result


def _validate_course_registry(outputs: dict, aliases: dict, modes: dict) -> None:
    owners = {}
    for mapping in (outputs, aliases, modes):
        for key, entry in mapping.items():
            label = normalize_course_label(entry['course_title'])
            if label in owners and owners[label] != key:
                raise ValueError('课程正式名称冲突')
            owners[label] = key
    for key, entry in aliases.items():
        for alias in entry['aliases']:
            label = normalize_course_label(alias)
            if label in owners and owners[label] != key:
                raise ValueError('课程别名已属于另一门课程')
            owners[label] = key


def _defaults() -> dict:
    root = desktop_directory() / 'Handouter'
    return {'input_dir': str(root / '输入'), 'output_dir': str(root / '输出'),
            'course_output_dirs': {}, 'course_aliases': {}, 'course_folder_modes': {},
            'configured': False}


def _read_settings() -> dict:
    path = directory_settings_path()
    _check_config_path(path)
    if not path.exists():
        return _defaults()
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError('unexpected configuration fields')
        if set(value) == {'input_dir', 'output_dir'}:
            version = 1
            result = _validated(value['input_dir'], value['output_dir'])
            outputs, aliases, modes = {}, {}, {}
        elif set(value) == {'schema_version', 'input_dir', 'output_dir', 'course_output_dirs'}:
            version = value.get('schema_version')
            if version != 2:
                raise ValueError('unsupported settings version')
            result = _validated(value['input_dir'], value['output_dir'])
            outputs = _validated_course_outputs(value['course_output_dirs'], result['input_dir'])
            aliases, modes = {}, {}
        elif set(value) == {'schema_version', 'input_dir', 'output_dir', 'course_output_dirs',
                            'course_aliases', 'course_folder_modes'}:
            version = value.get('schema_version')
            if version != SETTINGS_VERSION:
                raise ValueError('unsupported settings version')
            result = _validated(value['input_dir'], value['output_dir'])
            outputs = _validated_course_outputs(value['course_output_dirs'], result['input_dir'])
            aliases = _validated_aliases(value['course_aliases'])
            modes = _validated_folder_modes(value['course_folder_modes'])
        else:
            raise ValueError('unexpected configuration fields')
        _validate_course_registry(outputs, aliases, modes)
        # Alias validation is global; canonical titles referenced by other maps are
        # allowed without an alias entry.
        _ = version
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise ValueError('目录设置损坏或无效；请重新保存目录设置') from exc
    return {**result, 'course_output_dirs': outputs, 'course_aliases': aliases,
            'course_folder_modes': modes, 'configured': True}


def load_directory_settings() -> dict:
    return _read_settings()


def _write_settings(value: dict) -> None:
    path = directory_settings_path()
    _check_config_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _check_config_path(path)
    payload = {
        'schema_version': SETTINGS_VERSION,
        'input_dir': value['input_dir'],
        'output_dir': value['output_dir'],
        'course_output_dirs': value.get('course_output_dirs', {}),
        'course_aliases': value.get('course_aliases', {}),
        'course_folder_modes': value.get('course_folder_modes', {}),
    }
    # Validate exactly what will be persisted, including cross-course alias collisions.
    outputs = _validated_course_outputs(payload['course_output_dirs'], payload['input_dir'])
    aliases = _validated_aliases(payload['course_aliases'])
    modes = _validated_folder_modes(payload['course_folder_modes'])
    _validate_course_registry(outputs, aliases, modes)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.directories-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        _check_config_path(path)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_directory_settings(input_dir: str | Path | None = None,
                            output_dir: str | Path | None = None) -> dict:
    try:
        current = _read_settings()
    except ValueError:
        if input_dir is None or output_dir is None:
            raise
        current = {'input_dir': str(input_dir), 'output_dir': str(output_dir),
                   'course_output_dirs': {}, 'course_aliases': {}, 'course_folder_modes': {},
                   'configured': False}
    result = _validated(input_dir if input_dir is not None else current['input_dir'],
                        output_dir if output_dir is not None else current['output_dir'])
    updated = {**current, **result, 'configured': True}
    _write_settings(updated)
    return updated


def _alias_owner(label: str, settings: dict) -> str | None:
    normalized = normalize_course_label(label)
    for key, entry in settings.get('course_aliases', {}).items():
        if normalized == normalize_course_label(entry['course_title']):
            return key
        if any(normalized == normalize_course_label(alias) for alias in entry['aliases']):
            return key
    # Course-specific maps also establish canonical course identities.
    for mapping in ('course_output_dirs', 'course_folder_modes'):
        for key, entry in settings.get(mapping, {}).items():
            if normalized == normalize_course_label(entry['course_title']):
                return key
    return None


def resolve_course_title(course_title: str, *, settings: dict | None = None) -> str:
    current = settings if settings is not None else load_directory_settings()
    owner = _alias_owner(course_title, current)
    if owner is None:
        return _course_title(course_title)
    for mapping in ('course_aliases', 'course_output_dirs', 'course_folder_modes'):
        entry = current.get(mapping, {}).get(owner)
        if entry:
            return entry['course_title']
    return _course_title(course_title)


def aliases_for_course(course_title: str, *, settings: dict | None = None) -> list[str]:
    current = settings if settings is not None else load_directory_settings()
    canonical = resolve_course_title(course_title, settings=current)
    entry = current.get('course_aliases', {}).get(course_settings_key(canonical))
    return list(entry['aliases']) if entry else []


def course_folder_mode(course_title: str, *, settings: dict | None = None) -> str:
    current = settings if settings is not None else load_directory_settings()
    canonical = resolve_course_title(course_title, settings=current)
    entry = current.get('course_folder_modes', {}).get(course_settings_key(canonical))
    return entry['mode'] if entry else 'auto'


def save_course_preferences(course_title: str, *, output_dir=_UNSET, aliases=_UNSET,
                            folder_name_mode=_UNSET) -> dict:
    current = _read_settings()
    canonical = resolve_course_title(course_title, settings=current)
    key = course_settings_key(canonical)
    outputs = dict(current.get('course_output_dirs', {}))
    alias_map = dict(current.get('course_aliases', {}))
    modes = dict(current.get('course_folder_modes', {}))

    if aliases is not _UNSET:
        if not isinstance(aliases, (list, tuple)):
            raise ValueError('课程别名必须是列表')
        cleaned = []
        for alias in aliases:
            alias = _course_title(alias)
            if normalize_course_label(alias) != normalize_course_label(canonical):
                cleaned.append(alias)
        # Validate collisions against all other courses before write.
        proposed = dict(alias_map)
        proposed[key] = {'course_title': canonical, 'aliases': cleaned}
        alias_map = _validated_aliases(proposed)

    if output_dir is not _UNSET:
        if output_dir is None or not str(output_dir).strip():
            outputs.pop(key, None)
        else:
            outgoing = _validated_path(output_dir, label='课程输出目录')
            incoming = Path(current['input_dir'])
            if _overlaps(incoming, outgoing):
                raise ValueError('课程输出目录不能与输入目录相同或互相嵌套')
            outputs[key] = {'course_title': canonical, 'output_dir': str(outgoing)}

    if folder_name_mode is not _UNSET:
        if folder_name_mode not in FOLDER_NAME_MODES:
            raise ValueError('课程目录命名模式必须是 auto、always 或 never')
        modes[key] = {'course_title': canonical, 'mode': folder_name_mode}

    updated = {**current, 'course_output_dirs': outputs, 'course_aliases': alias_map,
               'course_folder_modes': modes, 'configured': True}
    _write_settings(updated)
    return updated


def save_course_output_directory(course_title: str, output_dir: str | Path) -> dict:
    return save_course_preferences(course_title, output_dir=output_dir)


def clear_course_output_directory(course_title: str) -> dict:
    return save_course_preferences(course_title, output_dir=None)


def save_course_aliases(course_title: str, aliases: list[str]) -> dict:
    return save_course_preferences(course_title, aliases=aliases)


def save_course_folder_mode(course_title: str, mode: str) -> dict:
    return save_course_preferences(course_title, folder_name_mode=mode)


def configured_course_output(course_title: str | None, *, settings: dict | None = None) -> str | None:
    if not course_title:
        return None
    current = settings if settings is not None else load_directory_settings()
    canonical = resolve_course_title(course_title, settings=current)
    entry = current.get('course_output_dirs', {}).get(course_settings_key(canonical))
    return entry['output_dir'] if isinstance(entry, dict) else None


def resolve_output_directory(course_title: str | None = None, *,
                             explicit: str | Path | None = None,
                             settings: dict | None = None) -> str:
    if explicit is not None and str(explicit).strip():
        return str(Path(explicit).expanduser().resolve())
    current = settings if settings is not None else load_directory_settings()
    return configured_course_output(course_title, settings=current) or str(current['output_dir'])
