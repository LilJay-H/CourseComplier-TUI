"""Full-screen ASCII TUI for Handouter's normal product workflow.

The interface is intentionally dependency-free (stdlib curses) and delegates all
business work to the canonical CLI. Internal manifest/raw/alignment files remain
hidden from the normal user surface.
"""

from __future__ import annotations

import json
from collections import deque
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import unicodedata
from typing import Any

from .agents import available_cli_agents
from .handoff import MODES, MODE_ORDER
from .platform_support import open_directory, start_task_process, terminate_process_tree
from .product import (exporter_script_path, inspect_input_source, normalize_terminal_path,
                      product_artifacts, resolve_input_source)
from .safe_io import read_bytes, publish_bytes
from .credentials import scan_credentials
from .settings import (load_directory_settings, save_directory_settings, save_course_preferences,
                       configured_course_output, resolve_output_directory, resolve_course_title,
                       aliases_for_course, course_folder_mode)
from .organization import resolve_workspace, find_workspace, package_for_workspace, lecture_date

# Kept for compatibility with older tests/importers. The default product TUI is
# deliberately curses/ASCII even when Textual happens to be installed.
App = None


def available_agent_choices() -> tuple[list[str], dict[str, str]]:
    """Return only Agent backends that are usable on this machine."""
    values = ["web", "manual"]
    labels = {"web": "Web Chat (TXT/PDF)", "manual": "GUI handoff"}
    for item in available_cli_agents():
        if not item.available:
            continue
        values.append(item.name)
        labels[item.name] = {"codex": "Codex CLI", "claude": "Claude CLI"}.get(item.name, f"{item.name} CLI")
    return values, labels


def selected_modes(cfg: dict[str, Any]) -> list[str]:
    """Return selected deliverables in stable order.

    ``full`` is the classroom record; ``verbatim`` is the faithful transcript;
    ``complete`` combines a visible tutorial with scoped optional detail layers. Older callers that only provide ``mode``
    continue to work.
    """
    explicit = cfg.get("modes")
    if explicit:
        wanted = set(explicit)
    elif any(key in cfg for key in ("notes_full", "notes_deep", "notes_summary", "notes_complete", "verbatim_transcript", "clean_transcript")):
        wanted = set()
        if cfg.get("notes_deep"):
            wanted.add("deep")
        if cfg.get("notes_summary"):
            wanted.add("summary")
        if cfg.get("notes_full"):
            wanted.add("full")
        if cfg.get("notes_complete"):
            wanted.add("complete")
        # clean_transcript is the legacy TUI key; it now means the faithful
        # verbatim deliverable rather than the newly redefined full mode.
        if cfg.get("verbatim_transcript") or cfg.get("clean_transcript"):
            wanted.add("verbatim")
    else:
        wanted = {str(cfg.get("mode") or "deep")}
    return [mode for mode in MODE_ORDER if mode in wanted]


def build_cli_args(cfg: dict[str, Any]) -> list[str]:
    """Build the canonical CLI invocation used by the ASCII TUI."""
    kind = cfg.get("kind", "product")
    modes = selected_modes(cfg)
    if not modes:
        raise ValueError("至少选择一种交付物")

    if kind == "prompt":
        workspace = _workspace_for_state(cfg)
        if workspace is None:
            raise ValueError('请选择已有讲次以刷新 Prompt')
        args = [
            "prompt", str(workspace),
            "--modes", *modes,
            "--format-profile", cfg.get("format_profile") or "clean",
        ]
        args.append("--use-slides-as-source" if cfg.get("use_slides", True) else "--ignore-slides")
        args.append("--embed-slides" if cfg.get("embed_slides") else "--no-embed-slides")
        args.append("--allow-web" if cfg.get("allow_web") else "--no-allow-web")
        args += ["--agent", str(cfg.get("agent") or "manual")]
        if cfg.get("source_note"):
            args += ["--source-note", normalize_terminal_path(cfg["source_note"]),
                     "--revision-action", str(cfg.get("revision_action") or "derive")]
        elif cfg.get("clear_source_note"):
            args.append("--clear-source-note")
        return args

    if kind == "revise":
        args = ["revise", normalize_terminal_path(cfg["source_note"]),
                "--output-dir", str(cfg.get("output_dir") or "output"),
                "--lecture-id", cfg["lecture"], "--course-title", cfg["title"],
                "--modes", *modes, "--format-profile", cfg.get("format_profile") or "clean",
                "--revision-action", cfg.get("revision_action") or "derive",
                "--agent", str(cfg.get("agent") or "web")]
        if cfg.get("asset"):
            args += ["--slides-zip", normalize_terminal_path(cfg["asset"])]
        if cfg.get("embed_slides"):
            args.append("--embed-slides")
        if not cfg.get("use_slides", True):
            args.append("--ignore-slides")
        if cfg.get("allow_web"):
            args.append("--allow-web")
        if cfg.get('lecture_date'):
            args += ['--lecture-date', lecture_date(cfg['lecture_date'])]
        return args

    if kind == "product":
        args = [
            "run",
            "--output-dir", str(cfg.get("output_dir") or "output"),
            "--modes", *modes,
            "--format-profile", cfg.get("format_profile") or "clean",
        ]
        if cfg.get("asset"):
            flag = {"local_audio": "--audio", "local_media": "--media",
                    "local_transcript": "--transcript"}.get(cfg.get("source_kind"), "--asset")
            args += [flag, normalize_terminal_path(cfg["asset"])]
        elif cfg.get("input_dir"):
            # Backward-compatible CLI construction for old callers/tests.
            args += ["--input-dir", str(cfg["input_dir"])]
        if cfg.get("lecture"):
            args += ["--lecture-id", str(cfg["lecture"])]
        if cfg.get("title"):
            args += ["--course-title", str(cfg["title"])]
        if cfg.get('lecture_date'):
            args += ['--lecture-date', lecture_date(cfg['lecture_date'])]
        args.append("--use-slides-as-source" if cfg.get("use_slides", True) else "--ignore-slides")
        if cfg.get("embed_slides"):
            args.append("--embed-slides")
        if cfg.get("allow_web"):
            args.append("--allow-web")
        args += ["--device", str(cfg.get("device") or "auto")]
        args += ["--agent", str(cfg.get("agent") or "manual")]
        for host in cfg.get("allowed_media_hosts", []):
            args += ["--allowed-media-host", host]
        if cfg.get("collector_source_sha256"):
            args += ["--collector-source-sha256", cfg["collector_source_sha256"]]
        if cfg.get("collector_binding"):
            args += ["--collector-binding", cfg["collector_binding"]]
        return args

    # Advanced compatibility path retained for tests/tools, but not exposed in
    # the normal ASCII form.
    source = cfg["source"]
    if kind == "asset":
        args = ["build-asset", source]
    elif kind == "transcript":
        args = ["prepare", "--transcript", source]
    elif kind == "audio":
        args = ["build-audio", source]
    elif kind == "media":
        args = ["build-media", source]
    else:
        raise ValueError(f"未知输入类型: {kind}")
    args += [
        "--lecture-id", cfg["lecture"],
        "--course-title", cfg["title"],
        "--output-dir", cfg.get("root") or "workspace",
        "--modes", *modes,
        "--format-profile", cfg.get("format_profile") or "clean",
    ]
    if kind != "asset":
        if cfg.get("slides_zip"):
            args += ["--slides-zip", cfg["slides_zip"]]
        else:
            if cfg.get("slides"):
                args += ["--slides-dir", cfg["slides"]]
            if cfg.get("meta"):
                args += ["--slides-meta", cfg["meta"]]
    args.append("--use-slides-as-source" if cfg.get("use_slides", True) else "--ignore-slides")
    if cfg.get("embed_slides"):
        args.append("--embed-slides")
    if cfg.get("allow_web"):
        args.append("--allow-web")
    if kind in {"asset", "audio", "media"}:
        args += ["--device", cfg.get("device") or "auto"]
    return args


