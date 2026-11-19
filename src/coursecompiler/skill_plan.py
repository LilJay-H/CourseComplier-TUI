"""Progressive-disclosure Skill planning and materialization.

The built-in lecture-note Skill is modular. A handoff only exposes modules that
are relevant to the selected deliverables/options. Custom single-file Skills
remain supported and simply produce an empty module list.
"""

from __future__ import annotations

import shutil
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable


TEACHING_MODULE = "references/common/teaching.md"
TEACHING_EXAMPLE = "references/examples/teaching-synthetic.md"
COMMON_MODULES = (
    "references/common/evidence.md",
    "references/common/editing.md",
    "references/common/writing.md",
    TEACHING_MODULE,
    "references/common/presentation.md",
    "references/common/delivery.md",
    "references/common/completion.md",
)
MODE_MODULES = {
    "verbatim": "references/modes/verbatim.md",
    "full": "references/modes/full.md",
    "deep": "references/modes/deep.md",
    "summary": "references/modes/summary.md",
    "complete": "references/modes/complete.md",
}
FORMAT_MODULES = {
    "clean": "references/formats/clean.md",
    "traceable": "references/formats/traceable.md",
}
SLIDE_MODULES = {
    "ignore": "references/slides/ignore.md",
    "source-only": "references/slides/source-only.md",
    "embed": "references/slides/embed.md",
}
SLIDE_READING_MODULE = "references/slides/reading.md"
SLIDE_LAYOUT_MODULE = "references/slides/layout.md"
MULTI_OUTPUT_MODULE = "references/execution/multi-output.md"
LONG_COURSE_MODULE = "references/execution/long-course.md"
FORMAL_REASONING_MODULE = "references/execution/formal-reasoning.md"
RULES_VERSION = "2026-10-14.2"
AUTHORING_MODULE = "references/execution/authoring.md"
EXAMPLE_SOURCES = "references/examples/sources.md"


def formal_reasoning_signals(text: str) -> bool:
    """Conservative text-only routing hint, never semantic classification.

    Scores, dates and percentages alone do not enable the module. Scan the full
    supplied text, including the end of long lectures; titles are not inputs.
    """
    return bool(re.search(
        r"\\(?:frac|sum|int|begin)\b|\$\$|```(?:python|cpp|c\+\+|java|rust)\b"
        r"|\b(?:AVL|Splay|BST|Big[- ]O)\b|O\s*\(\s*(?:n|N|log|1)"
        r"|递推(?:式|关系)|数学归纳|摊还分析|时间复杂度|空间复杂度|二叉(?:搜索|排序|平衡)树"
        r"|\b(?:Boolean|XOR|bit[- ]?width|overflow|underflow|two's complement)\b"
        r"|布尔|逻辑(?:式|门|电路)|位宽|溢出|补码|有限(?:步|状态)|无限(?:趋近|极限)"
        r"|\b(?:AND|OR|NOT)\s+(?:gate|logic)\b|电路(?:延迟|时序)|门延迟|扇入"
        r"|\b(?:latency|throughput|speedup|clock\s+(?:rate|frequency)|cycles?\s+per\s+instruction)\b"
        r"|吞吐量|加速比|时钟(?:周期|频率)|每条指令周期"
        r"|\b\d+(?:\.\d+)?[- ]*(?:bits?|[kmg]?Hz|[munp]?s|MIPS|FLOPS)\b"
        r"|\d+\s*位(?:有符号|无符号|二进制|寄存器|整数)"
        r"|\b[A-Za-z]\w*\s*=\s*(?:[¬~!]|NOT\b)\s*\(?[A-Za-z]\w*"
        r"|\b[A-Za-z]\w*\s*=\s*[(!¬~]*(?:[A-Za-z]\w*|\d+(?:\.\d+)?)"
        r"\s*(?:[+*/^&|∧∨⊕×÷−-]|\b(?:AND|OR|XOR)\b)"
        r"|(?:\d+(?:\.\d+)?|\b[A-Za-z]\w*)\s*[+*/^∧∨⊕×÷−-]\s*"
        r"(?:\d+(?:\.\d+)?|[A-Za-z]\w*)\s*=",
        text, re.I,
    ))


@dataclass(frozen=True)
class SkillPlan:
    source_entry: Path
    entry_path: str
    modules: tuple[str, ...]


def _safe_relative(relative: str) -> Path:
    posix = PurePosixPath(relative)
    if posix.is_absolute() or any(part in {"", ".", ".."} for part in posix.parts):
        raise ValueError(f"invalid Skill module path: {relative!r}")
    return Path(*posix.parts)


