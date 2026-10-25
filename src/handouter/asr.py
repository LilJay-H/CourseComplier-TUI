"""SenseVoice backend that persists raw evidence, normalized segments and text."""

from __future__ import annotations

import contextlib
import importlib.metadata
import json
import os
import shutil
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .sensevoice_normalize import ASRError, jsonable_result as _jsonable, normalize_funasr_result


@dataclass(frozen=True)
class ASRResult:
    output_dir: Path
    raw_path: Path
    segments_path: Path
    transcript_path: Path
    segment_count: int
    timestamp_kind: str
    device: str
    elapsed_seconds: float


def choose_device(requested: str = "auto") -> str:
    if requested not in {"auto", "cpu", "mps", "cuda"}:
        raise ValueError("device 必须是 auto/cpu/mps/cuda")
    if requested != "auto":
        return requested
    try:
        import torch
    except ImportError:
        return "cpu"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def transcribe_sensevoice(
    audio: str | Path,
    output_dir: str | Path,
    *,
    device: str = "auto",
    language: str = "auto",
    model: str = "iic/SenseVoiceSmall",
    model_path: str | None = None,
    vad_model_path: str | None = None,
    batch_size_s: int = 60,
    max_segment_ms: int = 30_000,
) -> ASRResult:
    """Run one local SenseVoice transcription into a new output directory."""
    audio = Path(audio)
    if not audio.is_file() or audio.is_symlink():
        raise FileNotFoundError(f"音频不存在或不是普通文件: {audio}")
    destination = Path(output_dir)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"ASR 输出目录已存在，拒绝覆盖: {destination}")
    if batch_size_s < 1 or max_segment_ms < 1000:
        raise ValueError("batch_size_s/max_segment_ms 参数无效")
    resolved_device = choose_device(device)

    try:
        from funasr import AutoModel
        from funasr.utils.postprocess_utils import rich_transcription_postprocess
    except ImportError as exc:
        raise ASRError("SenseVoice 需要可选依赖 funasr/torch；请安装 handouter[asr]") from exc

    staging = destination.parent / f".{destination.name}.tmp-{uuid.uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=False)
    started = time.time()
    try:
        backend_model = model_path or model
        if model_path is not None:
            if not vad_model_path or not Path(model_path).is_dir() or not Path(vad_model_path).is_dir():
                raise ASRError("本地 SenseVoice 与 VAD 模型目录都必须存在")
            vad_model = vad_model_path
        else:
            vad_model = "fsmn-vad"
        # FunASR currently prints version/progress messages to stdout in addition
        # to normal logging. Route those messages to stderr so CLI stdout remains
        # a machine-readable JSON channel.
        with contextlib.redirect_stdout(sys.stderr):
            runner = AutoModel(
                model=backend_model,
                vad_model=vad_model,
                vad_kwargs={"max_single_segment_time": max_segment_ms},
                device=resolved_device,
                disable_update=True,
            )
            raw = runner.generate(
                input=str(audio),
                cache={},
                language=language,
                use_itn=True,
                batch_size_s=batch_size_s,
                merge_vad=True,
                sentence_timestamp=True,
                return_raw_text=True,
                disable_pbar=True,
            )
        segments, transcript, timestamp_kind = normalize_funasr_result(
            raw, postprocess=rich_transcription_postprocess
        )
        elapsed = time.time() - started
        try:
            funasr_version = importlib.metadata.version("funasr")
        except importlib.metadata.PackageNotFoundError:
            funasr_version = None
        raw_payload = {
            "schema_version": "1",
            "backend": "funasr_sensevoice",
            "model": backend_model,
            "vad_model": vad_model,
            "funasr_version": funasr_version,
            "device": resolved_device,
            "language": language,
            "parameters": {
                "batch_size_s": batch_size_s,
                "max_single_segment_time_ms": max_segment_ms,
                "merge_vad": True,
                "sentence_timestamp": True,
                "use_itn": True,
            },
            "elapsed_seconds": round(elapsed, 6),
            "result": _jsonable(raw),
        }
        raw_path = staging / "raw.json"
        segments_path = staging / "segments.json"
        transcript_path = staging / "transcript.txt"
        raw_path.write_text(json.dumps(raw_payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        segments_path.write_text(json.dumps(segments, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        transcript_path.write_text(transcript + ("\n" if transcript else ""), encoding="utf-8")
        if not transcript.strip():
            raise ASRError("ASR 没有产生可用文本")
        for path in (raw_path, segments_path, transcript_path):
            # Windows _commit (used by fsync) requires a writable descriptor.
            with path.open("r+b") as written:
                os.fsync(written.fileno())
        staging.rename(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return ASRResult(
        output_dir=destination,
        raw_path=destination / "raw.json",
        segments_path=destination / "segments.json",
        transcript_path=destination / "transcript.txt",
        segment_count=len(segments),
        timestamp_kind=timestamp_kind,
        device=resolved_device,
        elapsed_seconds=elapsed,
    )