def friendly_artifact_paths(cfg: dict[str, Any]) -> dict[str, Any]:
    """Return only artifacts a normal user should care about."""
    root = _workspace_for_state(cfg)
    if root is None:
        raise ValueError('尚未选择讲次')
    if (root / "state.json").is_file():
        try:
            artifacts = product_artifacts(root)
            return {
                "exporter_script": artifacts.exporter_script,
                "prompt": artifacts.prompt,
                "transcript": artifacts.transcript,
                "slides": artifacts.slides,
                "notes": artifacts.notes,
                "expected_note": artifacts.expected_note,
                "expected_notes": artifacts.expected_notes,
            }
        except (OSError, ValueError, KeyError):
            pass
    modes = selected_modes(cfg) or ["deep"]
    expected = {mode: root / "notes" / f"{mode}-001.md" for mode in modes}
    return {
        "exporter_script": exporter_script_path(),
        "prompt": root / "handoff" / "PROMPT.md",
        "transcript": root / "transcript" / "transcript.txt",
        "slides": root / "slides" / "images",
        "notes": root / "notes",
        "expected_note": next(iter(expected.values())),
        "expected_notes": expected,
    }


def _terminate_process_group(process: subprocess.Popen[str]) -> threading.Thread | None:
    return terminate_process_tree(process)


def _char_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def _display_width(text: str) -> int:
    return sum(_char_width(char) for char in text)


def _clip_columns(text: str, columns: int) -> str:
    if columns <= 0:
        return ""
    used = 0
    result: list[str] = []
    for char in text:
        width = _char_width(char)
        if used + width > columns:
            break
        result.append(char)
        used += width
    return "".join(result)


def _safe_add(stdscr, row: int, col: int, text: str, attr: int = 0) -> None:
    height, width = stdscr.getmaxyx()
    if row < 0 or row >= height or col >= width:
        return
    available = max(0, width - col - 1)
    if available:
        try:
            stdscr.addstr(row, col, _clip_columns(text, available), attr)
        except Exception:
            pass


def _panel_add(stdscr, row: int, col: int, right: int, text: str, attr: int = 0) -> None:
    """Clip by terminal cells inside a panel, preserving its right border."""
    _safe_add(stdscr, row, col, _clip_columns(text, max(0, right - col)), attr)


def _box(stdscr, top: int, left: int, bottom: int, right: int, title: str = "") -> None:
    if right <= left + 1 or bottom <= top + 1:
        return
    _safe_add(stdscr, top, left, "+" + "-" * max(0, right - left - 1) + "+")
    for row in range(top + 1, bottom):
        _safe_add(stdscr, row, left, "|")
        _safe_add(stdscr, row, right, "|")
    _safe_add(stdscr, bottom, left, "+" + "-" * max(0, right - left - 1) + "+")
    if title:
        _panel_add(stdscr, top, left + 2, right, f" {title} ")


def _checkbox(value: bool) -> str:
    return "[x]" if value else "[ ]"


def _edit_value(stdscr, label: str, current: str, *, initial: str = "") -> tuple[str, bool]:
    """Edit one text value with real cursor movement and paste/drop support.

    Left/right/home/end are consumed by the editor, so they no longer conflict
    with option switching in the main form. Esc cancels; Enter commits.
    """
    import curses

    value = current
    cursor = len(value)
    if initial:
        value = initial
        cursor = len(value)
    curses.curs_set(1)
    try:
        while True:
            height, width = stdscr.getmaxyx()
            if height < 3 or width < 12:
                stdscr.erase()
                _safe_add(stdscr, 0, 0, 'Resize terminal; Esc cancels')
                stdscr.refresh()
                key = stdscr.get_wch()
                if key == '\x1b':
                    return current, False
                if key in ('\n', '\r'):
                    return value.strip(), True
                if isinstance(key, str) and key.isprintable():
                    value = value[:cursor] + key + value[cursor:]
                    cursor += len(key)
                continue
            row = max(1, height - 1)
            prompt = f"EDIT {label}: "
            prompt_width = _display_width(prompt)
            available = max(8, width - prompt_width - 4)
            start = cursor
            used = 0
            while start > 0:
                char_width = _char_width(value[start - 1])
                if used + char_width > max(1, available - 1):
                    break
                start -= 1
                used += char_width
            end = start
            used = 0
            while end < len(value):
                char_width = _char_width(value[end])
                if used + char_width > available:
                    break
                used += char_width
                end += 1
            visible = value[start:end]
            stdscr.move(row, 0)
            stdscr.clrtoeol()
            _safe_add(stdscr, row, 1, prompt + visible)
            cursor_col = 1 + prompt_width + _display_width(value[start:cursor])
            try:
                stdscr.move(row, min(width - 2, cursor_col))
            except Exception:
                pass
            stdscr.refresh()
            key = stdscr.get_wch()
            if key in ("\n", "\r"):
                return value.strip(), True
            if key == "\x1b":
                return current, False
            if isinstance(key, int):
                if key == curses.KEY_LEFT:
                    cursor = max(0, cursor - 1)
                elif key == curses.KEY_RIGHT:
                    cursor = min(len(value), cursor + 1)
                elif key == curses.KEY_HOME:
                    cursor = 0
                elif key == curses.KEY_END:
                    cursor = len(value)
                elif key == curses.KEY_DC and cursor < len(value):
                    value = value[:cursor] + value[cursor + 1 :]
                elif key == curses.KEY_BACKSPACE and cursor > 0:
                    value = value[: cursor - 1] + value[cursor:]
                    cursor -= 1
                continue
            if key in ("\x7f", "\b"):
                if cursor > 0:
                    value = value[: cursor - 1] + value[cursor:]
                    cursor -= 1
                continue
            if key == "\x01":  # Ctrl-A
                cursor = 0
                continue
            if key == "\x05":  # Ctrl-E
                cursor = len(value)
                continue
            if key == "\x15":  # Ctrl-U
                value = ""
                cursor = 0
                continue
            if key == "\x0b":  # Ctrl-K
                value = value[:cursor]
                continue
            if isinstance(key, str) and key.isprintable():
                value = value[:cursor] + key + value[cursor:]
                cursor += len(key)
    finally:
        curses.curs_set(0)


def _load_asset_into_state(state: dict[str, Any]) -> str:
    raw = str(state.get("asset") or "").strip()
    if not raw:
        return "请选择输入文件：支持资产 ZIP、本地音频/视频或 UTF-8 TXT。"
    normalized = normalize_terminal_path(raw)
    asset = resolve_input_source(state.get('input_dir') or '.', normalized)
    state["asset"] = str(asset)
    state.pop("allowed_media_hosts", None)
    state.pop("collector_source_sha256", None)
    state.pop("collector_binding", None)
    identity = inspect_input_source(asset)
    detected_kind = getattr(identity, "source_kind", "zhiyun_asset_zip")
    state["source_kind"] = detected_kind
    if not getattr(identity, "has_slides", detected_kind == "zhiyun_asset_zip"):
        state["use_slides"] = False
        state["embed_slides"] = False
    same = state.get('_identified_asset') == str(asset) or state.get('lecture') == identity.lecture_id
    directories = load_directory_settings()
    detected_title = resolve_course_title(identity.course_title, settings=directories) if identity.course_title else None
    if detected_title and (not same or not state.get('title')):
        state["title"] = detected_title
    if not same or not state.get('lecture_date'):
        state['lecture_date'] = getattr(identity, 'lecture_date', None) or ''
    if not same or not state.get('lecture'):
        state["lecture"] = identity.lecture_id
    if state.get('title') and not state.get('_output_dir_manual'):
        state['default_output_dir'] = directories['output_dir']
        state['output_dir'] = resolve_output_directory(state['title'], settings=directories)
        state['course_output_configured'] = configured_course_output(state['title'], settings=directories) is not None
        state.pop('workspace', None)
    state['_identified_asset'] = str(asset)
    return f"Loaded: {asset.name}  |  {detected_kind}  |  course/id identified."


