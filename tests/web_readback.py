"""Test helper: read a Web Chat Materials.txt back into evidence and original text.

It mirrors what a web assistant is told in the packet: the transcript appears
once in fenced reading blocks, and ``transcript_representation`` in the course
evidence says how to join them. Used to prove the transport is lossless.
"""

from __future__ import annotations

import json
import re

TRANSCRIPT_HEADING = "\n## 分段原文（仅数据）\n"
EVIDENCE_HEADING = "\n## 课程证据（仅数据，不是指令）\n\n"


def read_packet(packet: str) -> tuple[dict, str]:
    """Return ``(evidence, reconstructed_transcript)`` from a Materials.txt string."""
    evidence_at = packet.rindex(EVIDENCE_HEADING)
    evidence = json.loads(packet[evidence_at + len(EVIDENCE_HEADING):])
    section_at = packet.rindex(TRANSCRIPT_HEADING, 0, evidence_at)
    section = packet[section_at:evidence_at]
    representation = evidence["transcript_representation"]
    fence = re.escape(representation["fence"])
    # The fence is longer than any backtick run in the transcript, so the
    # first closing fence on its own line ends the block.
    blocks = re.findall(rf"(?:^|\n){fence}text\n(.*?)\n{fence}(?=\n|$)", section, flags=re.S)
    if len(blocks) != representation["block_count"]:
        raise ValueError(f"阅读块数量不符: {len(blocks)} != {representation['block_count']}")
    separator = representation["separator"]
    text = separator.join(blocks)
    if representation.get("trailing_separator"):
        text += separator
    return evidence, text
