"""Environment diagnostics without installing or modifying dependencies."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import io
import json
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass

from .runtime_tools import find_executable


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    required_for: str


def _version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


FEATURES = {'core', 'media', 'asr', 'tui', 'pdf', 'html'}
PACKAGES = (
    ('markdown_it', 'markdown-it-py', 'core'),
    ('reportlab', 'reportlab', 'pdf'),
    ('markdown_it', 'markdown-it-py', 'html'),
    ('mdit_py_plugins', 'mdit-py-plugins', 'html'),
    ('latex2mathml', 'latex2mathml', 'html'),
    ('funasr', 'funasr', 'asr'),
    ('torch', 'torch', 'asr'),
    ('torchaudio', 'torchaudio', 'asr'),
)


def _present(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def _native_asr_probe() -> dict:
    """Import native extensions and compute a synthetic fbank; never load a model."""
    code = """import json
import torch, torchaudio, funasr
from torchaudio.compliance.kaldi import fbank
fbank(torch.zeros(1, 16000), sample_frequency=16000)
devices = ['cpu']
if torch.cuda.is_available(): devices.append('cuda')
if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available(): devices.append('mps')
print('HANDOUTER_NATIVE=' + json.dumps({'devices': devices}))
"""
    try:
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                                encoding='utf-8', errors='replace',
                                # Cold native imports in a fresh Python 3.14
                                # environment can exceed 90 s; still bounded.
                                timeout=180, check=False)
        line = next((line for line in reversed(result.stdout.splitlines())
                     if line.startswith('HANDOUTER_NATIVE=')), None)
        if result.returncode or line is None:
            return {'status': 'failed', 'detail': 'ASR native import/fbank failed; run installer runtime check', 'devices': []}
        devices = json.loads(line.split('=', 1)[1])['devices']
        return {'status': 'passed', 'detail': 'native imports and synthetic fbank passed; no model loaded', 'devices': devices}
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError):
        return {'status': 'failed', 'detail': 'ASR native import/fbank unavailable or timed out', 'devices': []}


def _delivery_probe(feature: str) -> bool:
    """Exercise small PDF/HTML output entirely in memory, without model calls."""
    try:
        if feature == 'pdf':
            from reportlab.pdfgen.canvas import Canvas
            output = io.BytesIO()
            canvas = Canvas(output)
            canvas.drawString(10, 10, 'Course Compiler synthetic PDF check')
            canvas.save()
            return output.getvalue().startswith(b'%PDF-')
        from markdown_it import MarkdownIt
        from mdit_py_plugins.dollarmath import dollarmath_plugin
        from latex2mathml.converter import convert
        parser = MarkdownIt().use(dollarmath_plugin, renderer=lambda content, _options: convert(content))
        return '<math' in parser.render(r'$\frac{1}{2}$')
    except Exception:
        # Do not leak arbitrary third-party import messages or environment values.
        return False


def run_doctor(*, features=None, agent: str | None = None, native_asr: bool = False) -> dict:
    selected = FEATURES.copy() if features is None else set(features) | {'core'}
    if selected - FEATURES:
        raise ValueError('Unknown runtime feature: ' + ', '.join(sorted(selected - FEATURES)))
    if native_asr:
        selected.add('asr')
    checks: list[Check] = []
    checks.append(Check("python", (3, 11) <= sys.version_info[:2] < (4, 0), platform.python_version(), "core"))
    if 'media' in selected:
        for command in ("ffmpeg", "ffprobe"):
            path = find_executable(command)
            checks.append(Check(command, bool(path), path or "not found", "media"))
    for module, package, purpose in PACKAGES:
        if purpose not in selected:
            continue
        present = _present(module)
        version = _version(package) if present else None
        checks.append(Check(module, present, version or ("module available" if present else "not installed"), purpose))
    for feature in ('pdf', 'html'):
        if feature in selected and all(check.ok for check in checks if check.required_for == feature):
            passed = _delivery_probe(feature)
            checks.append(Check(feature + '_runtime', passed,
                                'in-memory synthetic rendering passed' if passed else 'runtime import/rendering failed', feature))
    curses_present = _present('curses') if 'tui' in selected else False
    if 'tui' in selected:
        if platform.system() == "Windows":
            curses_detail = _version("windows-curses") or ("module available" if curses_present else "install windows-curses")
        else:
            curses_detail = "stdlib" if curses_present else "not available"
        checks.append(Check("curses", curses_present, curses_detail, "tui"))
    native = _native_asr_probe() if native_asr else {'status': 'not_checked', 'detail': 'opt in to native imports and synthetic fbank; no model download', 'devices': []}
    if native_asr:
        checks.append(Check('asr_native', native['status'] == 'passed', native['detail'], 'asr'))
    agent_report = {'name': agent, 'available': None, 'login_verified': False, 'generation_verified': False}
    if agent is not None:
        if agent not in {'codex', 'claude', 'manual', 'web', 'none'}:
            raise ValueError('Unknown Agent')
        executable = shutil.which(agent) if agent in {'codex', 'claude'} else None
        available = bool(executable) or agent in {'manual', 'web', 'none'}
        agent_report.update(available=available, executable=executable,
                            detail='executable presence only; login and model generation not verified')
        checks.append(Check(agent, available, agent_report['detail'], 'agent'))
    ok_core = all(check.ok for check in checks if check.required_for == "core")
    report = {
        "platform": platform.system(),
        "tui_backend": "curses-ascii" if curses_present else None,
        "selected_features": sorted(selected),
        "asr_native": native,
        "devices": {'status': native['status'], 'available': native['devices']},
        "agent": agent_report,
        "model_generation_verified": False,
        "asr_languages": ['Chinese', 'English'],
        "checks": [asdict(check) for check in checks],
    }
    for feature in FEATURES:
        report['ok_' + feature] = (ok_core and all(check.ok for check in checks if check.required_for == feature)) if feature in selected else None
    return report


def require_features(features, *, native_asr: bool = False, agent: str | None = None) -> dict:
    """Fail before expensive work; only inspect explicitly requested capabilities."""
    report = run_doctor(features=features, native_asr=native_asr, agent=agent)
    failed = [f"{item['required_for']}/{item['name']}" for item in report['checks'] if not item['ok']]
    if failed:
        raise RuntimeError('运行依赖未满足: ' + ', '.join(failed) + '；请安装相应依赖后重试')
    return report