def _confirm_collection(stdscr, state: dict[str, Any]) -> bool:
    """Show the exact domains before launching a browser-export task."""
    import curses
    import textwrap
    from .external_collector import inspect_browser_export
    details = inspect_browser_export(state["asset"])
    offset = 0
    while True:
        height, width = stdscr.getmaxyx()
        rows = ["Download stays on this computer. Upload needs separate consent.", "", "Exact media domains:"]
        for host in details["media_hosts"]:
            rows.extend(textwrap.wrap(host, max(12, width - 6)))
        stdscr.erase()
        _safe_add(stdscr, 1, 2, " CONFIRM LOCAL COLLECTION ")
        for index, row in enumerate(rows[offset:offset + max(1, height - 6)]):
            _safe_add(stdscr, index + 3, 2, row)
        _safe_add(stdscr, height - 2, 2, "Y: confirm domains and collect | N/Esc: cancel | Up/Down: scroll")
        stdscr.refresh()
        key = stdscr.getch()
        if key in (ord("y"), ord("Y")):
            state["allowed_media_hosts"] = list(details["media_hosts"])
            state["collector_source_sha256"] = details["source_sha256"]
            state["collector_binding"] = details["collector_binding"]
            return True
        if key in (ord("n"), ord("N"), 27):
            return False
        if key == curses.KEY_DOWN:
            offset = min(max(0, len(rows) - max(1, height - 6)), offset + 1)
        elif key == curses.KEY_UP:
            offset = max(0, offset - 1)


def _commit_text_value(state: dict[str, Any], field_key: str, value: str) -> str | None:
    if field_key in {"asset", "output_dir", "input_dir"}:
        value = normalize_terminal_path(value)
    if field_key == 'lecture_date':
        value = lecture_date(value) or ''
    state[field_key] = value
    if field_key in {'asset', 'output_dir', 'lecture'}:
        state.pop('workspace', None)
    if field_key == 'output_dir':
        state['_output_dir_manual'] = True
    if field_key == 'title' and value and not state.get('_output_dir_manual'):
        directories = load_directory_settings()
        value = resolve_course_title(value, settings=directories)
        state['title'] = value
        state['default_output_dir'] = directories['output_dir']
        state['output_dir'] = resolve_output_directory(value, settings=directories)
        state['course_output_configured'] = configured_course_output(value, settings=directories) is not None
        state.pop('workspace', None)
    if field_key == "asset" and value:
        return _load_asset_into_state(state)
    return None


def _directory_dialog(stdscr, state: dict[str, Any]) -> bool:
    """Persist global defaults and, when a course is known, one course override."""
    import curses
    try:
        current = load_directory_settings()
    except (OSError, ValueError):
        current = {'input_dir': str(state.get('input_dir') or ''),
                   'output_dir': str(state.get('default_output_dir') or state.get('output_dir') or ''),
                   'course_output_dirs': {}, 'course_aliases': {}, 'course_folder_modes': {}}
    course_title = str(state.get('title') or '').strip()
    if course_title:
        course_title = resolve_course_title(course_title, settings=current)
    draft = {'input_dir': str(current.get('input_dir') or state.get('input_dir') or ''),
             'default_output_dir': str(current.get('output_dir') or state.get('output_dir') or '')}
    fields = [('input_dir', 'Input Dir'), ('default_output_dir', 'Default Output')]
    if course_title:
        draft['course_output_dir'] = configured_course_output(course_title, settings=current) or ''
        draft['course_aliases'] = ', '.join(aliases_for_course(course_title, settings=current))
        draft['folder_name_mode'] = course_folder_mode(course_title, settings=current)
        fields.extend([('course_output_dir', 'Course Output'), ('course_aliases', 'Aliases'),
                       ('folder_name_mode', 'Folder Name')])
    selected = 0
    message = ('Aliases: comma separated. Folder Name: auto/always/never. auto omits the course name when Output Dir is already the course/alias.'
               if course_title else 'Set the global default directories. Load a course to add course aliases/output rules.')
    while True:
        height, width = stdscr.getmaxyx()
        stdscr.erase()
        _box(stdscr, 0, 0, max(7, height - 2), max(20, width - 2), ' DIRECTORIES ')
        for index, (key, label) in enumerate(fields):
            shown = draft[key] or ('<inherit default>' if key == 'course_output_dir' else '<empty>')
            _panel_add(stdscr, 3 + index * 2, 3, width - 3, f'{label}: {shown}',
                       curses.A_REVERSE if selected == index else 0)
        save_index = len(fields)
        save_row = 3 + len(fields) * 2
        _panel_add(stdscr, save_row, 3, width - 3, '[ SAVE ]',
                   curses.A_REVERSE if selected == save_index else 0)
        _panel_add(stdscr, save_row + 2, 3, width - 3, message)
        _panel_add(stdscr, save_row + 4, 3, width - 3, 'Tab/Arrows select | Enter edit/save | Esc cancel')
        stdscr.refresh()
        key = stdscr.getch()
        if key == 27:
            return False
        if key in (9, 258):
            selected = (selected + 1) % (save_index + 1)
        elif key in (353, 259):
            selected = (selected - 1) % (save_index + 1)
        elif key in (10, 13):
            if selected < save_index:
                name, label = fields[selected]
                value, committed = _edit_value(stdscr, label, draft[name])
                if committed:
                    if name in {'input_dir', 'default_output_dir', 'course_output_dir'}:
                        draft[name] = normalize_terminal_path(value) if value.strip() else ''
                    else:
                        draft[name] = value.strip()
            else:
                try:
                    saved = save_directory_settings(draft['input_dir'], draft['default_output_dir'])
                    for name in ('input_dir', 'output_dir'):
                        Path(saved[name]).mkdir(parents=True, exist_ok=True)
                    if course_title:
                        course_value = draft.get('course_output_dir', '').strip()
                        aliases = [part.strip() for part in re.split(r'[,，]', draft.get('course_aliases', '')) if part.strip()]
                        mode = draft.get('folder_name_mode', 'auto').strip().lower()
                        saved = save_course_preferences(course_title,
                            output_dir=(course_value if course_value and Path(course_value).expanduser().resolve() != Path(saved['output_dir']).resolve() else None),
                            aliases=aliases, folder_name_mode=mode)
                        course_title = resolve_course_title(course_title, settings=saved)
                        if configured_course_output(course_title, settings=saved):
                            Path(resolve_output_directory(course_title, settings=saved)).mkdir(parents=True, exist_ok=True)
                    effective = resolve_output_directory(course_title or None, settings=saved)
                except (OSError, ValueError) as exc:
                    message = f'Cannot save: {exc}'
                    continue
                if state.get('output_dir') != effective:
                    state.pop('workspace', None)
                state.update(input_dir=saved['input_dir'], default_output_dir=saved['output_dir'],
                             output_dir=effective, title=course_title or state.get('title', ''), directories_configured=True,
                             course_output_configured=bool(course_title and configured_course_output(course_title, settings=saved)),
                             _output_dir_manual=False)
                return True


