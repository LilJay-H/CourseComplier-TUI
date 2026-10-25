"""Synthetic regressions: reject malformed layout without changing source text."""
import tempfile
from pathlib import Path
import unittest

from handouter.resources import parse_resources
from handouter.html_core import render_note


class StructureRegressionTests(unittest.TestCase):
    def test_html5_optional_end_tags_are_accepted_without_rewriting(self):
        for text in (
            '<div><p>第一段<div>下一块</div></div>',
            '<ul><li>第一项<li>第二项</ul>',
            '<ul><li>外层<ul><li>内层</li></ul></li></ul>',
            '<table><tbody><tr><td>A<td>B<tr><th>C</tbody></table>',
            '<table><tr><td>外<table><tr><td>内</td></tr></table></td></tr></table>',
            '<table><tr><td><p>段落一<td>段落二</tr></table>',
        ):
            with self.subTest(text=text):
                self.assertFalse(parse_resources(text).errors)
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    note = root / 'note.md'
                    note.write_text(text, encoding='utf-8')
                    rendered = render_note(note, root)
                    self.assertIn(text, rendered)
                    self.assertEqual(note.read_text(encoding='utf-8'), text)

    def test_mismatched_figure_and_grid_are_rejected(self):
        for text in (
            '<figure class="ppt-figure"><figcaption>证据甲</figcaption></div>\n正文乙',
            '<div class="ppt-grid"><figure><figcaption>甲</figcaption></div></figure>',
            '<details><summary>来源</summary><div>正文</details></div>',
            '<figure><figure>重复容器</figure></figure>',
            '<figcaption>脱离图片</figcaption>',
        ):
            with self.subTest(text=text):
                self.assertTrue(parse_resources(text).errors)
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory); note = root / "note.md"
                    note.write_text(text, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        render_note(note, root)
                    self.assertEqual(note.read_text(), text)

    def test_valid_nested_layout_and_literal_code_are_preserved(self):
        text = '''<div class="ppt-grid"><figure><figcaption>甲</figcaption></figure><figure><figcaption>乙</figcaption></figure></div>

<details><summary>来源</summary><p>合成来源</p></details>

```html
<figure></div>
```
'''
        self.assertFalse(parse_resources(text).errors)
