"""Standalone distribution boundaries, using temporary build products."""
import ast
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile

import handouter_build as build

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ('handouter_server', 'handouter_client', 'handouter_protocol', 'handouter_local')


class SplitDistributionTests(unittest.TestCase):
    def test_windows_source_bundle_keeps_its_build_allowlist_complete(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('tui_release', ROOT / 'scripts/build_release.py')
        release = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(release)
        with tempfile.TemporaryDirectory() as temporary:
            bundle = release.build_bundle('windows-x64', temporary, include_runtime=False)
            with zipfile.ZipFile(bundle) as archive:
                prefix = archive.namelist()[0].split('/')[0] + '/'
                entries = json.loads(archive.read(prefix + 'release-files.json'))
                self.assertTrue(all(prefix + entry in archive.namelist() for entry in entries))

    def test_local_source_has_no_mixed_repository_imports(self):
        for path in (ROOT / 'src').rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                    [node.module or ''] if isinstance(node, ast.ImportFrom) else [])
                self.assertFalse(any(n.startswith(FORBIDDEN) for n in names), str(path))

    def test_wheel_and_sdist_are_local_only_and_keep_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            with zipfile.ZipFile(Path(temporary) / build.build_wheel(temporary)) as wheel:
                names = wheel.namelist()
                self.assertFalse(any(n.startswith(FORBIDDEN) for n in names))
                self.assertIn('handouter/sensevoice_normalize.py', names)
                self.assertIn('handouter/assets/zhiyun-lecture-notes/SKILL.md', names)
                self.assertIn(build._dist_info() + '/NOTICE.md', names)
                metadata = wheel.read(build._dist_info() + '/METADATA').decode()
                self.assertNotIn('Provides-Extra: cloud', metadata)
                self.assertNotIn('Provides-Extra: client', metadata)
            with tarfile.open(Path(temporary) / build.build_sdist(temporary)) as sdist:
                names = [n.split('/', 1)[1] for n in sdist.getnames()]
                self.assertEqual(set(names), set(json.loads((ROOT / 'release-files.json').read_text())))
                self.assertFalse(any(n.startswith(('apps/', 'deploy/', '.git/', 'build/', 'scratch/')) for n in names))
                self.assertIn('provenance.json', names)
                self.assertIn('tests/test_split_distribution.py', names)
                self.assertIn('AGENTS.md', names)