def _view_text(stdscr, title: str, path: Path) -> None:
    if not path.is_file():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    offset = 0
    while True:
        height, width = stdscr.getmaxyx()
        stdscr.erase()
        _box(stdscr, 0, 0, max(3, height - 2), max(10, width - 2), title)
        page = max(1, height - 5)
        for row, line in enumerate(lines[offset : offset + page], start=2):
            _panel_add(stdscr, row, 2, width - 3, line)
        _safe_add(stdscr, height - 1, 1, "Up/Down scroll | PgUp/PgDn | Q/Esc return")
        stdscr.refresh()
        key = stdscr.getch()
        if key in (ord("q"), ord("Q"), 27):
            return
        if key in (258, ord("j")):
            offset = min(max(0, len(lines) - page), offset + 1)
        elif key in (259, ord("k")):
            offset = max(0, offset - 1)
        elif key == 338:
            offset = min(max(0, len(lines) - page), offset + page)
        elif key == 339:
            offset = max(0, offset - page)


def _workspace_for_state(cfg: dict[str, Any]) -> Path | None:
    if cfg.get('workspace'):
        return resolve_workspace(normalize_terminal_path(str(cfg['workspace'])))
    lecture = str(cfg.get("lecture") or "").strip()
    if not lecture:
        return None
    output = Path(cfg.get('output_dir') or 'output').expanduser()
    return find_workspace(output, lecture) or (output / lecture).resolve()


def _load_workspace_into_state(state: dict[str, Any], value: str) -> None:
    workspace = resolve_workspace(normalize_terminal_path(value))
    sources = json.loads(read_bytes(workspace, 'handoff/sources.json'))
    if not isinstance(sources, dict) or not isinstance(sources.get('options'), dict):
        raise ValueError('请选择含有效 handoff/sources.json 的讲次文件夹')
    package = package_for_workspace(workspace)
    state.update(workspace=str(workspace), output_dir=str(package.parent if package else workspace.parent), lecture=str(sources.get('lecture_id') or workspace.name),
                 title=str(sources.get('course_title') or workspace.name), asset='', source_note='',
                 clear_source_note=False, _output_dir_manual=True)
    if package:
        record = json.loads(read_bytes(package, '.handouter-collection.json'))
        state['lecture_date'] = record.get('lecture_date') or ''
    else:
        state['lecture_date'] = ''
    options = sources['options']
    modes = sources.get('modes') or [sources.get('mode', 'deep')]
    if not isinstance(modes, list) or not modes or any(not isinstance(mode, str) or mode not in MODES for mode in modes):
        raise ValueError('讲次交付模式无效，请先修复任务配置')
    for mode, key in [('deep', 'notes_deep'), ('summary', 'notes_summary'), ('full', 'notes_full'), ('verbatim', 'verbatim_transcript'), ('complete', 'notes_complete')]:
        state[key] = mode in modes
    state.pop('modes', None)
    state.update(use_slides=bool(options.get('use_slides_as_source')), embed_slides=bool(options.get('embed_slides')),
                 allow_web=bool(options.get('allow_web')), format_profile=options.get('format_profile') or 'clean')
    revision = sources.get('revision')
    if isinstance(revision, dict) and revision.get('action') in {'derive', 'illustrate'}:
        state['revision_action'] = revision['action']


_PREFERENCE_FIELDS = {'output_dir', 'workspace', 'lecture', 'notes_deep', 'notes_summary', 'notes_full', 'notes_complete',
                      'verbatim_transcript', 'format_profile', 'use_slides', 'embed_slides', 'allow_web', 'device', 'agent'}


def _safe_preferences(raw: dict) -> dict:
    result = {}
    boolean_fields = {'notes_deep', 'notes_summary', 'notes_full', 'notes_complete', 'verbatim_transcript', 'use_slides', 'embed_slides', 'allow_web'}
    enums = {'agent': {'web', 'manual', 'codex', 'claude'}, 'format_profile': {'clean', 'traceable'},
             'device': {'auto', 'cpu', 'mps', 'cuda'}}
    for key, value in raw.items():
        if key not in _PREFERENCE_FIELDS:
            continue
        if key in boolean_fields:
            if type(value) is bool:
                result[key] = value
            continue
        if not isinstance(value, str) or len(value) > 4096 or re.search(r'[\x00-\x1f]', value) or '://' in value:
            continue
        if key in enums and value not in enums[key]:
            continue
        credentials = scan_credentials(value)
        if not credentials.errors and not credentials.warnings:
            result[key] = value
    return result


def _preference_root(storage: Path | None = None) -> Path:
    # Local to this launcher directory; never writes user/global configuration.
    return storage if storage is not None else Path.cwd() / 'output'


def _load_preferences(storage: Path | None = None) -> dict:
    try:
        raw = json.loads(read_bytes(_preference_root(storage), '.handouter-preferences.json'))
        if not isinstance(raw, dict):
            return {}
        return _safe_preferences(raw)
    except (OSError, ValueError):
        return {}


def _save_preferences(state: dict[str, Any], storage: Path | None = None) -> None:
    payload = _safe_preferences(state)
    root = _preference_root(storage)
    root.mkdir(parents=True, exist_ok=True)
    publish_bytes(root, '.handouter-preferences.json', (json.dumps(payload, ensure_ascii=False) + '\n').encode('utf-8'), replace=True)


def _result_rows(workspace: Path) -> list[str]:
    from .tasks import plan_task
    plan = plan_task(workspace)
    state = json.loads(read_bytes(workspace, 'state.json'))
    sources = json.loads(read_bytes(workspace, 'handoff/sources.json'))
    if not isinstance(state, dict) or not isinstance(sources, dict):
        raise ValueError('任务控制文件必须是 JSON 对象')
    material_status = {
        'busy': 'not checked while another task is running',
        'invalid': 'not confirmed; inspect validation errors',
        'recovery_required': 'recovery required before validation',
    }.get(plan.action, 'validated task snapshot (transcript / selected PPT / Prompt)')
    rows = [f'Workspace: {workspace}', f'Task ID: {plan.task_id}', f'Status: {plan.action}',
            'Materials: ' + material_status, '', 'Markdown:']
    package = package_for_workspace(workspace)
    publication = state.get('publication') or {}
    if package:
        rows[0:0] = [f'Results folder: {package / "成果"}',
                     'Publication: ' + str(publication.get('status') or 'not published')]
        rows.extend('Publication error: ' + _diagnostic_line('course-compiler: error: ' + str(error))
                    for error in publication.get('errors', []))
    outputs = sources.get('expected_outputs') or {sources.get('mode', 'deep'): sources.get('expected_output')}
    for mode, path in outputs.items():
        rows.append(f'  {mode}: ' + ('ready' if mode in plan.completed else 'pending') + f' | {path}')
    rows.extend(['', 'Quality (no automatic semantic certification):'])
    for mode, quality in getattr(plan, 'quality', {}).items():
        rows.append('  ' + mode + ': ' + quality['editorial_state'])
        for key in ('structure_resources', 'quote_locations', 'source_preservation', 'claim_support', 'technical', 'rendering'):
            item = quality[key]
            rows.append('    ' + key + ': ' + item['status'] + ' [' + item['basis'] + ']')
    delivery = state.get('html_delivery') or {}
    rows.extend(['', 'HTML: ' + str(delivery.get('status') or 'not exported')])
    for mode, path in (delivery.get('outputs') or {}).items():
        rows.append(f'  {mode}: {path}')
    for error in delivery.get('errors') or []:
        rows.append('  ' + _diagnostic_line('course-compiler: error: ' + str(error)))
    web = sorted(workspace.glob('handoff/*web-chat-*/Materials.txt'))
    bundles = sorted(workspace.glob('handoff/*gui-handoff-*.zip'))
    rows.extend(['', 'Uploads: ' + (str(web[-1].parent) if web else str(bundles[-1]) if bundles else 'not prepared')])
    next_steps = {'complete': 'Open results; P creates a new task version.', 'html_only': 'RUN retries HTML only; Markdown stays unchanged.',
                  'publish_only': 'RUN repairs result copies only; no ASR or Agent.',
                  'partial': 'RUN continues pending modes; I imports a returned Markdown.',
                  'generate': 'RUN creates a handoff or starts the selected Agent.',
                  'refresh_required': 'P refreshes the task; choose a new lecture for changed materials.',
                  'invalid': 'Inspect validation errors; existing results are preserved.',
                  'busy': 'Wait for the running task to finish.', 'recovery_required': 'Continue to recover the interrupted task.'}
    rows.extend(['', 'Next: ' + next_steps.get(plan.action, 'Inspect task status.')])
    rows.extend('Review: ' + _diagnostic_line('course-compiler: error: ' + str(message)) for message in (*plan.errors, *plan.warnings))
    return rows


