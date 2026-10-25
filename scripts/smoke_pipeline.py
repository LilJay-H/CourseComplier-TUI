"""Installed synthetic pipeline; no model calls, no checkout path injection.

Creates reviewable examples in a NEW output directory. ASR inference and the
writing agent are explicitly simulated; ZIP/HTTP/media/workspace/handoff/
validation/HTML/publication use production code from the installed package.
"""
from __future__ import annotations

import argparse
import base64
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import ModuleType
from unittest.mock import patch
import wave
import zipfile


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
TEXT = '合成课程。设 x 大于零，平方根有定义。这里的材料仅用于验证软件链路。'


def cli(*args):
    result = subprocess.run([sys.executable, '-X', 'utf8', '-I', '-m', 'handouter', *map(str, args)],
                            capture_output=True, text=True, encoding='utf-8')
    if result.returncode:
        raise RuntimeError(result.stderr + result.stdout)
    return json.loads(result.stdout)


def fake_asr_modules():
    modules = {name: ModuleType(name) for name in ('funasr', 'funasr.utils', 'funasr.utils.postprocess_utils')}

    class SyntheticModel:
        def __init__(self, **kwargs):
            assert kwargs['device'] == 'cpu'

        def generate(self, **kwargs):
            assert Path(kwargs['input']).is_file()
            return [{'text': TEXT, 'sentence_info': [{'text': TEXT, 'start': 0, 'end': 1000}]}]

    modules['funasr'].AutoModel = SyntheticModel
    modules['funasr.utils.postprocess_utils'].rich_transcription_postprocess = lambda value: value
    return modules


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    import handouter
    from handouter.agents.base import AgentAvailability
    from handouter.agents.runner import run_cli_agent
    from handouter.agents.manual import create_manual_bundle
    from handouter.agents.web import create_web_handoff
    from handouter.product import exporter_script_path
    from handouter.service import build_from_asset_zip
    from handouter.tasks import retry_html
    from handouter.validation import validate_workspace, validate_note_output
    from handouter.doctor import require_features

    module = Path(handouter.__file__).resolve()
    assert 'site-packages' in module.parts, 'Run with an isolated, non-editable wheel installation'
    assert exporter_script_path().is_relative_to(module.parent)
    manifest = {'schema_version': 1, 'lecture': {'id': 'synthetic-local', 'course_title': '合成示例',
                'lecture_date': '2026-01-02'}, 'transcript': {'path': 'transcript.txt', 'origin': 'provided_transcript'},
                'slides': {'images_dir': 'slides/images'}}
    local_zip = root / 'synthetic-local.zip'
    with zipfile.ZipFile(local_zip, 'w') as archive:
        archive.writestr('handouter-input.json', json.dumps(manifest, ensure_ascii=False))
        archive.writestr('transcript.txt', TEXT)
        archive.writestr('slides/images/one.png', PNG)
    cli('run', '--asset', local_zip, '--output-dir', root / 'local', '--embed-slides', '--agent', 'none')

    # Actual HTTP fetch and FFmpeg processing; only the model inference is fake.
    media_dir = root / 'http-media'
    media_dir.mkdir()
    with wave.open(str(media_dir / 'synthetic.wav'), 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b'\0\0' * 16000)

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(media_dir)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        exporter_zip = root / 'synthetic-exporter.zip'
        with zipfile.ZipFile(exporter_zip, 'w') as archive:
            archive.writestr('course_info.json', json.dumps({'courseName': '合成示例'}))
            archive.writestr('slides_meta.json', json.dumps([{'filename': 'one.png', 'seconds': 0, 'index': 1}]))
            archive.writestr('slides/one.png', PNG)
            archive.writestr('private/source.json', json.dumps({'videoUrl': f'http://127.0.0.1:{server.server_port}/synthetic.wav'}))
        # The synthetic backend deliberately needs neither native ASR packages
        # nor models. Keep the real media/core capability checks.
        with patch.dict(sys.modules, fake_asr_modules()), \
             patch('handouter.doctor.require_features', side_effect=lambda features, **kwargs:
                   require_features([feature for feature in features if feature != 'asr'])):
            result = build_from_asset_zip(root / 'download', lecture_id='synthetic-download',
                                         course_title='合成示例', asset_zip=exporter_zip,
                                         device='cpu', embed_slides=True)
        assert validate_workspace(result.prepare.workspace).ok
        assert (result.prepare.workspace / 'transcript/raw.json').is_file()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    # A real child process receives stdin and writes expected Markdown in the
    # runner's isolated workspace. Never select or execute a real user Agent.
    agent = root / 'synthetic_agent.py'
    agent.write_text('''import json, sys
from pathlib import Path
prompt = sys.stdin.read()
assert 'local-agent.md' in prompt
sources = json.loads(Path('handoff/sources.json').read_text(encoding='utf-8'))
image = next(Path('slides/images').iterdir()).name
for relative in sources['expected_outputs'].values():
    Path(relative).write_text('# 合成讲义\\n\\n本稿由测试桩生成，不代表模型质量。\\n\\n$x > 0$\\n\\n![合成图](../slides/images/' + image + ')\\n', encoding='utf-8')
''', encoding='utf-8')
    outputs = []
    for handoff in sorted(root.rglob('handoff/sources.json')):
        workspace = handoff.parent.parent
        sources = json.loads(handoff.read_text(encoding='utf-8'))
        for rel in [sources['skill']['entry'], *sources['skill']['modules']]:
            assert (workspace / rel).is_file()
        manual = create_manual_bundle(workspace)
        web = create_web_handoff(workspace)
        assert manual.bundle.is_file() and web.slides.read_bytes().startswith(b'%PDF-')
        with patch('handouter.agents.codex.availability', return_value=AgentAvailability('codex', True, sys.executable, 'cli')), \
             patch('handouter.agents.codex.build_command', return_value=[sys.executable, '-X', 'utf8', '-I', str(agent)]):
            run = run_cli_agent(workspace, agent='codex')
        assert run.validation_ok, run.validation_errors
        assert validate_note_output(workspace).ok
        cli('validate', workspace)
        retry_html(workspace)
        for relative in run.outputs.values():
            note = workspace / relative
            html_files = list(note.parent.glob(note.stem + '*.html'))
            assert html_files, str(note)
            html = html_files[0].read_text(encoding='utf-8')
            assert '<math' in html and 'data:image/png;base64,' in html
            outputs.append({'markdown': str(note), 'html': str(html_files[0])})
    assert len(outputs) == 2
    summary = {'status': 'passed', 'installed_module': str(module), 'pipelines': ['local-input-zip', 'exporter-link-http-ffmpeg-mocked-asr'],
               'agent': 'synthetic real subprocess; adapter command patched', 'real_model_run': False,
               'real_course_run': False, 'outputs': outputs}
    (root / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
