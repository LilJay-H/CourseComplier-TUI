"""Visual policy routing/transport and resource checks, not model-quality claims."""

import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT))

from handouter.handoff import default_skill_path
from handouter.skill_plan import RULES_VERSION, SLIDE_LAYOUT_MODULE, SLIDE_READING_MODULE, plan_modules
from handouter.service import prepare_lecture, refresh_handoff
from handouter.agents.manual import create_manual_bundle
from handouter.agents.web import create_web_handoff
from handouter.resources import check_resources, parse_resources


PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')


class SlideVisualPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.transcript = self.root / 'source.txt'
        self.transcript.write_text('合成课程：先给出两种状态，再解释变化原因。', encoding='utf-8')
        self.slides = self.root / 'input-slides'
        self.slides.mkdir()
        (self.slides / '示例 01.png').write_bytes(PNG)
        self.skill = default_skill_path()

    def prepare(self, name='visual', **kwargs):
        return prepare_lecture(self.root / 'output', lecture_id=name, course_title='Synthetic visual test',
            transcript=self.transcript, slides_dir=self.slides, skill_path=self.skill,
            modes=['deep', 'full'], **kwargs).workspace.resolve()

    def test_reading_and_layout_are_loaded_only_with_respective_permission(self):
        for use, embed in [(False, False), (True, False), (True, True)]:
            with self.subTest(use=use, embed=embed):
                modules = plan_modules(self.skill, modes=['deep'], format_profile='clean',
                    use_slides_as_source=use, embed_slides=embed)
                self.assertEqual(SLIDE_READING_MODULE in modules, use)
                self.assertEqual(SLIDE_LAYOUT_MODULE in modules, embed)
                if embed:
                    self.assertLess(modules.index(SLIDE_READING_MODULE), modules.index(SLIDE_LAYOUT_MODULE))
                self.assertNotIn('references/modes/full.md', modules)

    def test_materialized_policy_preserves_content_matching_and_layout_contract(self):
        root = self.prepare(embed_slides=True)
        sources = json.loads((root / 'handoff/sources.json').read_text(encoding='utf-8'))
        self.assertEqual(sources['skill']['rules_version'], RULES_VERSION)
        layout = (root / 'handoff/skill' / SLIDE_LAYOUT_MODULE).read_text(encoding='utf-8')
        prompt = (root / 'handoff/PROMPT.md').read_text(encoding='utf-8')
        for css_class in ('ppt-half', 'ppt-third', 'ppt-cols-2', 'ppt-cols-3', 'ppt-grid-2x2'):
            self.assertIn(css_class, layout)
        self.assertIn('本次图文任务', prompt)
        self.assertIn('slides/reading', prompt)
        self.assertIn('slides/layout', prompt)
        self.assertEqual(layout, (self.skill.parent / SLIDE_LAYOUT_MODULE).read_text(encoding='utf-8'))

    def test_manual_and_web_transport_the_same_materialized_visual_rules(self):
        root = self.prepare(embed_slides=True)
        bundle = create_manual_bundle(root)
        with zipfile.ZipFile(bundle.bundle) as archive:
            for module in (SLIDE_READING_MODULE, SLIDE_LAYOUT_MODULE):
                relative = 'handoff/skill/' + module
                self.assertEqual(archive.read(relative), (root / relative).read_bytes())
        web = create_web_handoff(root)
        materials = web.materials.read_text(encoding='utf-8')
        for module in (SLIDE_READING_MODULE, SLIDE_LAYOUT_MODULE):
            self.assertIn((root / 'handoff/skill' / module).read_text(encoding='utf-8'), materials)
        self.assertTrue(web.slides.is_file())

    def test_refresh_adds_layout_without_changing_history_materials_or_old_notes(self):
        root = self.prepare(use_slides_as_source=True, embed_slides=False)
        old_prompt = (root / 'handoff/PROMPT.md').read_bytes()
        old_reading = (root / 'handoff/skill' / SLIDE_READING_MODULE).read_bytes()
        source_bytes = (root / 'transcript/transcript.txt').read_bytes()
        old_note = root / 'notes/full-001.md'
        old_note.write_text('# Existing user work', encoding='utf-8')
        refreshed = refresh_handoff(root, embed_slides=True, use_slides_as_source=True)
        self.assertEqual(refreshed.archived_prompt.read_bytes(), old_prompt)
        archived_reading = list((root / 'handoff/history').glob('task-*-skill/' + SLIDE_READING_MODULE))
        self.assertEqual(len(archived_reading), 1)
        self.assertEqual(archived_reading[0].read_bytes(), old_reading)
        self.assertTrue((root / 'handoff/skill' / SLIDE_LAYOUT_MODULE).is_file())
        self.assertEqual((root / 'transcript/transcript.txt').read_bytes(), source_bytes)
        self.assertEqual(old_note.read_text(encoding='utf-8'), '# Existing user work')

    def test_grid_resources_are_checked_even_inside_details(self):
        root = self.prepare(embed_slides=True)
        sources = json.loads((root / 'handoff/sources.json').read_text(encoding='utf-8'))
        events = json.loads((root / sources['slides']['index_path']).read_text(encoding='utf-8'))
        relative = events[0]['image_path']
        block = '<details><summary>步骤</summary><div class="ppt-grid ppt-grid-2x2">' + ''.join(
            f'<figure class="ppt-figure"><img src="../{relative}" alt="状态"><figcaption>{i}</figcaption></figure>'
            for i in range(4)) + '</div></details>'
        note = root / 'notes/deep-001.md'
        self.assertEqual(len(parse_resources(block).images), 4)
        self.assertFalse(check_resources(block, root, note, options=sources['options']).errors)
        self.assertTrue(check_resources(block, root, note,
            options={**sources['options'], 'embed_slides': False}).errors)
        self.assertEqual(parse_resources('```html\n' + block + '\n```').images, ())