def _show_results(stdscr, workspace: Path) -> None:
    rows = _result_rows(workspace)
    offset = 0
    while True:
        height, width = stdscr.getmaxyx()
        stdscr.erase()
        _box(stdscr, 0, 0, height - 2, width - 2, ' RESULTS / TASK STATUS ')
        for row, text in enumerate(rows[offset:offset + max(1, height - 5)], 2):
            _panel_add(stdscr, row, 2, width - 3, text)
        _safe_add(stdscr, height - 1, 1, 'Up/Down scroll | O open results folder | Q/Esc return')
        stdscr.refresh()
        key = stdscr.getch()
        if key in (ord('q'), ord('Q'), 27):
            return
        if key in (ord('o'), ord('O')):
            package = package_for_workspace(workspace)
            open_directory(package / '成果' if package else workspace / 'notes')
        elif key == 258:
            offset = min(max(0, len(rows) - max(1, height - 5)), offset + 1)
        elif key == 259:
            offset = max(0, offset - 1)


def _handoff_matches_selection(cfg: dict[str, Any], workspace: Path) -> bool:
    state_path = workspace / "state.json"
    if not state_path.is_file():
        raise ValueError(f"已有输出目录缺少 state.json，拒绝覆盖: {workspace}")
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    handoff = payload.get("handoff")
    if not isinstance(handoff, dict) or handoff.get("status") != "ready":
        raise ValueError(f"已有 workspace 尚未 handoff ready，拒绝自动覆盖: {workspace}")
    current_modes = handoff.get("modes")
    if not isinstance(current_modes, list):
        current_modes = [str(handoff.get("mode") or "deep")]
    return (
        list(current_modes) == selected_modes(cfg)
        and str(handoff.get("format_profile") or "clean") == str(cfg.get("format_profile") or "clean")
        and bool(handoff.get("use_slides_as_source")) == bool(cfg.get("use_slides", True))
        and bool(handoff.get("embed_slides")) == bool(cfg.get("embed_slides"))
        and bool(handoff.get("allow_web")) == bool(cfg.get("allow_web"))
    )


def _task_cli_args(cfg: dict[str, Any], *, prompt_only: bool) -> tuple[list[str], str]:
    """Choose build, refresh, or Agent continuation without redoing finished ASR."""
    if cfg.get("source_note"):
        workspace = _workspace_for_state(cfg)
        action = str(cfg.get('revision_action') or 'derive')
        if action == 'illustrate' and len(selected_modes(cfg)) != 1:
            raise ValueError('仅补图只允许选择一个交付模式')
        same_revision = False
        if workspace is not None and workspace.is_dir():
            sources = json.loads(read_bytes(workspace, 'handoff/sources.json'))
            revision = sources.get('revision') if isinstance(sources, dict) else None
            source = Path(normalize_terminal_path(str(cfg['source_note']))).expanduser()
            if isinstance(revision, dict) and not source.exists():
                raise ValueError('提交的讲义路径已不存在；按 W 打开已有讲次可继续已保存快照，无需找回原件。若要提交新讲义，请重新选择有效文件。')
            if isinstance(revision, dict):
                from .revision import read_revision_note
                submitted = read_revision_note(source, action=action)
                same_revision = revision.get('sha256') == submitted['sha256'] and revision.get('action') == action
        if prompt_only or not same_revision:
            revision_cfg = dict(cfg)
            revision_cfg['kind'] = 'prompt' if workspace is not None and workspace.exists() else 'revise'
            return build_cli_args(revision_cfg), 'refresh' if revision_cfg['kind'] == 'prompt' else 'build'
    if cfg.get("clear_source_note"):
        prompt_only = True
    if prompt_only:
        prompt_cfg = dict(cfg)
        prompt_cfg["kind"] = "prompt"
        return build_cli_args(prompt_cfg), "refresh"

    workspace = _workspace_for_state(cfg)
    if workspace is not None and workspace.exists():
        if not workspace.is_dir():
            raise ValueError(f"输出目标已存在但不是目录: {workspace}")
        from .tasks import plan_task
        fingerprint = None
        if cfg.get('asset'):
            from .stage_cache import asset_fingerprint, source_fingerprint
            source = Path(normalize_terminal_path(str(cfg['asset'])))
            fingerprint = (asset_fingerprint(source) if cfg.get('source_kind') in {None, 'zhiyun_asset_zip', 'local_asset_package'}
                           else source_fingerprint(source))
        selection = {'modes': selected_modes(cfg), 'format_profile': cfg.get('format_profile') or 'clean',
                     'use_slides_as_source': bool(cfg.get('use_slides', True)), 'embed_slides': bool(cfg.get('embed_slides')),
                     'allow_web': bool(cfg.get('allow_web'))}
        plan = plan_task(workspace, selection=selection, input_fingerprint=fingerprint)
        if plan.action == 'refresh_required':
            raise ValueError('材料、模式或规则已改变；请按 P 明确刷新 Prompt，或为新材料选择新讲次。')
        if plan.action in {'invalid', 'busy'}:
            raise ValueError('任务暂不可运行；R 查看具体状态。' + '；'.join(plan.errors))
        if plan.action == 'complete':
            return ['task-status', str(workspace)], 'complete'
        if plan.action == 'publish_only':
            return ['continue', str(workspace), '--agent', 'none'], 'publish_only'
        if plan.action == 'html_only':
            return ['retry-html', str(workspace)], 'html_only'
        return ['continue', str(workspace), '--agent', str(cfg.get('agent') or 'manual')], 'continue'

    product_cfg = dict(cfg)
    product_cfg["kind"] = "product"
    return build_cli_args(product_cfg), "build"


def _task_error_summary(lines: list[str], code: int) -> str:
    for line in reversed(lines):
        text = line.strip()
        if text.startswith(("course-compiler: error:", "handouter: error:")):
            return _diagnostic_line(text)
    tail = [line.strip() for line in list(lines)[-4:] if line.strip()]
    return " | ".join(tail) if tail else f"Task ended with code {code}."


def _diagnostic_line(line: str) -> str:
    credentials = scan_credentials(line)
    if credentials.errors or credentials.warnings:
        return 'course-compiler: error: diagnostic contained credential-like data; details hidden'
    if line.startswith('handouter: error:'):
        line = 'course-compiler: error:' + line[len('handouter: error:'):]
    if not line.startswith(('[progress]', 'course-compiler: error:')):
        return '[task output omitted; use Results for delivery status]'
    line = re.sub(r'https?://[^\s]+', '[URL hidden]', line, flags=re.I)
    return line[:300]


def _append_task_log(lines, line: str) -> None:
    clean = _diagnostic_line(line.rstrip())
    if not lines or lines[-1] != clean:
        lines.append(clean)


