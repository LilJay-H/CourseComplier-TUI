"""Synthetic old-note regressions; no native Typora UI acceptance is implied."""
from pathlib import Path
import tempfile
import unittest

from handouter.typora_export import export_typora, typora_markdown
from handouter.resources import markdown_parser, parse_resources


def panel(index):
    return (f'<figure class="ppt-figure"><img src="{index}.png" alt="图 {index}">'
            f'<figcaption>第 {index} 步</figcaption></figure>')


class TyporaMathTests(unittest.TestCase):
    def test_old_html_paragraph_and_summary_math_become_markdown(self):
        source = ('前文原字节\r\n\r\n<details class="complete-detail">\r\n'
                  '<summary>递推 $T(n)$</summary>\r\n\r\n'
                  '<p>保留正文 $T(n)=2T(n/2)+n$。</p>\r\n'
                  '<p>$$\\frac{n}{2} &lt; n$$</p>\r\n</details>\r\n后文原字节')
        result = typora_markdown(source)
        self.assertTrue(result.startswith('前文原字节\r\n\r\n'))
        self.assertTrue(result.endswith('\r\n后文原字节'))
        for removed in ('<details', '</details>', '<summary', '<p>'):
            self.assertNotIn(removed, result)
        for kept in ('### 递推 $T(n)$', '保留正文 $T(n)=2T(n/2)+n$。', '$$\r\n\\frac{n}{2} < n\r\n$$'):
            self.assertIn(kept, result)

    def test_summary_only_math_expands(self):
        self.assertNotIn('<details', typora_markdown('<details><summary>$x$</summary><p>正文</p></details>'))

    def test_plain_folds_and_source_excerpts_remain_exact(self):
        source = ('<details class="complete-verbatim">\r\n<summary>原文节选</summary>\r\n\r\n'
                  '<blockquote>原话 &amp; [链接](x.md)\r\n\r\n第二段。</blockquote>\r\n</details>')
        self.assertEqual(typora_markdown(source), source)

    def test_nested_math_expands_all_ancestors_but_retains_plain_child(self):
        plain = '<details><summary>原文</summary><p>原话 &amp; 字节。</p></details>'
        source = ('<details><summary>外层</summary><p>开始。</p>\n\n'
                  '<details><summary>内层</summary><p>$x^2$</p></details>\n'
                  + plain + '\n<p>结尾。</p></details>')
        result = typora_markdown(source)
        self.assertEqual(result.count('<details>'), 1)
        for content in (plain, '外层', '内层', '$x^2$', '开始。', '结尾。'):
            self.assertIn(content, result)

    def test_code_only_math_does_not_expand_fold(self):
        examples = ['`$x$`', '``<details>$x$</details>``', '<pre><code>$x$</code></pre>',
                    '\n```latex\n$x$\n```\n', '\n~~~latex\n$x$\n~~~\n']
        for example in examples:
            with self.subTest(example=example):
                source = '<details><summary>示例</summary>\n' + example + '\n</details>'
                self.assertEqual(typora_markdown(source), source)

    def test_code_examples_survive_inside_expanded_fold(self):
        examples = ['`<details>$z$</details>`', '<pre><code>&lt;details&gt;$y$</code></pre>',
                    '\n```html\n<details><summary>$q$</summary></details>\n```\n',
                    '\n~~~html\n<details><summary>$q$</summary></details>\n~~~\n']
        for example in examples:
            with self.subTest(example=example):
                source = '<details><summary>证明</summary><p>$x$</p>\n' + example + '\n</details>'
                result = typora_markdown(source)
                self.assertIn(example, result)
                self.assertNotIn('<summary>证明', result)

    def test_literal_folds_and_gallery_examples_are_unchanged(self):
        sample = '<details><summary>$x$</summary>' + '<div class="ppt-gallery ppt-gallery-2">' + panel(1) + panel(2) + '</div></details>'
        for source in ['```html\n' + sample + '\n```\n', '`' + sample + '`', '<pre>' + sample + '</pre>']:
            with self.subTest(source=source[:10]):
                self.assertEqual(typora_markdown(source), source)

    def test_links_images_markdown_code_and_body_are_retained(self):
        source = ('<details><summary>说明</summary><p>$x$ <a href="chapter.md#part">链接</a> '
                  '<img src="one.png" alt="原图"> `x += 1`。</p>\n\n'
                  '[原引用](source.md)\n\n![第二图](two.png)\n</details>')
        result = typora_markdown(source)
        self.assertEqual(set(parse_resources(result).images), {'one.png', 'two.png'})
        self.assertLess(result.index('one.png'), result.index('two.png'))
        for kept in ('$x$', '[链接](<chapter.md#part>)', '`x += 1`', '[原引用](source.md)', '![第二图](two.png)'):
            self.assertIn(kept, result)

    def test_legacy_galleries_keep_order_without_classes_across_blank_lines(self):
        for count in (2, 3):
            with self.subTest(count=count):
                source = (f'<div class="ppt-gallery ppt-gallery-{count}">\n\n'
                          + '\n\n'.join(panel(i) for i in range(count)) + '\n\n</div>')
                result = typora_markdown(source)
                self.assertEqual(result.count('<td '), count)
                self.assertNotIn('class=', result)
                self.assertEqual(parse_resources(result).images, tuple(f'{i}.png' for i in range(count)))

    def test_gallery_in_math_fold_keeps_layout_and_body(self):
        source = ('<details><summary>推导</summary><p>$x$</p>\n'
                  '<div class="ppt-gallery ppt-gallery-2">' + panel(1) + panel(2) + '</div>\n</details>')
        result = typora_markdown(source)
        self.assertIn('<table width="100%">', result)
        self.assertNotIn('<details', result)
        self.assertIn('$x$', result)

    def test_bad_gallery_and_unclosed_math_fold_refuse(self):
        for source in ['<details><summary>$x$</summary>',
                       '<div class="ppt-gallery ppt-gallery-3">' + panel(1) + panel(2) + '</div>']:
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    typora_markdown(source)

    def test_export_rejects_damage_and_opt_in_only_repairs_new_versions(self):
        with tempfile.TemporaryDirectory() as td:
            note = Path(td) / 'note.md'
            original = '<details><summary>公式</summary><p>$x \times y$</p></details>\n'
            note.write_bytes(original.encode())
            with self.assertRaises(ValueError):
                export_typora(note)
            self.assertEqual(list(Path(td).glob('*.typora*.md')), [])
            first = export_typora(note, repair_escapes=True)
            second = export_typora(note, repair_escapes=True)
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertIn(r'$x \times y$', first.read_text())
            self.assertNotIn('<details', first.read_text())
            self.assertEqual(note.read_bytes(), original.encode())

    def test_unknown_damage_still_refuses_after_opt_in(self):
        with tempfile.TemporaryDirectory() as td:
            note = Path(td) / 'note.md'
            note.write_text('$x\x01y$')
            with self.assertRaises(ValueError):
                export_typora(note, repair_escapes=True)
            self.assertEqual(list(Path(td).glob('*.typora*.md')), [])

    def test_explicit_recovery_preserves_even_damaged_code_examples(self):
        with tempfile.TemporaryDirectory() as td:
            note = Path(td) / 'note.md'
            examples = ('\n~~~latex\n$x \times y$\n~~~\n'
                        '<pre><code>$x\x0crac{1}{2}$</code></pre>\n'
                        '`$x \times y$`')
            source = '<details><summary>示例</summary><p>$x \times y$</p>\n' + examples + '\n</details>'
            note.write_bytes(source.encode())
            result = export_typora(note, repair_escapes=True).read_text()
            self.assertIn(examples, result)
            self.assertIn(r'$x \times y$', result)
            self.assertEqual(note.read_bytes(), source.encode())

    def test_nested_quotes_keep_depth_and_paragraphs(self):
        source = ('<details><summary>引用</summary><blockquote><p>第一段 $x$。</p>'
                  '<blockquote><p>二层原话。</p><p>二层续段。</p></blockquote>'
                  '<p>外层结尾。</p></blockquote><p>引用之外。</p></details>')
        result = typora_markdown(source)
        depth, content_depth = 0, {}
        for token in markdown_parser().parse(result):
            if token.type == 'blockquote_open':
                depth += 1
            elif token.type == 'blockquote_close':
                depth -= 1
            elif token.type == 'inline':
                content_depth[token.content] = depth
        self.assertEqual(content_depth['第一段 $x$。'], 1)
        self.assertEqual(content_depth['二层原话。'], 2)
        self.assertEqual(content_depth['二层续段。'], 2)
        self.assertEqual(content_depth['外层结尾。'], 1)
        self.assertEqual(content_depth['引用之外。'], 0)

    def test_ordered_list_start_values_and_nested_lists_render_as_numbers(self):
        source = ('<details><summary>步骤</summary><ol start="3">'
                  '<li><p>第一步 $x$。</p><p>第一步续段。</p>'
                  '<ol start="7"><li>内层七。</li><li>内层八。</li></ol></li>'
                  '<li>第四步。</li><li value="10">第十步。</li></ol></details>')
        result = typora_markdown(source)
        self.assertIn('3. 第一步 $x$。', result)
        self.assertIn('4. 第四步。', result)
        self.assertIn('10. 第十步。', result)
        tokens = markdown_parser().parse(result)
        starts = [t.attrGet('start') or 1 for t in tokens if t.type == 'ordered_list_open']
        self.assertEqual(starts, [3, 7, 10])
        self.assertFalse(any(t.type == 'bullet_list_open' for t in tokens))
        inline = [t.content for t in tokens if t.type == 'inline']
        for content in ('第一步续段。', '内层七。', '内层八。', '第四步。', '第十步。'):
            self.assertIn(content, inline)

    def test_reversed_ordered_list_preserves_rendered_numbers(self):
        source = '<details><summary>$x$</summary><ol reversed><li>三</li><li>二</li><li>一</li></ol></details>'
        tokens = markdown_parser().parse(typora_markdown(source))
        self.assertEqual([t.attrGet('start') or 1 for t in tokens if t.type == 'ordered_list_open'], [3, 2, 1])

    def test_summary_is_a_heading_with_inline_math_and_emphasis(self):
        source = '<details><summary><em>证明</em> $x$</summary><p>正文。</p></details>'
        tokens = markdown_parser().parse(typora_markdown(source))
        heading = next(i for i, t in enumerate(tokens) if t.type == 'heading_open')
        self.assertEqual(tokens[heading].tag, 'h3')
        self.assertEqual(tokens[heading + 1].content, '*证明* $x$')

    def test_display_delimiters_are_separate_lines_only_inside_expanded_folds(self):
        outside = '$$outside$$\n\n'
        code = '`$$literal$$`'
        source = outside + '<details><summary>公式</summary><p>之前$$x &lt; y$$之后</p>' + code + '</details>'
        result = typora_markdown(source)
        self.assertTrue(result.startswith(outside))
        self.assertIn('之前\n\n$$\nx < y\n$$\n\n之后', result)
        self.assertIn(code, result)
        self.assertEqual(typora_markdown(result), result)

    def test_display_math_in_quote_and_list_keeps_structural_prefixes(self):
        source = ('<details><summary>公式</summary><blockquote><p>$$x=1$$</p></blockquote>'
                  '<ol><li><p>$$y=2$$</p></li></ol></details>')
        result = typora_markdown(source)
        self.assertIn('> $$\n> x=1\n> $$', result)
        self.assertIn('1. $$\n   y=2\n   $$', result)

    def test_display_only_summary_keeps_heading_and_standalone_delimiters(self):
        source = '<details><summary>$$x=1$$</summary><p>正文。</p></details>'
        result = typora_markdown(source)
        self.assertIn('### 详情\n\n$$\nx=1\n$$', result)
        self.assertNotIn('### $$', result)
        self.assertIn('正文。', result)

    def test_nested_quote_list_code_payload_and_links_survive(self):
        payload = 'x = "$$example$$"\nprint(x)\n'
        source = ('<details><summary>$x$</summary><blockquote><ol start="2"><li>'
                  '<p>[原引用](source.md)</p>\n\n```python\n' + payload + '```\n'
                  '<blockquote><p>原话。</p></blockquote></li></ol></blockquote></details>')
        result = typora_markdown(source)
        tokens = markdown_parser().parse(result)
        self.assertEqual([t.content for t in tokens if t.type == 'fence'], [payload])
        self.assertEqual([t.attrGet('start') for t in tokens if t.type == 'ordered_list_open'], [2])
        self.assertEqual(sum(t.type == 'blockquote_open' for t in tokens), 2)
        self.assertIn('[原引用](source.md)', result)

    def test_gallery_with_mathematical_caption_is_also_rejected(self):
        source = ('<details><summary>图组</summary><div class="ppt-gallery ppt-gallery-2">'
                  + panel(1).replace('第 1 步', '$x$') + panel(2) + '</div></details>')
        with self.assertRaisesRegex(ValueError, 'HTML 表格内公式'):
            typora_markdown(source)

    def test_math_tables_reject_instead_of_publishing_an_unrenderable_copy(self):
        table = '<table><tbody><tr><td>$x$</td></tr></tbody></table>'
        with tempfile.TemporaryDirectory() as td:
            note = Path(td) / 'table.md'
            for source in [table, '<details><summary>表格</summary>' + table + '</details>',
                           '<details><summary>表格</summary><table><tr><td>$$x$$</td></tr></table></details>']:
                with self.subTest(source=source):
                    note.write_bytes(source.encode())
                    with self.assertRaisesRegex(ValueError, 'HTML 表格内公式'):
                        export_typora(note)
                    self.assertEqual(note.read_bytes(), source.encode())
                    self.assertEqual(list(Path(td).glob('*.typora*.md')), [])

    def test_literal_math_tables_and_nonmath_tables_remain_supported(self):
        table = '<table><tr><td>$x$</td></tr></table>'
        for literal in ['`' + table + '`', '<pre><code>' + table + '</code></pre>',
                        '\n```html\n' + table + '\n```\n']:
            with self.subTest(literal=literal):
                source = '<details><summary>$y$</summary>' + literal + '</details>'
                self.assertIn(literal, typora_markdown(source))
        plain = '<table><tr><td>原文</td></tr></table>'
        self.assertIn(plain, typora_markdown('<details><summary>$x$</summary>' + plain + '</details>'))

    def test_unsupported_or_malformed_lists_refuse_without_dropping_content(self):
        for body in ['<ol type="a"><li>$x$</li></ol>', '<ol><li>$x$</ol>',
                     '<ol>混入正文<li>$x$</li></ol>', '<ol start="-1"><li>$x$</li></ol>']:
            with self.subTest(body=body):
                with self.assertRaises(ValueError):
                    typora_markdown('<details><summary>列表</summary>' + body + '</details>')


if __name__ == '__main__':
    unittest.main()