def plan_modules(
    skill_entry: str | Path,
    *,
    modes: Iterable[str],
    format_profile: str,
    use_slides_as_source: bool,
    embed_slides: bool,
    segment_count: int = 0,
    transcript_bytes: int = 0,
    material_text: str = "",
) -> tuple[str, ...]:
    """Return the built-in Skill modules needed for this handoff.

    A custom standalone Skill may not have the built-in reference tree; in that
    case we intentionally return no modules rather than inventing requirements.
    """
    entry = Path(skill_entry).resolve()
    root = entry.parent
    sentinel = root / COMMON_MODULES[0]
    if not sentinel.is_file():
        return ()

    requested = [str(mode).strip().lower() for mode in modes]
    modules: list[str] = list(COMMON_MODULES)
    # Complete uses these modes as scoped layers, not as extra deliverables.
    required_modes = set(requested)
    if "complete" in required_modes:
        required_modes.update(("deep", "full", "verbatim"))
        modules.append(MODE_MODULES["complete"])
    for mode in ("deep", "summary", "full", "verbatim"):
        if mode in required_modes:
            modules.append(MODE_MODULES[mode])
    modules.append(FORMAT_MODULES[format_profile])

    if not use_slides_as_source:
        modules.append(SLIDE_MODULES["ignore"])
    else:
        modules.append(SLIDE_READING_MODULE)
        if embed_slides:
            modules.extend((SLIDE_MODULES["embed"], SLIDE_LAYOUT_MODULE))
        else:
            modules.append(SLIDE_MODULES["source-only"])

    modules.append(AUTHORING_MODULE)
    # Examples are selected deliverable models, not layer prerequisites. A
    # Complete-only task must not load every standalone example again.
    modules.append(EXAMPLE_SOURCES)
    if any(mode in requested for mode in ("deep", "complete")):
        modules.append(TEACHING_EXAMPLE)
    for mode in ("deep", "summary", "full", "verbatim", "complete"):
        if mode in requested:
            modules.append(f"references/examples/{mode}.md")
    if use_slides_as_source and embed_slides:
        modules.append("references/examples/visual.md")
    if len(set(requested)) > 1:
        modules.append(MULTI_OUTPUT_MODULE)
    # Multiple outputs and Complete layers can exhaust output budget even for
    # fewer than 100 input segments. These are hints, not token estimates.
    if (int(segment_count) >= 100 or int(transcript_bytes) >= 20_000
            or "complete" in requested or len(set(requested)) > 1):
        modules.append(LONG_COURSE_MODULE)
    if formal_reasoning_signals(material_text) and (root / FORMAL_REASONING_MODULE).is_file():
        modules.append(FORMAL_REASONING_MODULE)

    # Preserve deterministic order and reject plans whose source files vanished.
    result: list[str] = []
    seen: set[str] = set()
    for relative in modules:
        if relative in seen:
            continue
        source = root / _safe_relative(relative)
        if not source.is_file():
            raise FileNotFoundError(f"Skill module missing: {source}")
        seen.add(relative)
        result.append(relative)
    return tuple(result)


def materialize_skill_plan(
    workspace: str | Path,
    skill_entry: str | Path,
    modules: Iterable[str],
) -> SkillPlan:
    """Copy the Skill entry and selected modules into the handoff directory."""
    workspace = Path(workspace)
    source_entry = Path(skill_entry).resolve()
    if not source_entry.is_file():
        raise FileNotFoundError(f"Skill entry missing: {source_entry}")
    source_root = source_entry.parent
    target_root = workspace / "handoff" / "skill"
    if target_root.exists():
        raise FileExistsError(f"handoff Skill already exists: {target_root}")
    target_root.mkdir(parents=True)
    copied: list[str] = []
    try:
        shutil.copyfile(source_entry, target_root / "SKILL.md")
        for relative in modules:
            safe = _safe_relative(relative)
            source = source_root / safe
            if not source.is_file():
                raise FileNotFoundError(f"Skill module missing: {source}")
            target = target_root / safe
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            copied.append((Path("handoff") / "skill" / safe).as_posix())
    except BaseException:
        shutil.rmtree(target_root, ignore_errors=True)
        raise

    return SkillPlan(
        source_entry=source_entry,
        entry_path="handoff/skill/SKILL.md",
        modules=tuple(copied),
    )