def _run_task(stdscr, state: dict[str, Any], *, prompt_only: bool, command: list[str] | None = None) -> tuple[bool, str]:
    cfg = dict(state)
    cli_args, action = (command, command[0]) if command else _task_cli_args(cfg, prompt_only=prompt_only)
    kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "bufsize": 1,
        "env": os.environ.copy(),
        "encoding": "utf-8",
        "errors": "replace",
    }
    kwargs["env"]["PYTHONUTF8"] = "1"
    process, job = start_task_process(cli_args, **kwargs)
    cancellation = None
    lines = deque(maxlen=200)
    progress_line = {"text": "Working..."}

    def reader() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            clean = _diagnostic_line(line.rstrip())
            if clean.startswith("[progress]"):
                progress_line["text"] = clean
            else:
                _append_task_log(lines, line)

    thread = threading.Thread(target=reader, daemon=True)
    reader_started = False
    try:
        thread.start()
        reader_started = True
        stdscr.nodelay(True)
        while process.poll() is None:
            height, width = stdscr.getmaxyx()
            stdscr.erase()
            _box(stdscr, 0, 0, max(5, height - 2), max(20, width - 2), " COURSE COMPILER / RUNNING ")
            _panel_add(stdscr, 2, 2, width - 3, progress_line["text"] + "  Q/Esc = cancel process group")
            for row, line in enumerate(list(lines)[-max(1, height - 7) :], start=4):
                _panel_add(stdscr, row, 2, width - 3, line)
            stdscr.refresh()
            key = stdscr.getch()
            if key in (ord("q"), ord("Q"), 27):
                cancellation = _terminate_process_group(process)
                break
            time.sleep(0.08)
        code = process.wait()
    finally:
        try:
            if process.poll() is None and cancellation is None:
                cancellation = _terminate_process_group(process)
            if job is not None:
                job.close()
            if cancellation is not None:
                cancellation.join()
            process.wait()
            if reader_started:
                thread.join(timeout=1)
        finally:
            stdscr.nodelay(False)
    if code != 0:
        return False, _task_error_summary(lines, code) + ' Existing results retained; R shows status and retry action.'
    if action in {'complete', 'html_only', 'publish_only', 'retry-html', 'import-note', 'task-status'}:
        return True, 'Results updated. R shows Markdown, HTML and the next action; semantic review remains required.'
    if action == 'continue':
        return True, 'Prepared workspace continued. R shows each deliverable and upload files.'
    agent = str(cfg.get("agent") or "manual")
    if agent == "web":
        root = _workspace_for_state(cfg) / 'handoff'
        exports = sorted(root.glob("*web-chat-*/Materials.txt"))
        location = str(exports[-1].parent) if exports else str(root)
        return True, f"Upload Materials.txt and Slides.pdf (if present) from: {location}"
    if action == "bundle":
        root = _workspace_for_state(cfg) / 'handoff'
        bundles = sorted(root.glob("*gui-handoff-*.zip"), key=lambda path: path.stat().st_mtime_ns if path.exists() else 0)
        suffix = f" {bundles[-1]}" if bundles else ""
        return True, "Existing workspace reused; GUI handoff bundle ready:" + suffix
    if action == "agent":
        return True, f"Existing workspace reused; {agent} finished; outputs structurally validated."
    if action == "refresh":
        return True, ("Prompt refreshed and GUI handoff bundle ready." if agent == "manual" else f"Prompt refreshed; {agent} finished; outputs structurally validated.")
    if action == "reuse":
        return True, "Existing workspace is valid."
    return True, ("GUI handoff bundle ready." if agent == "manual" else f"{agent} finished; outputs structurally validated.")


