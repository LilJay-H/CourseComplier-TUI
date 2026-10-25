"""Regression tests for the stdlib-only PEP 517 backend."""

from __future__ import annotations

import tempfile
from pathlib import Path
import sys
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import handouter_build


class BuildBackendTests(unittest.TestCase):
    """The single normal-wheel build of the core suite; other modules must not rebuild it."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.wheel = Path(cls._tmp.name) / handouter_build.build_wheel(cls._tmp.name)
        cls.archive = zipfile.ZipFile(cls.wheel)
        cls.names = set(cls.archive.namelist())
        cls.dist_info = handouter_build._dist_info()

    @classmethod
    def tearDownClass(cls):
        cls.archive.close()
        cls._tmp.cleanup()

    def test_every_rule_and_runtime_file_is_byte_identical(self):
        skill_root = ROOT / ".agents/skills/zhiyun-lecture-notes"
        expected = {"handouter/assets/zhiyun-lecture-notes/" + path.relative_to(skill_root).as_posix(): path
                    for path in skill_root.rglob("*.md")}
        expected.update({"handouter/" + path.relative_to(ROOT / "src/handouter").as_posix(): path
                         for path in (ROOT / "src/handouter").rglob("*.py") if "__pycache__" not in path.parts})
        expected["handouter/assets/zhiyun_exporter.user.js"] = ROOT / "zhiyun_exporter.user.js"
        for member, path in expected.items():
            with self.subTest(member=member):
                self.assertEqual(self.archive.read(member), path.read_bytes())

    def test_metadata_entry_points_and_no_checkout_paths(self):
        entry = self.archive.read(f"{self.dist_info}/entry_points.txt").decode()
        self.assertIn("handouter = handouter.cli:main", entry)
        self.assertIn("course-compiler = handouter.cli:main", entry)
        metadata = self.archive.read(f"{self.dist_info}/METADATA").decode()
        for line in ("Requires-Python: >=3.11,<4", "Requires-Dist: reportlab", "Requires-Dist: markdown-it-py",
                     "Requires-Dist: windows-curses>=2.4; sys_platform == 'win32'", "Provides-Extra: asr",
                     'Requires-Dist: funasr>=1.4,<2; extra == "asr"'):
            self.assertIn(line, metadata)
        self.assertIn(f"{self.dist_info}/RECORD", self.names)
        self.assertFalse(any(name.endswith(".pth") or "/__pycache__/" in name for name in self.names))

    def test_editable_wheel_uses_project_src_path(self):
        with tempfile.TemporaryDirectory() as td:
            filename = handouter_build.build_editable(td)
            with zipfile.ZipFile(Path(td) / filename) as archive:
                pth = archive.read("handouter-editable.pth").decode().strip()
            self.assertEqual(Path(pth), (ROOT / "src").resolve())


if __name__ == "__main__":
    unittest.main()
