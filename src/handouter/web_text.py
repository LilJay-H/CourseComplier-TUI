"""Readable web transcript blocks plus a lossless canonical-text index."""
from __future__ import annotations

import json
import re
from .source_refs import TranscriptIndex


def _time_label(start, end) -> str:
    # Keep invalid/absent raw values in metadata; never manufacture a clock.
    if (type(start) is not int or type(end) is not int or start < 0 or end < start):
        return '时间未知（缺失或不可用）'
    def clock(value):
        seconds, millis = divmod(value, 1000)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f'{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}'
    return clock(start) + '–' + clock(end)


def whole_text_view(transcript: str) -> tuple[str, dict]:
    """Show a TXT or submitted note verbatim without inventing segment IDs.

    Also used when TXT and segment text differ: the canonical TXT is displayed
    here, while the differing segment version remains intact in its own index.
    """
    longest = max((len(run) for run in re.findall(r'`+', transcript)), default=0)
    fence = '`' * max(3, longest + 1)
    index = TranscriptIndex(transcript)
    return ('### 完整原文｜未按分段索引替换\n\n'
            f'{fence}text\n{transcript}\n{fence}'), {
        'transcript_representation': {
            'version': 2, 'kind': 'whole_text', 'section': '分段原文（仅数据）',
            'block_count': 1, 'separator': '', 'trailing_separator': False, 'fence': fence,
            'source_line_range': [1, len(index.lines)] if index.lines else None,
            'source_line_range_semantics': 'containing_lines',
            'line_numbering': '原 TXT 的 splitlines()，从1开始；不是 Markdown 显示行号',
        },
        'source_text_status': '上方完整原文保持输入字符与换行，不捏造段号或时间。若分段索引另有文字，两种版本均保留，不自动选择其一。',
    }


def segment_view(transcript: str, segments: list[dict]) -> tuple[str, dict]:
    """Mutate only the transport's segment copies, never the input files.

    If TXT and segments disagree, return no deduplication: the caller retains
    both original versions. An empty list cannot account for a nonempty TXT.
    """
    if not segments or any(not isinstance(s.get('text'), str) for s in segments):
        return '', {}
    texts = [s['text'] for s in segments]
    separator = next((sep for sep in ('\n', '\r\n')
                      if transcript in (sep.join(texts), sep.join(texts) + sep)), None)
    if separator is None:
        return '', {}
    longest = max((len(run) for run in re.findall(r'`+', transcript)), default=0)
    fence = '`' * max(3, longest + 1)
    blocks = []
    index = TranscriptIndex(transcript)
    cursor = 0
    for number, segment in enumerate(segments, 1):
        text = segment.pop('text')
        segment['text_span'] = [cursor, cursor + len(text)]
        segment['text_span_semantics'] = 'exact'
        line_range = index.containing_lines(*segment['text_span'])
        segment['source_line_range'] = list(line_range) if line_range else None
        segment['source_line_range_semantics'] = 'containing_lines'
        segment['readable_block'] = number
        cursor += len(text) + len(separator)
        identity = (json.dumps(segment['id'], ensure_ascii=False)
                    if segment.get('id') is not None else '源ID缺失')
        label = _time_label(segment.get('start_ms'), segment.get('end_ms'))
        locator = (f'transcript:L{line_range[0]}-L{line_range[1]}'
                   if line_range else '空文本（无来源行）')
        blocks.append(f'### 阅读块 {number}｜{identity}｜{label}｜{locator}\n\n{fence}text\n{text}\n{fence}')
    return '\n\n'.join(blocks), {
        'transcript_representation': {
            'version': 2, 'section': '分段原文（仅数据）', 'block_count': len(segments),
            'separator': separator, 'trailing_separator': transcript != separator.join(texts),
            'fence': fence,
        },
        'segment_text_mapping': '正文只在上方阅读块出现一次；无需计数字符。text_span 是原TXT零基Unicode精确区间[start,end)，不是字节/UTF-16；source_line_range 及 transcript:Lx-Ly 是包含该区间的来源行，不是精确子串；空区间无来源行。时间和ID保持原值。',
    }
