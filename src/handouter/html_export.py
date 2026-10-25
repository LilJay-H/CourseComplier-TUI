"""Local HTML export entry points; shared rendering lives in html_core."""
from __future__ import annotations
from pathlib import Path
from .safe_io import safe_path, publish_bytes
from .html_core import STYLE, SafeHTML, render_note

def export_html(source: str | Path, theme: str | None = None) -> list[Path]:
    """Export a note or all notes in a lecture folder, never overwriting files."""
    source = Path(source).expanduser().absolute()
    if source.is_dir():
        from .organization import resolve_workspace
        root = resolve_workspace(source)
        # A reading copy is not a second authored note. Explicit file exports
        # remain supported, but directory export must not multiply derivatives.
        notes = sorted(note for note in (root / 'notes').glob('*.md')
                       if '.typora' not in note.stem)
    else:
        if source.suffix.lower() != '.md':
            raise ValueError('请选择 Markdown 文件或讲次文件夹')
        notes = [source]
        lexical_root = source.parent.parent if source.parent.name == 'notes' else source.parent
        relative = source.relative_to(lexical_root).as_posix()
        root = lexical_root.resolve(strict=True)
        source = safe_path(root, relative)
        notes = [source]
    if not notes:
        raise ValueError('notes 文件夹内没有 Markdown；请先保存 AI 返回的讲义')
    documents = []
    for note in notes:
        safe_path(root, note.relative_to(root).as_posix())
        documents.append((note, render_note(note, root, theme=theme)))
    outputs = []
    for note, content in documents:
        index = 0
        while True:
            target = note.with_suffix('.html') if index == 0 else note.with_name(f'{note.stem}-{index:03d}.html')
            try:
                publish_bytes(root, target.relative_to(root).as_posix(), content.encode('utf-8'))
                outputs.append(target)
                break
            except FileExistsError:
                index += 1
    if (root / 'state.json').is_file() and (root / 'handoff/sources.json').is_file():
        from .tasks import record_html_exports
        record_html_exports(root, list(zip(notes, outputs)))
    return outputs


def export_agent_html(workspace, result) -> dict:
    """Only export this successful Agent run's outputs; report partial delivery."""
    if not result.validation_ok or result.returncode != 0:
        return {'status': 'skipped', 'outputs': {}, 'errors': []}
    outputs, errors = {}, []
    root = Path(workspace).resolve()
    for mode, relative in result.outputs.items():
        try:
            note = root / relative
            if not note.resolve().is_relative_to(root / 'notes'):
                raise ValueError('输出必须位于 notes 文件夹内')
            outputs[mode] = str(export_html(note)[0])
        except (OSError, ValueError, RuntimeError) as exc:
            errors.append(f'{mode}: {exc}')
    return {'status': 'failed' if errors else 'ready', 'outputs': outputs, 'errors': errors}
