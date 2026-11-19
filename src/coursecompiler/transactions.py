"""Recoverable multi-file Prompt refresh (process interruption, not disk failure)."""
import json
import os
from pathlib import Path
import shutil
import uuid

from .safe_io import safe_path, read_bytes, workspace_lock, publish_bytes


def _checkpoint(name: str) -> None:
    """Fault-injection boundary used by subprocess recovery tests."""


def atomic_bytes(path: Path, data: bytes) -> None:
    temporary = path.with_name('.' + path.name + '.write-' + uuid.uuid4().hex)
    try:
        with temporary.open('xb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _phase(transaction: Path, phase: str) -> None:
    atomic_bytes(transaction / 'journal.json', json.dumps({'phase': phase}).encode())


def _restore(root: Path, transaction: Path) -> None:
    backup = transaction / 'backup'
    safe_path(root, '.refresh-transaction/backup')
    snapshot = {}
    for path in backup.rglob('*'):
        relative = path.relative_to(root).as_posix()
        safe_path(root, relative)
        if path.is_file():
            snapshot[path.relative_to(backup).as_posix()] = read_bytes(root, relative)
    for relative in ('handoff/PROMPT.md', 'handoff/sources.json'):
        if relative not in snapshot:
            raise RuntimeError('事务备份不完整；保留现场')
    if 'state.json' not in snapshot or 'handoff/skill/SKILL.md' not in snapshot:
        raise RuntimeError('事务备份不完整；保留现场')
    for relative in ('handoff/PROMPT.md', 'handoff/sources.json'):
        publish_bytes(root, relative, snapshot[relative], replace=True)
    skill = safe_path(root, 'handoff/skill', missing=True)
    if skill.exists():
        shutil.rmtree(skill)
    skill.mkdir()
    for relative, data in snapshot.items():
        if relative.startswith('handoff/skill/'):
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            publish_bytes(root, relative, data)
    publish_bytes(root, 'state.json', snapshot['state.json'], replace=True)


def recover_refresh(root: Path) -> bool:
    root = Path(root).resolve(strict=True)
    with workspace_lock(root):
        transaction = safe_path(root, '.refresh-transaction', missing=True)
        if not transaction.exists():
            return False
        journal = transaction / 'journal.json'
        if not journal.exists():
            # An unrecognized directory is not ours to erase.
            raise RuntimeError('刷新事务缺少日志；请保留目录并进行恢复检查')
        phase = json.loads(read_bytes(root, '.refresh-transaction/journal.json'))['phase']
        if phase == 'switching':
            _restore(root, transaction)
        elif phase not in {'preparing', 'committed'}:
            raise RuntimeError('未知刷新事务状态，需要恢复检查')
        shutil.rmtree(transaction)
        return True


def refresh_transaction(root: Path, build):
    """Build new controls first; journal before any change to current controls.

    The caller holds the lecture lock. Readers must use the same lock/recovery
    entry. This is recoverable switching, not a multi-file atomic rename.
    """
    root = Path(root).resolve(strict=True)
    transaction = safe_path(root, '.refresh-transaction', missing=True)
    transaction.mkdir()
    _phase(transaction, 'preparing')
    try:
        backup, prepared = transaction / 'backup', transaction / 'prepared'
        for destination in (backup, prepared):
            (destination / 'handoff').mkdir(parents=True)
            (destination / 'notes').mkdir()
            for name in ('state.json', 'manifest.json'):
                (destination / name).write_bytes(read_bytes(root, name))
        # Module planning now inspects the supplied text for formal-reasoning
        # signals. Give the prepared snapshot an independent safe text copy.
        (prepared / 'transcript').mkdir()
        (prepared / 'transcript/transcript.txt').write_bytes(
            read_bytes(root, 'transcript/transcript.txt', role='transcript'))
        for name in ('PROMPT.md', 'sources.json'):
            (backup / 'handoff' / name).write_bytes(read_bytes(root, 'handoff/' + name))
        # Copy only checked regular files, never links.
        for path in (root / 'handoff/skill').rglob('*'):
            relative = path.relative_to(root).as_posix()
            safe_path(root, relative, role='skill')
            target = backup / relative
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(read_bytes(root, relative, role='skill'))
        for path in (root / 'notes').rglob('*'):
            relative = path.relative_to(root).as_posix()
            safe_path(root, relative)
            target = prepared / relative
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch()  # reserve names, never copy historical content
        handoff = build(prepared)
        _checkpoint('new_files')
        from .agents.base import expected_outputs
        sources = json.loads(read_bytes(prepared, 'handoff/sources.json'))
        expected_outputs(sources)
        if not read_bytes(prepared, 'handoff/PROMPT.md').strip():
            raise ValueError('新 Prompt 为空')
        history = safe_path(root, 'handoff/history', missing=True)
        history.mkdir(exist_ok=True)
        index = 1
        while any(history.glob(f'task-{index:03d}-*')):
            index += 1
        prefix = f'task-{index:03d}-'
        _phase(transaction, 'switching')
        for name, checkpoint in [('PROMPT.md', 'archive_prompt'), ('sources.json', 'archive_sources')]:
            target = history / (prefix + name)
            with target.open('xb') as handle:
                handle.write(read_bytes(backup, 'handoff/' + name))
            _checkpoint(checkpoint)
        shutil.copytree(backup / 'handoff/skill', history / (prefix + 'skill'))
        for name in ('PROMPT.md', 'sources.json'):
            publish_bytes(root, 'handoff/' + name, read_bytes(prepared, 'handoff/' + name), replace=True)
        skill = safe_path(root, 'handoff/skill')
        shutil.rmtree(skill)
        shutil.copytree(prepared / 'handoff/skill', skill)
        _checkpoint('skill')
        publish_bytes(root, 'state.json', read_bytes(prepared, 'state.json'), replace=True)
        _checkpoint('state')
        from .validation import validate_workspace
        validation = validate_workspace(root, update_state=True, _transaction=True)
        if not validation.ok:
            raise ValueError('刷新工作区验证失败: ' + '; '.join(validation.errors))
        _phase(transaction, 'committed')
        _checkpoint('committed')
        return handoff, validation, history / (prefix + 'PROMPT.md')
    finally:
        # On ordinary exceptions roll back now. SIGKILL/os._exit leaves the
        # journal for the next service/Agent entry to recover under the lock.
        recover_refresh(root)
