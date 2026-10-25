"""Public SenseVoice/FunASR result normalization shared by local and CLI ASR.

This module deliberately has no authoring or model-runtime dependencies.
"""

from __future__ import annotations

import re
from typing import Any, Callable


class ASRError(RuntimeError):
    pass


def jsonable_result(value: Any) -> Any:
    """Keep raw evidence, excluding speaker embeddings and opaque objects."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): jsonable_result(item) for key, item in value.items() if key != "spk_embedding"}
    if isinstance(value, (list, tuple)):
        return [jsonable_result(item) for item in value]
    if hasattr(value, "tolist"):
        try:
            return jsonable_result(value.tolist())
        except Exception:
            pass
    return str(value)


def _time_ms(value: Any) -> int | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = int(round(value))
    return value if value >= 0 else None


def normalize_funasr_result(
    result: list[dict[str, Any]], *, postprocess: Callable[[str], str] | None = None,
) -> tuple[list[dict[str, Any]], str, str]:
    """Normalize sentence/VAD evidence without manufacturing timestamps."""
    if not isinstance(result, list) or not result or not isinstance(result[0], dict):
        raise ASRError("ASR 未返回有效结果")
    root = result[0]
    clean = postprocess or (lambda text: text)
    segments: list[dict[str, Any]] = []
    sentence_info = root.get("sentence_info")
    if isinstance(sentence_info, list):
        for item in sentence_info:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or item.get("sentence") or "").strip()
            if not text:
                continue
            cleaned = clean(text).strip()
            if not re.search(r"[A-Za-z0-9\u3400-\u9fff]", cleaned):
                continue
            start, end = _time_ms(item.get("start")), _time_ms(item.get("end"))
            if start is not None and end is not None and end < start:
                start = end = None
            segments.append({
                "id": f"seg-{len(segments) + 1:04d}",
                "start_ms": start,
                "end_ms": end,
                "text": cleaned,
                "timestamp_kind": "sentence_or_vad" if start is not None and end is not None else "unavailable",
                "speaker": item.get("spk"),
            })
    full_text = clean(str(root.get("text") or "").strip()).strip()
    if not segments:
        if not full_text:
            raise ASRError("ASR 结果为空")
        return ([{"id": "seg-0001", "start_ms": None, "end_ms": None,
                  "text": full_text, "timestamp_kind": "unavailable", "speaker": None}],
                full_text, "unavailable")
    transcript = "\n".join(segment["text"] for segment in segments if segment["text"]).strip()
    if not transcript:
        transcript = full_text
    timed = all(segment["start_ms"] is not None and segment["end_ms"] is not None for segment in segments)
    return segments, transcript, "sentence_or_vad" if timed else "partial"
