"""Shared structural note validation without local workspace orchestration."""
from __future__ import annotations
import json
import hashlib
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from .safe_io_core import relative_path, safe_path, read_bytes, publish_bytes
from .credentials import scan_credentials
from .resources import has_task_placeholders, has_pending_images, check_resources

@dataclass(frozen=True)
class NoteValidationReport:
    ok: bool
    output_path: str | None
    errors: tuple[str, ...]
    image_links: int
    sha256: str | None
    warnings: tuple[str, ...] = ()
    # Fixed validator categories for safe operational diagnostics. Human-facing
    # errors may contain paths or excerpts and must never be persisted remotely.
    diagnostic_codes: tuple[str, ...] = ()


class _VisibleNoteText(HTMLParser):
    """Readability view only; resource and safety checks still use the full note."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.details: list[bool] = []
        self.summaries: list[int] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "details":
            self.details.append(not any(name == "open" for name, _value in attrs))
            self.parts.append("\n")
        elif tag == "summary":
            self.summaries.append(len(self.details))
        elif tag in {"p", "br"} and not any(self.details):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "details" and self.details:
            depth = len(self.details)
            self.details.pop()
            self.summaries = [level for level in self.summaries if level < depth]
            self.parts.append("\n")
        elif tag == "summary" and self.summaries:
            self.summaries.pop()
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        summary_visible = (
            bool(self.summaries)
            and self.summaries[-1] == len(self.details)
            and not any(self.details[:-1])
        )
        if not any(self.details) or summary_visible:
            self.parts.append(data)


def _visible_note_text(text: str) -> str:
    parser = _VisibleNoteText()
    try:
        parser.feed(text)
        parser.close()
    except (AssertionError, ValueError):
        # Readability parsing must not abort validation of malformed Agent text.
        return text
    # An unclosed fold must not hide the remainder from readability warnings.
    return text if parser.details else "".join(parser.parts)


class _PendingCheckLayout(HTMLParser):
    """Check pending-list container structure, not semantic truth or risk level."""

    def __init__(self):
        super().__init__()
        self.closed: list[bool] = []
        self.capture: str | None = None
        self.label: list[str] = []
        self.issues: set[str] = set()
        self.pending_folds = 0

    def handle_starttag(self, tag, attrs):
        if tag == "details":
            self.closed.append(not any(key == "open" for key, _ in attrs))
        if tag in {"summary", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.capture, self.label = tag, []

    def handle_data(self, data):
        if self.capture:
            self.label.append(data)

    def handle_endtag(self, tag):
        if tag == self.capture:
            if "待核对" in "".join(self.label):
                if tag == "summary":
                    self.pending_folds += 1
                if not any(self.closed):
                    self.issues.add("待核对清单未整体默认折叠；请把条目及来源放入一个关闭的 details，关键风险短提醒仍保留可见")
            self.capture = None
        if tag == "details" and self.closed:
            self.closed.pop()


def _pending_check_warnings(text: str) -> list[str]:
    from .resources import markdown_parser
    parser = _PendingCheckLayout()
    parser.feed(markdown_parser().render(text))
    parser.close()
    if parser.pending_folds > 1:
        parser.issues.add("待核对被拆成多个容器；应聚合为文末一个默认关闭的清单")
    if parser.pending_folds and parser.closed:
        parser.issues.add("待核对折叠容器未闭合，请检查实际阅读呈现")
    return sorted(parser.issues)



_RAW_TRACK = re.compile(r"原始\s*ASR|原始\s*转写|ASR\s*原文", re.I)
_CLEAN_TRACK = re.compile(r"精修|清理|整理|润色")
_CLEAN_HEADING = re.compile(r"精修|清理|整理稿|润色")
_TABLE_DELIMITER = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$")


def _has_dual_track_layout(text: str) -> bool:
    """Structural raw-ASR/cleaned dual track only, never a prose mention.

    A table header row with a raw-ASR column and a cleaned column, or headings
    naming both tracks. Sentences such as "据原始 ASR 整理" stay valid.
    """
    lines = text.splitlines()
    for header, delimiter in zip(lines, lines[1:]):
        if "|" not in header or not _TABLE_DELIMITER.match(delimiter):
            continue
        cells = [cell.strip() for cell in header.strip().strip("|").split("|")]
        raw = [index for index, cell in enumerate(cells) if _RAW_TRACK.search(cell)]
        if raw and any(_CLEAN_TRACK.search(cell) for index, cell in enumerate(cells) if index not in raw):
            return True
    headings = [match.group(1) for match in re.finditer(r"^ {0,3}#{1,6}\s+(.+)$", text, flags=re.M)]
    raw_headings = {index for index, heading in enumerate(headings) if _RAW_TRACK.search(heading)}
    return bool(raw_headings) and any(
        _CLEAN_HEADING.search(heading) for index, heading in enumerate(headings) if index not in raw_headings)


def _safe_relative(value):
    return relative_path(value)


def _dump(path: Path, data):
    publish_bytes(path.parent, path.name,
                  (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8"), replace=True)

def validate_note_output(
    workspace: str | Path,
    *,
    output: str | Path | None = None,
    update_state: bool = False,
    revision_guard=None,
    complete_quote_check=None,
) -> NoteValidationReport:
    """Validate one Agent-produced Markdown structurally, without claiming semantic correctness."""
    workspace = Path(workspace)
    errors: list[str] = []
    warnings: list[str] = []
    diagnostic_codes: list[str] = []
    image_links = 0
    output_hash: str | None = None
    output_rel: str | None = None

    try:
        state = json.loads(read_bytes(workspace, 'state.json'))
        sources = json.loads(read_bytes(workspace, 'handoff/sources.json'))
        if not isinstance(state, dict) or not isinstance(sources, dict):
            raise ValueError('state/sources 必须是 JSON 对象')
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return NoteValidationReport(False, None, (f"无法读取 handoff/state: {exc}",), 0, None, (),
                                    ("handoff_state_invalid",))

    candidate = output if output is not None else sources.get("expected_output")
    try:
        if candidate is None:
            raise ValueError("没有 expected_output")
        candidate_path = Path(candidate)
        if candidate_path.is_absolute():
            try:
                root = workspace.resolve(strict=True)
                target = candidate_path.absolute()
                target.relative_to(root)
            except (OSError, ValueError) as exc:
                raise ValueError("输出必须位于讲次工作区内") from exc
            output_rel = target.relative_to(root).as_posix()
        else:
            output_rel = _safe_relative(str(candidate))
            target = workspace / output_rel
        target = safe_path(workspace, output_rel, role='note', missing=True)
        if not output_rel.startswith("notes/"):
            errors.append("Agent 输出必须位于 notes/ 下")
            diagnostic_codes.append("output_path_invalid")
        if not target.is_file() or target.is_symlink():
            errors.append(f"讲义输出不存在或不是普通文件: {output_rel}")
            diagnostic_codes.append("output_missing")
            text = ""
        else:
            data = read_bytes(workspace, output_rel, role='note')
            text = data.decode('utf-8')
            if not text.strip():
                errors.append("讲义输出为空")
                diagnostic_codes.append("empty_markdown")
            if has_task_placeholders(text):
                errors.append("讲义仍含模板占位符")
                diagnostic_codes.append("template_placeholder")
            if has_pending_images(text):
                errors.append('讲义仍有 PDF 插图位置占位；需补齐真实图片后再验收')
                diagnostic_codes.append("pending_image")
            credentials = scan_credentials(text)
            errors.extend(credentials.errors)
            if credentials.errors:
                diagnostic_codes.append("credential_detected")
            warnings.extend(credentials.warnings)
            output_hash = hashlib.sha256(data).hexdigest()
            revision = sources.get("revision")
            if isinstance(revision, dict) and revision.get("action") == "illustrate":
                if revision_guard is None or not revision_guard(str(revision.get("text") or ""), text):
                    errors.append("仅补图输出删除、改写或重排了原讲义正文")
                    diagnostic_codes.append("revision_integrity")
    except (OSError, UnicodeError, ValueError) as exc:
        errors.append(str(exc))
        diagnostic_codes.append("output_read_invalid")
        text = ""

    options = sources.get("options", {})
    format_profile = str(options.get("format_profile") or "clean")
    mode = str(sources.get("mode") or "")
    raw_outputs = sources.get("expected_outputs")
    if output_rel and isinstance(raw_outputs, dict):
        for mode_name, expected_path in raw_outputs.items():
            if expected_path == output_rel:
                mode = str(mode_name)
                break

    analysis_text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    visible_text = _visible_note_text(analysis_text)
    prose_text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", visible_text)
    if sources.get("skill", {}).get("rules_version") not in {None, "custom"}:
        warnings.extend(_pending_check_warnings(text))
    if format_profile == "clean":
        if re.search(r"\b(?:seg|occ)-\d{3,}\b", visible_text):
            warnings.append("clean 格式正文仍显示 seg/occ 来源 ID；应放入默认折叠的本章时间与来源 details 中")
        if re.search(r"(?:transcript|slides|handoff)/(?:[^\s`]+)", prose_text):
            warnings.append("clean 格式正文仍显示内部工作区路径")
        if re.search(r"^##?\s*(?:讲义编制说明|讲义元信息|材料说明|任务配置|处理流程)", visible_text, flags=re.M):
            warnings.append("clean 格式包含面向工程处理的元信息章节")
    if mode in {"verbatim", "full"} and _has_dual_track_layout(analysis_text):
        errors.append(f"{mode} 模式不应生成原始 ASR 与整理稿双轨对照；只保留对应的单一阅读成品")
        diagnostic_codes.append("mode_contract")
    if mode == "summary" and len(visible_text) > 20_000:
        warnings.append("summary 过长，可能偏离快速阅读目标")
    if mode == 'complete':
        if complete_quote_check is None:
            raise ValueError("complete 校验器不可用")
        quote_result = complete_quote_check(text, workspace, sources)
        if not isinstance(quote_result, tuple) or len(quote_result) not in {2, 3}:
            raise ValueError('complete quote checker returned an invalid result')
        quote_errors, quote_warnings = quote_result[:2]
        errors.extend(quote_errors)
        warnings.extend(quote_warnings)
        if quote_errors:
            from .source_excerpt import QUOTE_DIAGNOSTIC_CODES
            supplied = quote_result[2] if len(quote_result) == 3 else ()
            safe_codes = [code for code in supplied if code in QUOTE_DIAGNOSTIC_CODES]
            diagnostic_codes.extend(safe_codes or ["quote_integrity"])
    if re.search(r"<details class=\"complete-detail\">\s*<summary>[^\n]*</summary>\s*\n\s*\n", text):
        warnings.append("<details class=\"complete-detail\"> 折叠框在 summary 后存在裸空行，会打断 CommonMark/Typora HTML 块解析并导致 </details> 标签外溢；内容段落应使用 <p>...</p> 紧凑包裹")
    from .math_markup import math_diagnostics, normalize_table_math
    math_errors = math_diagnostics(text)
    errors.extend(math_errors)
    if math_errors:
        diagnostic_codes.append("math_integrity")
    if math_errors:
        warnings.append("公式中包含制表符或转义损坏字符（如 $	o$ 代替了 \\to 或 →）；流程演变建议使用标准 Unicode 箭头 →")
    if mode in {'complete', 'full', 'deep', 'summary'}:
        from .numeric_review import review_numeric_output
        numeric = review_numeric_output(text, output_mode=mode)
        mismatches = sum(item.status == 'mismatch' for item in numeric.numeric)
        if mismatches:
            warnings.append(f'发现 {mismatches} 条可复算十进制等式的左右值不一致；这只是候选证据，需核对引用身份、数制和上下文，不能静默改写原话')
        if numeric.completeness != 'complete':
            warnings.append('数值候选检查达到受限范围，未检查部分不代表正确；仍需独立语义复核')
    root = workspace.resolve(strict=True)
    # Validate the full table row, including cells previously lost to math pipes.
    resources = check_resources(normalize_table_math(text), root,
                                root / (output_rel or 'notes/unknown.md'), options=options)
    image_links = len(resources.images)
    errors.extend(resources.errors)
    if resources.errors:
        diagnostic_codes.append("resource_policy")

    if errors and not diagnostic_codes:
        diagnostic_codes.append("markdown_structure")
    report = NoteValidationReport(not errors, output_rel, tuple(errors), image_links, output_hash,
                                  tuple(warnings), tuple(dict.fromkeys(diagnostic_codes)))
    if update_state:
        try:
            state["agent_output"] = {
                "status": "validated" if report.ok else "invalid",
                "path": output_rel,
                "sha256": output_hash,
                "image_links": image_links,
                "errors": list(report.errors),
                "warnings": list(report.warnings),
                "diagnostic_codes": list(report.diagnostic_codes),
                "semantic_review": "required",
            }
            _dump(workspace / "state.json", state)
        except OSError:
            pass
    return report
