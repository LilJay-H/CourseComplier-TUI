"""Regression fixtures derived from failure shapes, not private course data."""
import importlib.util
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest

from handouter.math_markup import (math_spans, math_diagnostics, normalize_table_math,
                                   repair_math_escapes)


class _RenderedText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_math = False
        self.prose, self.math, self.cells = [], [], []
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == 'math':
            self.in_math = True
        if tag == 'td':
            self.cell = []

    def handle_endtag(self, tag):
        if tag == 'math':
            self.in_math = False
        if tag == 'td' and self.cell is not None:
            self.cells.append(''.join(self.cell))
            self.cell = None

    def handle_data(self, data):
        (self.math if self.in_math else self.prose).append(data)
        if self.cell is not None:
            self.cell.append(data)


class MathMarkupTests(unittest.TestCase):
    def test_delimiters_do_not_eat_following_prose(self):
        text = r'$$a+b$$ 加上 $n$ 次，总代价 $3n$。'
        self.assertEqual([(s.content, s.display) for s in math_spans(text)],
                         [('a+b', True), ('n', False), ('3n', False)])
        self.assertEqual(list(math_spans('$x\n正文$')), [])
        self.assertEqual(list(math_spans(r'\$5 and \$10')), [])
        self.assertEqual(list(math_spans('$5 and $10')), [])

    def test_code_attributes_comments_and_existing_math_are_literal(self):
        text = ('<p title="$attribute$">$x<y$</p><code>$code$</code>'
                '<pre>$pre$</pre><!-- $comment$ --><math>$native$</math> `$literal$`')
        self.assertEqual([s.content for s in math_spans(text)], ['x<y'])

    def test_table_pipes_normalized_without_changing_other_text(self):
        table = '| name | formula | explanation |\n| --- | --- | --- |\n' + r'| tree | $|a-b|$ | tail |' + '\n'
        result = normalize_table_math(table)
        self.assertIn(r'$\|a-b\|$', result)
        self.assertEqual(normalize_table_math(result), result)
        for prefix in ('```md\n', '~~~md\n'):
            literal = prefix + table + prefix[:3] + '\n'
            self.assertEqual(normalize_table_math(literal), literal)
        self.assertEqual(normalize_table_math('text $|x|$\n'), 'text $|x|$\n')

    def test_resource_scanner_sees_cells_after_formula_pipes(self):
        from handouter.resources import parse_resources
        table = ('| formula | image |\n| --- | --- |\n'
                 + r'| $|x|$ | ![plot](missing.png) |' + '\n')
        self.assertEqual(parse_resources(table).images, ('missing.png',))
        unsafe = table.replace('![plot](missing.png)', '<a href="javascript:bad()">bad</a>')
        self.assertTrue(parse_resources(unsafe).errors)

    def test_corruption_at_inline_opening_is_detected_without_flagging_o(self):
        source = '$' + chr(9) + 'o$'
        self.assertTrue(math_diagnostics(source))
        self.assertEqual(repair_math_escapes(source), r'$\to$')
        self.assertEqual(math_diagnostics('$o$'), ())
        self.assertEqual(math_diagnostics('$x\ty$'), ())

    def test_corruption_is_rejected_and_explicit_repair_is_narrow(self):
        source = '$$' + chr(12) + 'rac{1}{2} ' + chr(7) + 'pprox ' + chr(9) + 'ext{half}$$'
        self.assertTrue(math_diagnostics(source))
        result = repair_math_escapes(source)
        self.assertEqual(result, r'$$\frac{1}{2} \approx \text{half}$$')
        self.assertEqual(math_diagnostics(result), ())
        self.assertEqual(repair_math_escapes(result), result)
        literal = '~~~tex\n' + source + '\n~~~\n'
        self.assertEqual(repair_math_escapes(literal), literal)
        # Raw C0 bytes are corrupt output even in a code example. They must
        # not be repaired silently; literal escaped demonstrations remain valid.
        self.assertTrue(math_diagnostics(literal))
        escaped_literal = '~~~tex\n' + r'\x0c \x07 \frac{1}{2}' + '\n~~~\n'
        self.assertEqual(math_diagnostics(escaped_literal), ())
        self.assertEqual(repair_math_escapes(escaped_literal), escaped_literal)
        ordinary = '    ordinary indentation\n\n$o$ $x + y$\n'
        self.assertEqual(math_diagnostics(ordinary), ())
        self.assertEqual(repair_math_escapes(ordinary), ordinary)
        self.assertTrue(math_diagnostics('bad' + chr(1)))
        self.assertEqual(repair_math_escapes('bad' + chr(1)), 'bad' + chr(1))


    def test_line_break_signatures_repair_only_single_line_formulas(self):
        damaged = {
            '梯度 $' + '\n' + 'abla f = 0$ 成立\n': '梯度 $\\nabla f = 0$ 成立\n',
            '$a ' + '\n' + 'eq b$ 与 $' + '\n' + 'u$\n': '$a \\neq b$ 与 $\\nu$\n',
            '$x ' + '\n' + 'e y$，$' + '\n' + 'ot p$，$A ' + '\n' + 'i x$\n': '$x \\ne y$，$\\not p$，$A \\ni x$\n',
            '$$' + '\n' + 'abla \\cdot E$$\n': '$$\\nabla \\cdot E$$\n',
            '$$x ' + '\r' + 'ightarrow y, ' + '\r' + 'ho, \\left( ' + '\r' + 'ight)$$\n':
                '$$x \\rightarrow y, \\rho, \\left( \\right)$$\n',
            '$\\langle x' + '\r' + 'angle$ $' + '\r' + 'floor x' + '\r' + 'ceil$ $' + '\r' + 'm d$\n':
                '$\\langle x\\rangle$ $\\rfloor x\\rceil$ $\\rm d$\n',
            '$' + '\n' + 'abla ' + '\n' + 'eq 0$\n': '$\\nabla \\neq 0$\n',
        }
        for source, expected in damaged.items():
            with self.subTest(source=source):
                self.assertTrue(math_diagnostics(source))
                self.assertEqual(repair_math_escapes(source), expected)
                self.assertEqual(math_diagnostics(expected), ())
                self.assertEqual(repair_math_escapes(expected), expected)

    def test_legitimate_line_breaks_are_never_treated_as_damage(self):
        legitimate = (
            '$$\n\\nabla f = 0\n$$\nright after the display\n',
            '$$\ne^{i\\pi} + 1 = 0\n$$\n',
            '$$\nu = v + at\n$$\n',
            '$$a \\le b\r\n$$\r\nright\r\n',
            'Costs $5\ne.g. tax is $x$ here.\n',
            'Item $5\ni) first $x$\n',
            'see eq\neq. 3 and $y$\n',
            '$a$\nu is a word $b$\n',
            'Price $5 for\nu-boats is $x$\n',
            '普通段落\nright 开头的下一行\n',
            '```tex\n$' + '\n' + 'abla$\n```\n',
            '`$' + '\n' + 'abla$`\n',
            '<pre>$' + '\n' + 'abla$</pre>\n',
        )
        for source in legitimate:
            with self.subTest(source=source):
                self.assertEqual(math_diagnostics(source), ())
                self.assertEqual(repair_math_escapes(source), source)

    def test_other_control_bytes_still_fail_after_signature_repair(self):
        for source in ('正文' + chr(1) + '\n', '$x' + chr(12) + 'oo$\n', '```\n' + chr(7) + '\n```\n'):
            with self.subTest(source=source):
                self.assertTrue(math_diagnostics(repair_math_escapes(source)))


