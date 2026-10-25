"""Local TUI release gate; no cloud imports or model calls."""
import argparse
import base64
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CORE = (
    'test_build_backend', 'test_tui_workflow', 'test_tui_directories',
    'test_local_asset_input', 'test_media_asr_alignment', 'test_setup_handouter',
    'test_local_agent_workflow', 'test_local_editorial', 'test_split_distribution', 'test_external_collection',
)
FULL = CORE + ('test_handoff_workspace', 'test_agent_interaction',
    'test_task_workflow', 'test_delivery_copy', 'test_html_export',
    'test_platform_compat', 'test_importers_package')


def run_regressions(profile, exhaustive=False):
    sys.path[:0] = [str(ROOT / 'tests'), str(ROOT / 'src'), str(ROOT)]
    os.environ['HANDOUTER_SLOW_TESTS'] = '1' if profile == 'full' else '0'
    from handouter.doctor import require_features
    require_features(['core', 'pdf', 'html', 'tui'])
    names = tuple(p.stem for p in sorted((ROOT / 'tests').glob('test_*.py'))) if exhaustive else (FULL if profile == 'full' else CORE)
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    from _gates import WINDOWS_REASON, MACOS_REASON, POSIX_REASON, SLOW_REASON, EVAL_REASON
    allowed = {WINDOWS_REASON, MACOS_REASON, POSIX_REASON, EVAL_REASON,
               'Windows x64 native required', 'requires real Windows Job Objects',
               'POSIX process-group integration', 'macOS Bash bootstrap integration',
               'symlinks unavailable', 'FIFO unavailable',
               'POSIX executable fixture; native Windows containment has its own tests'}
    if profile == 'core': allowed.add(SLOW_REASON)
    skips = [{'test': t.id(), 'reason': r, 'allowed': r in allowed} for t, r in result.skipped]
    summary = {'profile': profile, 'system': platform.system(), 'python': platform.python_version(),
               'tests': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
               'skips': skips, 'real_model_run': False, 'modules': names}
    (ROOT / 'build').mkdir(exist_ok=True)
    (ROOT / 'build' / ('tests-' + profile + '.json')).write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if result.testsRun and result.wasSuccessful() and not result.expectedFailures and all(s['allowed'] for s in skips) else 1


def installed_smoke(profile: str) -> int:
    # Invoked with -I from a clean environment. Do not add the checkout to sys.path.
    import handouter
    from handouter.doctor import require_features
    from handouter.service import prepare_lecture
    from handouter.agents.web import create_web_handoff
    from handouter.validation import validate_note_output
    if Path(handouter.__file__).resolve().is_relative_to(ROOT / 'src'):
        raise RuntimeError('Installed smoke accidentally imported the source checkout')
    require_features(['core', 'pdf', *(['html'] if profile == 'full' else [])])
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        transcript = root / 'lecture.txt'
        transcript.write_text('合成课程：结论仅在 x > 0 时成立。', encoding='utf-8')
        slides = root / 'slides'
        slides.mkdir()
        (slides / 'one.png').write_bytes(base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='))
        result = prepare_lecture(root / 'out', lecture_id='wheel-smoke', course_title='合成测试',
                                 transcript=transcript, slides_dir=slides, embed_slides=True,
                                 organized=True, lecture_date='2026-01-02')
        workspace = result.workspace
        web = create_web_handoff(workspace)
        assert web.slides.read_bytes().startswith(b'%PDF-')
        sources = json.loads((workspace / 'handoff/sources.json').read_text(encoding='utf-8'))
        note = workspace / sources['expected_output']
        image = next((workspace / 'slides/images').iterdir())
        note.write_text('# 合成讲义\n\n$\\frac{1}{\\sqrt{x}}$\n\n![图](../slides/images/' + image.name + ')', encoding='utf-8')
        assert validate_note_output(workspace).ok
        if profile == 'full':
            from handouter.html_export import export_html
            html = export_html(note)[0].read_text(encoding='utf-8')
            assert '<math' in html and 'data:image/png;base64,' in html
            published = workspace.parent.parent / '成果/notes' / note.name
            assert published.read_bytes() == note.read_bytes()
    print(json.dumps({'installed_smoke': 'passed', 'profile': profile, 'module': handouter.__file__,
                      'real_model_run': False}, ensure_ascii=False))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=['core', 'full'], default='full')
    parser.add_argument('--installed-smoke', action='store_true')
    parser.add_argument('--exhaustive', action='store_true')
    args = parser.parse_args()
    return installed_smoke(args.profile) if args.installed_smoke else run_regressions(args.profile, args.exhaustive)

if __name__ == '__main__':
    raise SystemExit(main())