def _run_curses() -> None:  # pragma: no cover - interactive UI
    import curses

    def main(stdscr) -> None:
        curses.curs_set(0)
        stdscr.keypad(True)
        state: dict[str, Any] = {
            "asset": "",
            "output_dir": "output",
            "title": "",
            "lecture": "",
            "notes_full": False,
            "notes_complete": False,
            "notes_deep": True,
            "notes_summary": False,
            "verbatim_transcript": False,
            "format_profile": "clean",
            "use_slides": True,
            "embed_slides": False,
            "allow_web": False,
            "device": "auto",
            "agent": "web",
            "source_note": "",
            "revision_action": "derive",
        }
        state.update(_load_preferences())
        try:
            directories = load_directory_settings()
            directory_error = ''
        except (OSError, ValueError) as exc:
            from .platform_support import desktop_directory
            root = desktop_directory() / 'Handouter'
            directories = dict(input_dir=str(root / '输入'), output_dir=str(root / '输出'), course_output_dirs={},
                               course_aliases={}, course_folder_modes={}, configured=False)
            directory_error = str(exc)
        state.update(input_dir=directories['input_dir'], default_output_dir=directories['output_dir'],
                     output_dir=directories['output_dir'], directories_configured=directories['configured'],
                     course_output_configured=False, _output_dir_manual=False, lecture_date='')
        # key, label, kind. Actions behave like buttons.
        fields = [
            ("asset", "Source File", "text"),
            ("output_dir", "Output Dir", "text"),
            ("title", "Course", "text"),
            ("lecture", "Lecture ID", "text"),
            ("notes_full", "Full polished notes", "toggle"),
            ("notes_deep", "Deep notes", "toggle"),
            ("notes_summary", "Summary notes", "toggle"),
            ("verbatim_transcript", "Verbatim transcript", "toggle"),
            ("format_profile", "Reading format", "format"),
            ("use_slides", "Use PPT as source", "toggle"),
            ("embed_slides", "Embed PPT", "toggle"),
            ("allow_web", "Allow web supplements", "toggle"),
            ("device", "ASR device", "device"),
            ("agent", "Agent interaction", "agent"),
            ("run", "RUN", "action"),
            ("prompt", "REFRESH PROMPT", "action"),
            ("view", "VIEW PROMPT", "action"),
            ("quit", "QUIT", "action"),
            ('workspace', 'OPEN WORKSPACE', 'action'),
            ('results', 'RESULTS', 'action'),
            ('import', 'IMPORT RETURN', 'action'),
            ('html', 'EXPORT HTML', 'action'),
            ('note', 'SUBMIT NOTE', 'action'),
            ('revision', 'DERIVE / ILLUSTRATE', 'action'),
            ('notes_complete', 'Complete layered handout', 'toggle'),
            ('directories', 'DIRECTORIES', 'action'),
            ('lecture_date', 'Lecture Date YYYY-MM-DD', 'text'),
        ]
        # Keep existing action IDs while navigating in the visible field order.
        focus_order = [*range(8), 24, *range(8, 24), 25, 26]
        selected = 0
        status = "Ready. Focus Source File, then drag an asset ZIP, audio, video, or UTF-8 TXT."
        if not state['directories_configured']:
            status = 'First use: DIRECTORIES -> confirm Input Dir / Output Dir -> SAVE. ' + directory_error
        device_values = ["auto", "mps", "cuda", "cpu"]
        agent_values, agent_labels = available_agent_choices()
        if state['agent'] not in agent_values:
            state['agent'] = agent_values[0]
        if state.get('device') not in device_values:
            state['device'] = 'auto'

        while True:
            height, width = stdscr.getmaxyx()
            if height < 30 or width < 94:
                stdscr.erase()
                _safe_add(stdscr, 1, 2, "Course Compiler ASCII TUI needs at least 94x30. Resize terminal; Q quits.")
                stdscr.refresh()
                if stdscr.getch() in (ord("q"), ord("Q")):
                    return
                continue

            stdscr.erase()
            right = width - 2
            _box(stdscr, 0, 0, height - 2, right, " COURSE COMPILER :: LOCAL ASSETS -> NOTES ")
            exporter = exporter_script_path()
            exporter_name = exporter.name if exporter else "not found"
            _panel_add(stdscr, 1, 2, right - 1, f"New course: ZIP/audio/video/TXT | Continue: W Workspace | Revise: N Note | Exporter: {exporter_name}")
            _panel_add(stdscr, 2, 2, right - 1, "Tab/Arrows move | Enter edit/action | F5 run | Letters type in text fields; hotkeys on buttons/options")
            _panel_add(stdscr, 3, 4, right - 3, 'Workspace: ' + str(_workspace_for_state(state) or '<W opens an existing course without a source file>'))

            _box(stdscr, 4, 2, 10, right - 2, " SOURCE / DESTINATION ")
            labels = [
                ("Source File", state["asset"] or "<drag ZIP, audio, video, or TXT here>"),
                ("Output Dir", state["output_dir"]),
                ("Course", state["title"] or "<auto from metadata/file name>"),
                ("Lecture ID", state["lecture"] or "<auto from metadata/content>"),
            ]
            for idx, (label, value) in enumerate(labels):
                attr = curses.A_REVERSE if selected == idx else 0
                field_width = max(1, right - 3 - 4 - 17)
                _panel_add(stdscr, 5 + idx, 4, right - 3, f"{label:<11} [ {_clip_columns(value, field_width)} ]", attr)
            note_label = str(state.get("source_note") or "<none>")
            _panel_add(stdscr, 9, 4, right - 3, f"[ N SUBMIT NOTE ] [ G {state['revision_action']} ] Note: {note_label}",
                       curses.A_REVERSE if selected in {22, 23} else 0)

            mid = width // 2
            _box(stdscr, 11, 2, 19, mid - 1, " DELIVERABLES ")
            _panel_add(stdscr, 12, 5, mid - 2, 'Date: ' + (state['lecture_date'] or '<unknown; Enter YYYY-MM-DD>'),
                       curses.A_REVERSE if selected == 26 else 0)
            deliver_rows = [
                (4, 13, "Full 完整课堂笔记", "notes_full"),
                (5, 14, "Deep 图文教程讲义", "notes_deep"),
                (6, 15, "Summary 速览讲义", "notes_summary"),
                (7, 16, "Verbatim 清晰逐字稿", "verbatim_transcript"),
                (24, 17, "Complete 可展开讲义", "notes_complete"),
            ]
            for idx, row, label, key in deliver_rows:
                attr = curses.A_REVERSE if selected == idx else 0
                _panel_add(stdscr, row, 5, mid - 2, f"{_checkbox(bool(state[key]))} {label}", attr)

            _box(stdscr, 11, mid, 19, right - 2, " OPTIONS ")
            option_lines = [
                (8, 13, f"Reading format  < {state['format_profile']} >"),
                (9, 14, f"{_checkbox(bool(state['use_slides']))} Use PPT as source"),
                (10, 15, f"{_checkbox(bool(state['embed_slides']))} Embed PPT"),
                (11, 16, f"{_checkbox(bool(state['allow_web']))} Allow web supplements"),
                (12, 17, f"ASR device      < {state['device']} >"),
                (13, 18, f"Agent           < {agent_labels[state['agent']]} >"),
            ]
            for idx, row, text in option_lines:
                attr = curses.A_REVERSE if selected == idx else 0
                _panel_add(stdscr, row, mid + 3, right - 3, text, attr)

            _box(stdscr, 20, 2, 23, right - 2, " ACTIONS ")
            view_label = "[ OPEN UPLOADS ]" if state["agent"] == "web" else "[ VIEW PROMPT ]"
            actions = [(14, "[ RUN ]"), (15, "[ REFRESH PROMPT ]"), (16, view_label), (17, "[ QUIT ]")]
            col = 5
            for idx, text in actions:
                attr = curses.A_REVERSE if selected == idx else 0
                _panel_add(stdscr, 21, col, right - 3, text, attr)
                col += len(text) + 3
            col = 5
            for idx, text in [(18, '[ W WORKSPACE ]'), (19, '[ R RESULTS ]'), (20, '[ I IMPORT RETURN ]'), (21, '[ H HTML ]')]:
                _panel_add(stdscr, 22, col, right - 3, text, curses.A_REVERSE if selected == idx else 0)
                col += len(text) + 2

            _box(stdscr, 24, 2, height - 3, right - 2, " STATUS ")
            modes = selected_modes(state)
            _panel_add(stdscr, 25, 4, right - 3, f"Deliverables: {', '.join(modes) if modes else '<none selected>'}  |  Agent: {agent_labels[state['agent']]}")
            _panel_add(stdscr, 26, 4, right - 3, status)
            _panel_add(stdscr, 27, 4, right - 3, '[ DIRECTORIES ] Input Dir: ' + state['input_dir'], curses.A_REVERSE if selected == 25 else 0)
            stdscr.refresh()

            key = stdscr.getch()
            field_key, label, kind = fields[selected]
            if kind == 'text' and 32 <= key <= 126:
                value, committed = _edit_value(stdscr, label, str(state[field_key]), initial=chr(key))
                if committed:
                    try:
                        message = _commit_text_value(state, field_key, value)
                        if message:
                            status = message
                    except Exception as exc:
                        status = f'Invalid {label}: {exc}'
                continue
            if key in (10, 13) and selected >= 18 and kind == 'action':
                if selected == 25:
                    if _directory_dialog(stdscr, state):
                        status = 'Directory defaults saved. Existing course folders were not moved.'
                    continue
                key = ord({18: 'w', 19: 'r', 20: 'i', 21: 'h', 22: 'n', 23: 'g'}[selected])
            if key in (ord('w'), ord('W')):
                value, committed = _edit_value(stdscr, 'Existing lecture workspace (no ZIP needed)', str(_workspace_for_state(state) or ''))
                if committed and value:
                    try:
                        _load_workspace_into_state(state, value)
                        try:
                            _save_preferences(state)
                        except (OSError, ValueError):
                            pass
                        status = 'Workspace loaded. RUN continues the task; R Results; I imports returned Markdown.'
                    except (OSError, ValueError) as exc:
                        status = f'Cannot open workspace: {exc}'
                continue
            if key in (ord('r'), ord('R')):
                try:
                    workspace = _workspace_for_state(state)
                    if workspace is None:
                        raise ValueError('W: select a workspace first')
                    _show_results(stdscr, workspace)
                except (OSError, ValueError, RuntimeError) as exc:
                    status = f'Results unavailable: {exc}'
                continue
            if key in (ord('i'), ord('I')):
                try:
                    from .tasks import plan_task
                    workspace = _workspace_for_state(state)
                    if workspace is None:
                        raise ValueError('W: select the intended workspace first')
                    plan = plan_task(workspace)
                    mode, chosen = _edit_value(stdscr, 'Mode: deep / summary / full / verbatim / complete', next(iter(plan.pending), 'deep'))
                    if not chosen:
                        continue
                    value, committed = _edit_value(stdscr, 'Returned Markdown for this task ' + plan.task_id, '')
                    if committed and value:
                        ok, status = _run_task(stdscr, state, prompt_only=False, command=[
                            'import-note', str(workspace), normalize_terminal_path(value), '--mode', mode, '--task-id', plan.task_id])
                        if ok:
                            _show_results(stdscr, workspace)
                except (OSError, ValueError, RuntimeError) as exc:
                    status = f'Import failed; old results retained: {exc}'
                continue
            if key in (ord('h'), ord('H')):
                default = str(_workspace_for_state(state) or state.get('source_note') or '')
                value, committed = _edit_value(stdscr, 'Markdown file or lecture folder', default)
                if committed and value.strip():
                    try:
                        from .html_export import export_html
                        import webbrowser
                        target = Path(normalize_terminal_path(value)).expanduser()
                        if target.is_dir():
                            target = resolve_workspace(target)
                        if target.is_dir() and (target / 'handoff/sources.json').is_file():
                            ok, status = _run_task(stdscr, state, prompt_only=False, command=['retry-html', str(target)])
                        else:
                            outputs = export_html(target)
                            webbrowser.open(outputs[0].as_uri())
                            status = f'Exported {len(outputs)} offline HTML file(s) beside Markdown: {outputs[0].name}'
                    except (OSError, ValueError, RuntimeError) as exc:
                        status = f'HTML export failed: {exc}'
                continue
            if key in (ord("n"), ord("N")):
                value, committed = _edit_value(stdscr, "Existing note (.md/.txt; empty clears)", state["source_note"])
                if committed:
                    state["source_note"] = normalize_terminal_path(value) if value.strip() else ""
                    state["clear_source_note"] = not bool(state["source_note"])
                    if state["source_note"]:
                        path = Path(state["source_note"])
                        if not state.get("title"):
                            state["title"] = path.stem
                        if not state.get("lecture"):
                            state["lecture"] = "note-" + str(int(time.time()))
                        workspace = _workspace_for_state(state)
                        if not state.get("asset") and (workspace is None or not workspace.exists()):
                            state["use_slides"] = False
                    status = "Note submitted. G switches derive / illustrate. RUN reuses prepared materials."
                continue
            if key in (ord("g"), ord("G")):
                state["revision_action"] = "illustrate" if state["revision_action"] == "derive" else "derive"
                if state["revision_action"] == "illustrate":
                    state["use_slides"] = True
                status = "Illustrate: select one mode and provide PPT; preserves existing text." if state["revision_action"] == "illustrate" else "Derive selected modes from submitted note."
                continue
            if key in (ord("q"), ord("Q")):
                try:
                    _save_preferences(state)
                except (OSError, ValueError):
                    pass
                return
            if key in (9, 258):  # Tab / Down
                selected = focus_order[(focus_order.index(selected) + 1) % len(focus_order)]
                continue
            if key in (353, 259):  # Shift-Tab / Up
                selected = focus_order[(focus_order.index(selected) - 1) % len(focus_order)]
                continue
            if key == curses.KEY_F5:
                selected = 14
                key = 10
            elif key in (ord("p"), ord("P")):
                selected = 15
                key = 10

            field_key, label, kind = fields[selected]
            if key in (260, 261):  # left/right
                if kind == "format":
                    state[field_key] = "traceable" if state[field_key] == "clean" else "clean"
                elif kind == "device":
                    pos = device_values.index(state[field_key])
                    state[field_key] = device_values[(pos + (1 if key == 261 else -1)) % len(device_values)]
                elif kind == "agent":
                    pos = agent_values.index(state[field_key])
                    state[field_key] = agent_values[(pos + (1 if key == 261 else -1)) % len(agent_values)]
                continue
            if key == ord(" "):
                if kind == "toggle":
                    state[field_key] = not bool(state[field_key])
                    if field_key == "embed_slides" and state[field_key]:
                        state["use_slides"] = True
                elif kind == "format":
                    state[field_key] = "traceable" if state[field_key] == "clean" else "clean"
                elif kind == "device":
                    pos = device_values.index(state[field_key])
                    state[field_key] = device_values[(pos + 1) % len(device_values)]
                elif kind == "agent":
                    pos = agent_values.index(state[field_key])
                    state[field_key] = agent_values[(pos + 1) % len(agent_values)]
                continue
            if key not in (10, 13):
                continue

            if kind == "text":
                value, committed = _edit_value(stdscr, label, str(state[field_key]))
                if committed:
                    try:
                        message = _commit_text_value(state, field_key, value)
                        if message:
                            status = message
                    except Exception as exc:
                        status = f"Invalid {label}: {exc}"
                continue
            if kind == "toggle":
                state[field_key] = not bool(state[field_key])
                if field_key == "embed_slides" and state[field_key]:
                    state["use_slides"] = True
                continue
            if kind in {"format", "device", "agent"}:
                if kind == "format":
                    state[field_key] = "traceable" if state[field_key] == "clean" else "clean"
                elif kind == "device":
                    pos = device_values.index(state[field_key])
                    state[field_key] = device_values[(pos + 1) % len(device_values)]
                else:
                    pos = agent_values.index(state[field_key])
                    state[field_key] = agent_values[(pos + 1) % len(agent_values)]
                continue

            if field_key == "quit":
                try:
                    _save_preferences(state)
                except (OSError, ValueError):
                    pass
                return
            if field_key == "view":
                if not state.get("lecture"):
                    status = "No lecture selected yet."
                    continue
                if state.get("agent") == "web":
                    workspace = _workspace_for_state(state)
                    exports = sorted(workspace.glob("handoff/*web-chat-*/Materials.txt")) if workspace else []
                    if not exports:
                        status = "No web upload files yet. RUN or REFRESH PROMPT first."
                    else:
                        try:
                            open_directory(exports[-1].parent)
                            status = "Select Materials.txt and Slides.pdf (if present) in the opened folder."
                        except (OSError, subprocess.SubprocessError) as exc:
                            status = f"Cannot open upload folder: {exc}"
                    continue
                path = friendly_artifact_paths(state)["prompt"]
                if isinstance(path, Path) and path.is_file():
                    _view_text(stdscr, " PROMPT.md ", path)
                else:
                    status = f"Prompt not found: {path}"
                continue
            if field_key in {"run", "prompt"}:
                current_workspace = _workspace_for_state(state)
                if not state.get('directories_configured') and (current_workspace is None or not current_workspace.is_dir()):
                    if not _directory_dialog(stdscr, state):
                        status = 'Confirm directory defaults with DIRECTORIES before starting a task.'
                        continue
                modes = selected_modes(state)
                if not modes:
                    status = "Select at least one deliverable."
                    continue
                workspace = _workspace_for_state(state)
                if field_key == "run" and not state.get("source_note") and (workspace is None or not workspace.is_dir()):
                    try:
                        status = _load_asset_into_state(state)
                    except Exception as exc:
                        status = f"Cannot load source file: {exc}"
                        continue
                    if not state.get("title"):
                        status = "Course title is missing; edit Course manually."
                        continue
                if not state.get("lecture"):
                    status = "Lecture ID is required."
                    continue
                if (field_key == "run" and not state.get("source_note")
                        and (workspace is None or not workspace.is_dir())
                        and state.get("source_kind") == "zhiyun_asset_zip"):
                    try:
                        if not _confirm_collection(stdscr, state):
                            status = "Collection cancelled; original browser ZIP retained."
                            continue
                    except (OSError, ValueError) as exc:
                        status = str(exc)
                        continue
                try:
                    try:
                        _save_preferences(state)
                    except (OSError, ValueError):
                        pass
                    ok, message = _run_task(stdscr, state, prompt_only=(field_key == "prompt"))
                    status = message
                    if ok:
                        paths = friendly_artifact_paths(state)
                        expected = paths.get("expected_notes", {})
                        if isinstance(expected, dict):
                            status += " Targets: " + ", ".join(Path(path).name for path in expected.values())
                        workspace = _workspace_for_state(state)
                        if workspace is not None:
                            _show_results(stdscr, workspace)
                except Exception as exc:
                    status = f"Task failed: {exc}"

    curses.wrapper(main)


def run_tui() -> None:
    try:
        import curses  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("ASCII TUI requires Python curses support.") from exc
    _run_curses()