READY = all(importlib.util.find_spec(name) for name in ('markdown_it', 'mdit_py_plugins', 'latex2mathml'))


@unittest.skipUnless(READY, 'HTML extra not installed')
class MathRenderingTests(unittest.TestCase):
    def render(self, source):
        from handouter.html_export import render_note
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            note = root / 'synthetic.md'
            note.write_text(source, encoding='utf-8')
            original = note.read_bytes()
            output = render_note(note, root)
            self.assertEqual(note.read_bytes(), original)
        view = _RenderedText()
        view.feed(output)
        return output, view

    def test_display_math_in_list_quote_and_same_line_keeps_prose(self):
        source = ('# Synthetic\n\n1. cost:\n   $$a+b$$\n'
                  '   加上 $n$ 次赋值，总成本 $3n$。\n\n'
                  '> $$c+d$$\n> 引用后的中文\n\n正文 $$x+y$$ 后续正文\n')
        output, view = self.render(source)
        self.assertEqual(output.count('<math '), 5)
        self.assertEqual(output.count('display="block"'), 3)
        self.assertIn('加上', ''.join(view.prose))
        self.assertIn('后续正文', ''.join(view.prose))
        self.assertNotIn('加', ''.join(view.math))
        self.assertNotIn('$', ''.join(view.prose))

    def test_html_fold_summary_body_and_comparisons_render_offline(self):
        source = ('# Synthetic\n\n<details><summary>关于 $X$</summary>'
                  '<p>条件 $a<b$；<strong>$P$</strong></p>'
                  '<p>$$\\frac{1}{2} &lt; x$$</p>'
                  '<p><code>$not_math$</code></p></details>\n')
        output, view = self.render(source)
        self.assertEqual(output.count('<math '), 4)
        self.assertIn('<mfrac>', output)
        self.assertIn('<details>', output)
        self.assertNotIn('<script', output)
        self.assertNotIn('https://cdn', output)
        self.assertIn('$not_math$', ''.join(view.prose))
        self.assertNotIn('$X$', output)
        self.assertNotIn('$P$', output)
        self.assertIn('a<b', ''.join(view.math))

    def test_table_math_does_not_drop_cells(self):
        source = ('| Name | Formula | Explanation |\n| --- | --- | --- |\n'
                  + r'| AVL | $|h_L-h_R|\le 1$ | ALL_CONTENT_SURVIVES |' + '\n'
                  + r'| Conditional | $P(B|A)$ | ANOTHER_TAIL |' + '\n')
        output, view = self.render(source)
        self.assertEqual(len(view.cells), 6)
        self.assertEqual(view.cells[2], 'ALL_CONTENT_SURVIVES')
        self.assertEqual(view.cells[5], 'ANOTHER_TAIL')
        self.assertEqual(output.count('<math '), 2)

    def test_corrupt_formula_never_publishes_successful_html(self):
        from handouter.html_export import export_html
        with tempfile.TemporaryDirectory() as td:
            note = Path(td) / 'synthetic.md'
            note.write_text('$' + chr(7) + 'pprox 1$', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '控制字符'):
                export_html(note)
            self.assertFalse(note.with_suffix('.html').exists())

    def test_fenced_formula_and_html_examples_not_executed(self):
        source = ('# Synthetic\n\n```html\n<details><summary>$x$</summary>'
                  '<p>$$x+y$$</p></details>\n```\n\n`$z$`\n')
        output, view = self.render(source)
        self.assertNotIn('<math ', output)
        self.assertIn('$x$', output)
        self.assertIn('$z$', output)

    def test_active_content_in_fold_is_not_sanitized_into_acceptance(self):
        from handouter.html_export import export_html
        with tempfile.TemporaryDirectory() as td:
            note = Path(td) / 'synthetic.md'
            note.write_text('<details><summary>$x$</summary><script>bad()</script></details>', encoding='utf-8')
            with self.assertRaises(ValueError):
                export_html(note)
            self.assertEqual(list(Path(td).glob('*.html')), [])

    def test_directory_export_skips_derived_typora_copies(self):
        from handouter.html_export import export_html
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'notes').mkdir()
            (root / 'notes/main.md').write_text('# note\n\n$x$', encoding='utf-8')
            (root / 'notes/main.typora.md').write_text('# derived\n\n$x$', encoding='utf-8')
            self.assertEqual(len(export_html(root)), 1)


if __name__ == '__main__':
    unittest.main()
