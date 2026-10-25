"""Command-line entry point for Course Compiler's local material pipeline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agents import available_cli_agents, create_manual_bundle, run_cli_agent
from .agents.web import create_web_handoff
from .agents.base import expected_outputs, read_handoff
from .asr import transcribe_sensevoice
from .doctor import run_doctor
from .handoff import MODE_ORDER
from .media import extract_audio
from .revision import read_revision_note
from tempfile import TemporaryDirectory
from .product import inspect_asset_zip, inspect_input_source, product_artifacts, resolve_asset_zip, resolve_input_source
from .service import (BuildResult, PrepareResult, build_from_asset_zip, build_from_audio,
                      build_from_local_asset_zip, build_from_media, prepare_lecture, refresh_handoff)
from .validation import validate_note_output, validate_workspace


def _selected_modes(args: argparse.Namespace) -> list[str]:
    modes = getattr(args, "modes", None)
    if modes:
        return list(dict.fromkeys(modes))
    return [getattr(args, "mode", None) or "deep"]


def _course_output_context(course_title: str, *, explicit=None):
    from .settings import (load_directory_settings, resolve_course_title, resolve_output_directory,
                           aliases_for_course, course_folder_mode)
    settings = load_directory_settings()
    canonical = resolve_course_title(course_title, settings=settings)
    return (canonical,
            resolve_output_directory(canonical, explicit=explicit, settings=settings),
            aliases_for_course(canonical, settings=settings),
            course_folder_mode(canonical, settings=settings))


def _progress_event(stage: str, percent: int, detail: str = "") -> None:
    width = 24
    filled = round(width * max(0, min(100, percent)) / 100)
    bar = "#" * filled + "-" * (width - filled)
    suffix = f"  {detail}" if detail else ""
    print(f"[progress] {stage:<10} [{bar}] {percent:3d}%{suffix}", file=sys.stderr, flush=True)


def _lecture_args(parser: argparse.ArgumentParser, *, transcript: bool) -> None:
    parser.add_argument("--lecture-id", required=True, help="稳定讲次 ID；不能包含路径分隔符")
    parser.add_argument("--course-title", required=True, help="课程显示名称")
    _organization_args(parser)
    if transcript:
        parser.add_argument("--transcript", required=True, help="已有 UTF-8 转写 TXT")
        parser.add_argument("--transcript-origin", choices=("asr_text", "provided_transcript", "generated_note"),
                            default=None, help="输入身份：原始 ASR 文本/身份未核实的转写/生成讲义（含生成逐字稿）；不表示已核音频")
        parser.add_argument("--segments", help="可选结构化 ASR segments.json")
        parser.add_argument("--raw-asr", help="可选原始 ASR raw.json")
        parser.add_argument("--audio", help="可选本地音频证据；复制进工作区但不会交给 Agent")
    parser.add_argument("--slides-dir", help="可选 PPT 图片目录；可内含 slides_meta.json")
    parser.add_argument("--slides-meta", help="可选独立 slides_meta.json；用于旧版元数据与图片分目录布局")
    parser.add_argument("--slides-zip", help="可选浏览器资产 ZIP；与 slides-dir/slides-meta 互斥，private/ 不会进入交接")
    parser.add_argument("--workspace-root", "--output-dir", dest="workspace_root", default=None, help="输出根目录；默认使用保存设置或桌面 Handouter/输出")
    _mode_args(parser)


def _organization_args(parser):
    from .organization import lecture_date
    parser.add_argument('--lecture-date', type=lecture_date, help='真实授课日期 YYYY-MM-DD；未知留空，不能用导出日期替代')
    parser.add_argument('--legacy-layout', action='store_true', help='兼容已有脚本：新建旧版 ID 工作区布局')


def _mode_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mode", choices=MODE_ORDER, default="deep", help="兼容单交付物入口")
    parser.add_argument("--modes", nargs="+", choices=MODE_ORDER, help="一次选择多个交付物；complete 为可展开完整讲义，不要求另选其他模式")
    parser.add_argument("--format-profile", choices=("clean", "traceable"), default="clean", help="最终讲义格式：clean 干净阅读版（默认）或 traceable 可见来源版")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--use-slides-as-source", dest="use_slides", action="store_true", help="允许 Agent 参考 PPT")
    source.add_argument("--ignore-slides", dest="use_slides", action="store_false", help="即使提供 PPT，也不允许 Agent 使用")
    parser.set_defaults(use_slides=None)
    parser.add_argument("--embed-slides", action="store_true", help="允许最终 Markdown 嵌入 PPT；同时启用 PPT 来源")
    parser.add_argument("--allow-web", action="store_true", help="允许 Agent 使用外部资料补充；默认关闭")
    parser.add_argument("--skill-path", help="覆盖默认讲义 Skill 路径")


def _agent_args(parser: argparse.ArgumentParser, *, default: str = "none") -> None:
    parser.add_argument(
        "--agent",
        choices=("none", "web", "manual", "codex", "claude"),
        default=default,
        help="none=只准备 handoff；web=网页 Chat 文本/PDF；manual=ZIP 包；codex/claude=本机 CLI Agent",
    )
    parser.add_argument("--agent-model", help="可选覆盖 CLI Agent 使用的模型")
    parser.add_argument("--bundle-output", help="manual 模式可选 GUI handoff ZIP 输出路径")


def _asr_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--device", choices=("auto", "cpu", "mps", "cuda"), default="auto")
    parser.add_argument("--language", default="auto", help="SenseVoice 语言提示，默认 auto")
    parser.add_argument("--model-path", help="可选本地 SenseVoice 模型路径；不填则使用默认模型 ID")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="course-compiler",
        description="Local-first lecture material preparation and Agent handoff.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="面向普通用户：站点资产 ZIP 或本地音频/媒体/转写 -> 标准工作区与讲义")
    run.add_argument("--input-dir", default=None, help="扫描 ZIP 的目录；默认保存设置或桌面 Handouter/输入")
    run.add_argument("--output-dir", default=None, help="输出根目录；默认保存设置或桌面 Handouter/输出")
    _organization_args(run)
    run_source_input = run.add_mutually_exclusive_group()
    run_source_input.add_argument("--asset", help="智云 ZIP 或含 handouter-input.json 的站点无关本地资产 ZIP")
    run_source_input.add_argument("--audio", help="本地音频文件；运行本地 ASR")
    run_source_input.add_argument("--media", help="本地音频/视频文件；先抽取音频再运行本地 ASR")
    run_source_input.add_argument("--transcript", help="已有 UTF-8 TXT 转写；不运行 ASR")
    run.add_argument("--transcript-origin", choices=("asr_text", "provided_transcript"),
                     default="provided_transcript", help="--transcript 的身份，默认 provided_transcript")
    run.add_argument("--audio-evidence", help="使用 --transcript 时可附带的本地音频证据；不会交给 Agent")
    run.add_argument("--slides-dir", help="本地 PPT 图片目录；可内含 slides_meta.json")
    run.add_argument("--slides-meta", help="独立 slides_meta.json；须同时提供 --slides-dir")
    run.add_argument("--slides-zip", help="仅含 PPT 图片/元数据的 ZIP；与 slides-dir/slides-meta 互斥")
    run.add_argument("--lecture-id", help="可选；站点 ZIP 读取元数据，本地文件默认使用内容哈希 ID")
    run.add_argument("--course-title", help="可选；站点 ZIP 读取元数据，本地文件默认使用文件名")
    run.add_argument("--mode", choices=MODE_ORDER, default="deep", help="兼容单交付物入口")
    run.add_argument("--modes", nargs="+", choices=MODE_ORDER, help="多选输出；verbatim=清晰逐字稿，full=完整课堂笔记，deep=图文教程，summary=速览，complete=可展开完整讲义")
    run.add_argument("--format-profile", choices=("clean", "traceable"), default="clean")
    run_source = run.add_mutually_exclusive_group()
    run_source.add_argument("--use-slides-as-source", dest="use_slides", action="store_true", help="允许 Agent 参考 PPT")
    run_source.add_argument("--ignore-slides", dest="use_slides", action="store_false", help="完全不让 Agent 使用 PPT")
    run.set_defaults(use_slides=None)
    run.add_argument("--embed-slides", action="store_true", help="最终讲义可嵌 PPT 图片")
    run.add_argument("--allow-web", action="store_true", help="允许 Agent 联网补充，默认关闭")
    run.add_argument("--skill-path", help="覆盖默认讲义 Skill 路径")
    run.add_argument('--new-version', action='store_true', help='已有相同材料任务显式刷新 Prompt，再创建新版本')
    _agent_args(run, default="none")
    _asr_args(run)

    prepare = sub.add_parser("prepare", help="高级入口：已有转写/PPT -> 标准工作区 -> Agent 交接包")
    _lecture_args(prepare, transcript=True)

    audio = sub.add_parser("build-audio", help="高级入口：本地音频 -> SenseVoice -> 标准工作区 -> Agent 交接包")
    audio.add_argument("audio_input", help="本地音频文件")
    _lecture_args(audio, transcript=False)
    _asr_args(audio)

    media = sub.add_parser("build-media", help="本地/授权媒体 -> 抽音频 -> SenseVoice -> Agent 交接包")
    media.add_argument("media_source", help="本地视频/媒体路径，或用户有权访问的 HTTP(S) 媒体地址")
    _lecture_args(media, transcript=False)
    _asr_args(media)

    asset = sub.add_parser("build-asset", help="新版浏览器资产 ZIP -> 音频/PPT/ASR -> Agent 交接包")
    asset.add_argument("asset_zip", help="zhiyun_exporter.user.js v1.4+ 导出的私有资产 ZIP")
    _lecture_args(asset, transcript=False)
    _asr_args(asset)

    extract = sub.add_parser("extract-audio", help="只抽取音频；新文件写入并验证，不上传云端")
    extract.add_argument("source", help="本地媒体或授权 HTTP(S) 地址")
    extract.add_argument("output", help="新的音频输出路径，例如 lecture.m4a")

    transcribe = sub.add_parser("transcribe", help="只运行本地 SenseVoice 并保存 raw/segments/txt")
    transcribe.add_argument("audio", help="本地音频文件")
    transcribe.add_argument("output_dir", help="新的 ASR 输出目录")
    _asr_args(transcribe)

    validate = sub.add_parser("validate", help="检查已有工作区材料、hash、PPT/ASR/交接结构")
    validate.add_argument("workspace", help="讲次工作区路径")

    prompt = sub.add_parser("prompt", help="已有课程工作区只刷新 Prompt/格式，不重新跑音频或 ASR")
    prompt.add_argument("workspace", help="已有讲次工作区，例如 output/course-001")
    prompt.add_argument("--mode", choices=MODE_ORDER, help="兼容单交付物入口")
    prompt.add_argument("--modes", nargs="+", choices=MODE_ORDER, help="刷新为多个交付物；不填则沿用当前选择")
    prompt.add_argument("--format-profile", choices=("clean", "traceable"), help="不填则沿用当前格式")
    prompt_source = prompt.add_mutually_exclusive_group()
    prompt_source.add_argument("--use-slides-as-source", dest="use_slides", action="store_true")
    prompt_source.add_argument("--ignore-slides", dest="use_slides", action="store_false")
    prompt.set_defaults(use_slides=None)
    prompt.add_argument("--embed-slides", action=argparse.BooleanOptionalAction, default=None)
    prompt.add_argument("--allow-web", action=argparse.BooleanOptionalAction, default=None)
    prompt.add_argument("--skill-path", help="覆盖默认讲义 Skill 路径")
    prompt.add_argument('--authoring-version', type=int, choices=(1, 2), default=None,
                        help='显式刷新编辑协议；2 为未做真实模型验收的候选，默认保留原任务版本')
    prompt.add_argument('--staged-authoring', action=argparse.BooleanOptionalAction, default=None,
                        help='显式启用/关闭 v2 分阶段 CLI 候选；默认保留原任务配置')
    prompt.add_argument('--compose-blocks', action=argparse.BooleanOptionalAction, default=None,
                        help='分阶段候选中由 Host 生成原文/来源/图块/折叠容器；默认保留配置')
    note_input = prompt.add_mutually_exclusive_group()
    note_input.add_argument("--source-note", help="明确提交已有 .md/.txt 讲义，复用原工作区材料继续处理")
    note_input.add_argument("--clear-source-note", action="store_true", help="清除上次提交的讲义输入，恢复从原始材料生成")
    prompt.add_argument("--revision-action", choices=("derive", "illustrate"), help="derive=派生所选版本；illustrate=只补图")
    _agent_args(prompt, default="none")

    revise = sub.add_parser("revise", help="直接提交已有讲义创建新任务，不下载音频或运行 ASR")
    revise.add_argument("source_note", help="UTF-8 .md/.txt 讲义")
    revise.add_argument("--output-dir", default=None, help="新任务输出目录；默认保存设置或桌面 Handouter/输出")
    _organization_args(revise)
    revise.add_argument("--lecture-id", required=True, help="新的任务 ID，不能覆盖已有工作区")
    revise.add_argument("--course-title", help="课程标题，默认取讲义文件名")
    revise.add_argument("--revision-action", choices=("derive", "illustrate"), default="derive")
    revise.add_argument("--slides-dir", help="可选已有课件图片目录")
    revise.add_argument("--slides-zip", help="可选课程资产 ZIP，仅导入课件，不下载音频")
    _mode_args(revise)
    _agent_args(revise, default="web")

    agents = sub.add_parser("agents", help="检查 Course Compiler 可用的 Agent 交互后端")

    agent_run = sub.add_parser("agent-run", help="对已有 handoff 自动运行本机 CLI Agent 并校验输出")
    agent_run.add_argument("workspace", help="已有讲次工作区")
    agent_run.add_argument("--agent", choices=("codex", "claude"), required=True)
    agent_run.add_argument("--agent-model", help="可选覆盖 CLI Agent 使用的模型")

    audit = sub.add_parser('authoring-audit', help='显式审计已有 staged 候选；修复另存，不覆盖成果')
    audit.add_argument('workspace')
    audit.add_argument('--agent', choices=('codex', 'claude', 'manual', 'web'), required=True)
    audit.add_argument('--mode', choices=('verbatim', 'full', 'deep', 'summary', 'complete'), required=True)
    audit.add_argument('--agent-model')
    audit.add_argument('--repair', action='store_true')
    audit.add_argument('--max-repairs', type=int, default=2)
    audit.add_argument('--run-id')
    audit.add_argument('--continue-run', action='store_true', help='续作同一审计 run；必须提供 --run-id')
    audit.add_argument('--max-calls-per-run', type=int, default=8)
    audit.add_argument('--max-calls-total', type=int, default=128)
    audit.add_argument('--max-total-bytes', type=int, default=512 * 1024 * 1024)

    bundle = sub.add_parser("bundle", help="为 GUI/Web Agent 生成可上传的安全 handoff ZIP")
    bundle.add_argument("workspace", help="已有讲次工作区")
    bundle.add_argument("--output", help="可选 bundle 输出路径；默认写入 workspace/handoff/")
    web_handoff = sub.add_parser("web-handoff", help="为网页 Chat 生成直接上传的文本和可选课件 PDF")
    web_handoff.add_argument("workspace", help="已有讲次工作区")

    note = sub.add_parser("validate-note", help="Agent 生成后检查 Markdown 路径、图片链接、凭证泄漏等结构问题")
    note.add_argument("workspace", help="讲次工作区路径")
    note.add_argument("--output", help="只检查指定的 notes/ 下输出；默认检查全部 expected_outputs")
    note.add_argument("--update-state", action="store_true", help="将结构校验结果写入 state.json；不代表语义验收通过")

    context = sub.add_parser('editorial-context', help='只读有界来源/正文导航与精确原话；不调用模型')
    context.add_argument('workspace')
    scopes = context.add_mutually_exclusive_group()
    scopes.add_argument('--source-ref', help='transcript:Lx-Ly 或已有 SourceRef JSON')
    scopes.add_argument('--mode', choices=MODE_ORDER)
    scopes.add_argument('--query', help='转写中的字面搜索，仅导航')
    context.add_argument('--start', type=int, default=0)
    context.add_argument('--max-bytes', type=int, default=8192)
    context.add_argument('--quote', action='store_true', help='精确整段行引用；超限拒绝，不静默截断')
    calculation = sub.add_parser('editorial-calculate', help='有界十进制有理数复算；不执行Python/布尔/位宽运算')
    calculation.add_argument('expression')
    calculation.add_argument('--domain', required=True, choices=('decimal_rational',))
    review_template = sub.add_parser('editorial-review-template', help='输出当前任务/稿件绑定与可选复核Schema')
    review_template.add_argument('workspace')
    review_template.add_argument('--mode', required=True, choices=MODE_ORDER)
    review_import = sub.add_parser('editorial-review-import', help='保存有绑定的复核声明，不认证语义、不修改讲义')
    review_import.add_argument('workspace')
    review_import.add_argument('returned_review')

    html_export = sub.add_parser("export-html", help="将 Markdown 或整个讲次导出为离线单文件 HTML")
    html_export.add_argument("source", help="Markdown 文件或包含 notes/ 的讲次文件夹")
    typora_export = sub.add_parser('export-typora', help='生成不依赖自定义类的 Typora 图文阅读副本；不修改原稿')
    typora_export.add_argument('source', help='一个 Markdown 文件；图片须在原相对路径可读')
    typora_export.add_argument('--repair-latex-escapes', action='store_true',
                              help='仅在新副本中恢复已知 LaTeX 转义损坏；未知损坏仍拒绝导出，不修改原稿')
    status = sub.add_parser('task-status', help='查看本次任务各模式 Markdown/HTML 和下一步')
    status.add_argument('workspace')
    continuation = sub.add_parser('continue', help='继续已有讲次，无需原 ZIP；完成后不重跑 Agent')
    continuation.add_argument('workspace')
    continuation.add_argument('--new-version', action='store_true', help='明确刷新为新任务版本')
    _agent_args(continuation, default='web')
    retry = sub.add_parser('retry-html', help='仅补齐当前任务 HTML，不调用 Agent/ASR')
    retry.add_argument('workspace')
    returned = sub.add_parser('import-note', help='接收网页返回稿，绑定讲次/任务/模式，校验后排他保存')
    returned.add_argument('workspace')
    returned.add_argument('returned_note')
    returned.add_argument('--mode', required=True, choices=MODE_ORDER)
    returned.add_argument('--task-id', required=True, help='task-status 显示的当前任务 ID')
    returned.add_argument('--no-html', action='store_true', help='此模式仅交付 Markdown；续作不补 HTML，显式 retry-html 可重新请求')
    returned.add_argument('--authoring-record', help='v2 候选任务的独立 progress.json；不将 JSON 混入正文')
    cache = sub.add_parser('cache-status', help='预览输出目录下可复用缓存；不删除任何文件')
    cache.add_argument('output_dir')
    cleanup = sub.add_parser('cache-clean', help='只清理精确缓存条目；默认预览，--confirm 才删除')
    cleanup.add_argument('output_dir')
    cleanup.add_argument('--entry', required=True, help='cache-status 列出的 lecture-id/hash')
    cleanup.add_argument('--confirm', action='store_true')
    doctor = sub.add_parser('doctor', help='按能力检查运行环境；不安装依赖、不调用模型')
    doctor.add_argument('--native-asr', action='store_true', help='实际导入原生 ASR 依赖并运行合成 fbank，不下载模型')
    doctor.add_argument('--agent', choices=('codex', 'claude'), help='检查所选 CLI 是否存在；不验证登录或模型额度')
    doctor.add_argument('--require', nargs='+', choices=('core', 'media', 'asr', 'pdf', 'html', 'tui'), help='只以请求能力决定退出码')
    directories = sub.add_parser('directories', help='查看或保存默认目录与每门课的输出目录；不搬迁课程')
    directories.add_argument('--input-dir', help='默认输入目录；可单独修改')
    directories.add_argument('--output-dir', help='默认输出目录；可单独修改')
    directories.add_argument('--course-title', help='课程正式名称或已登记别名')
    directories.add_argument('--course-alias', action='append', help='为课程增加别名；可重复，例如 --course-alias 计组')
    directories.add_argument('--clear-course-aliases', action='store_true', help='清空该课程全部别名')
    directories.add_argument('--course-folder-name', choices=('auto', 'always', 'never'), help='课程文件夹是否带课程名：auto/always/never')
    course_output = directories.add_mutually_exclusive_group()
    course_output.add_argument('--course-output-dir', help='该课程专用输出根目录；优先于默认输出目录')
    course_output.add_argument('--clear-course-output', action='store_true', help='清除该课程专用目录，恢复继承默认输出目录')
    sub.add_parser("tui", help="启动标准库 curses 全屏 ASCII TUI；可选择 GUI handoff / Codex CLI / Claude CLI")
    return parser


def _prepare_payload(result: PrepareResult) -> dict:
    return {
        "status": "handoff_ready",
        "workspace": str(result.workspace),
        "lecture_id": result.materials.lecture_id,
        "mode": result.handoff.mode,
        "modes": list(result.handoff.modes),
        "format_profile": result.handoff.format_profile,
        "prompt": str(result.workspace / result.handoff.prompt_path),
        "sources": str(result.workspace / result.handoff.sources_path),
        "expected_agent_output": str(result.workspace / result.handoff.output_path),
        "expected_agent_outputs": {mode: str(result.workspace / path) for mode, path in result.handoff.output_paths.items()},
        "slides": {
            "events": result.materials.slide_event_count,
            "images": result.materials.slide_image_count,
            "missing": result.materials.slide_missing_count,
            "unknown_time": result.materials.slide_unknown_time_count,
            "use_as_source": result.handoff.use_slides_as_source,
            "embed": result.handoff.embed_slides,
        },
        "validation": "passed",
        "agent_was_run": False,
        "artifacts": product_artifacts(result.workspace).as_dict(),
    }


def _require_delivery(agent: str) -> None:
    from .doctor import require_features
    features = ['core']
    if agent == 'web':
        features.append('pdf')
    if agent in {'codex', 'claude'}:
        features.append('html')
    require_features(features, agent=agent if agent in {'codex', 'claude'} else None)


def _apply_agent_interaction(
    workspace: str | Path,
    *,
    agent: str,
    agent_model: str | None = None,
    bundle_output: str | Path | None = None,
) -> tuple[dict, int]:
    mode = (agent or "none").strip().lower()
    _require_delivery(mode)
    if mode == "none":
        return {"mode": "none", "agent_was_run": False}, 0
    if mode == "web":
        if bundle_output is not None:
            raise ValueError("--bundle-output 仅适用于 manual ZIP 模式")
        from .tasks import plan_task
        pending = plan_task(workspace)
        result = create_web_handoff(workspace, modes=list(pending.pending) if pending.action == 'partial' else None)
        return {
            "mode": "web", "agent_was_run": False,
            "upload_directory": str(result.directory),
            "upload_files": [str(p) for p in (result.materials, result.slides) if p],
            "expected_outputs": result.expected_outputs,
            "instruction": "上传列出的文件到网页 Chat；无需 ZIP 或文件夹。",
        }, 0
    if mode == "manual":
        from .tasks import plan_task
        pending = plan_task(workspace)
        bundle = create_manual_bundle(workspace, output=bundle_output,
            modes=list(pending.pending) if pending.action == 'partial' else None)
        return {
            "mode": "manual",
            "agent_was_run": False,
            "prompt": str(bundle.prompt),
            "bundle": str(bundle.bundle),
            "expected_outputs": bundle.expected_outputs,
            "included_files": list(bundle.included_files),
        }, 0
    result = run_cli_agent(workspace, agent=mode, model=agent_model)
    if not result.validation_ok:
        print('course-compiler: error: Agent 阶段未完整通过；已发布的合格成果保留。使用 task-status 查看缺失项，再 continue 续作。', file=sys.stderr)
    from .tasks import retry_html
    html_delivery = retry_html(workspace, requested_only=True) if result.validation_ok and result.returncode == 0 else {
        'status': 'skipped', 'outputs': {}, 'errors': []}
    if html_delivery['errors']:
        print('course-compiler: error: Markdown 已保存；HTML 导出失败：' + '; '.join(html_delivery['errors']), file=sys.stderr)
    from .tasks import _publish_results
    publication = _publish_results(Path(workspace))
    return {
        "mode": "cli",
        "agent": result.agent,
        "agent_was_run": True,
        "returncode": result.returncode,
        "expected_outputs": result.outputs,
        "html_delivery": html_delivery,
        "publication": publication,
        "validation_ok": result.validation_ok,
        "validation_errors": list(result.validation_errors),
        "semantic_review": "required",
    }, (0 if result.validation_ok and result.returncode == 0 and html_delivery['status'] == 'ready' and not publication['errors'] else 3)


def _run_product(args: argparse.Namespace) -> int:
    requested = [(kind, getattr(args, kind)) for kind in ("asset", "audio", "media", "transcript")
                 if getattr(args, kind, None)]
    if requested:
        source_kind, raw_source = requested[0]
        kind_map = {"audio": "local_audio", "media": "local_media", "transcript": "local_transcript"}
        source = resolve_input_source(args.input_dir, raw_source, source_kind=kind_map.get(source_kind))
        identity = inspect_input_source(source, source_kind=kind_map.get(source_kind))
    else:
        source = resolve_asset_zip(args.input_dir, None)
        identity = inspect_input_source(source)
        source_kind = "asset"
    actual_kind = identity.source_kind
    use_slides = (args.use_slides if args.use_slides is not None else
                  bool(identity.has_slides or args.slides_dir or args.slides_zip))
    _progress_event("检查资产", 0, "正在读取输入身份与课程信息")
    _progress_event("检查资产", 100, "完成")
    lecture_id = args.lecture_id or identity.lecture_id
    course_title = args.course_title or identity.course_title
    if not course_title:
        raise ValueError("输入没有可用课程标题，请用 --course-title 指定")
    course_title, args.output_dir, course_aliases, folder_name_mode = _course_output_context(
        course_title, explicit=args.output_dir)
    if actual_kind in {"zhiyun_asset_zip", "local_asset_package"} and (args.slides_dir or args.slides_meta or args.slides_zip):
        raise ValueError("资产 ZIP 已自带其声明的 PPT；不要再传 slides-dir/slides-meta/slides-zip")
    if args.audio_evidence and actual_kind != "local_transcript":
        raise ValueError("--audio-evidence 只可与 --transcript 一起使用")
    from .tasks import plan_task
    from .stage_cache import asset_fingerprint, local_input_fingerprint
    fingerprint = (asset_fingerprint(source) if actual_kind in {"zhiyun_asset_zip", "local_asset_package"}
                   else local_input_fingerprint(source, slides_dir=args.slides_dir,
                       slides_meta=args.slides_meta, slides_zip=args.slides_zip,
                       audio_evidence=args.audio_evidence))
    from .workspace import validate_lecture_id
    from .organization import find_workspace
    workspace = find_workspace(args.output_dir, validate_lecture_id(lecture_id))
    if workspace is not None:
        if args.lecture_date:
            from .safe_io import read_json as read_safe_json
            known = read_safe_json(workspace, 'manifest.json').get('output_organization', {}).get('lecture_date')
            if known != args.lecture_date:
                raise ValueError('既有任务日期不同或未确认；保留旧目录，请使用新的讲次 ID')
        selection = {'modes': _selected_modes(args), 'format_profile': args.format_profile,
                     'use_slides_as_source': use_slides, 'embed_slides': args.embed_slides, 'allow_web': args.allow_web}
        plan = plan_task(workspace, input_fingerprint=fingerprint, selection=selection)
        if plan.action == 'refresh_required':
            # Different evidence must not be papered over by refreshing a Prompt.
            evidence = plan_task(workspace, input_fingerprint=fingerprint)
            if evidence.action == 'refresh_required' or not args.new_version:
                print(json.dumps(plan.as_dict(), ensure_ascii=False, indent=2))
                return 2
        if args.new_version:
            refresh_handoff(workspace, modes=_selected_modes(args), format_profile=args.format_profile,
                use_slides_as_source=use_slides, embed_slides=args.embed_slides, allow_web=args.allow_web,
                skill_path=args.skill_path)
        payload, code = _continue_payload(workspace, agent=args.agent, agent_model=args.agent_model)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return code
    _require_delivery(args.agent)
    common = dict(lecture_id=lecture_id, course_title=course_title, mode=args.mode,
        modes=_selected_modes(args), use_slides_as_source=use_slides,
        embed_slides=args.embed_slides, allow_web=args.allow_web,
        format_profile=args.format_profile, skill_path=args.skill_path,
        organized=not args.legacy_layout,
        lecture_date=(args.lecture_date or identity.lecture_date) if not args.legacy_layout else None,
        course_aliases=course_aliases, folder_name_mode=folder_name_mode)
    asr = dict(device=args.device, language=args.language, model_path=args.model_path, progress=_progress_event)
    slides = dict(slides_dir=args.slides_dir, slides_meta=args.slides_meta, slides_zip=args.slides_zip)
    if actual_kind == "zhiyun_asset_zip":
        result = build_from_asset_zip(args.output_dir, asset_zip=source, **common, **asr)
    elif actual_kind == "local_asset_package":
        result = build_from_local_asset_zip(args.output_dir, asset_zip=source, **common, **asr)
    elif actual_kind == "local_audio":
        result = build_from_audio(args.output_dir, audio=source, input_fingerprint=fingerprint,
                                  **common, **slides, **asr)
    elif actual_kind == "local_media":
        result = build_from_media(args.output_dir, media_source=source, source_type="media_asr",
                                  input_fingerprint=fingerprint, **common, **slides, **asr)
    else:
        result = prepare_lecture(args.output_dir, transcript=source,
            transcript_origin=args.transcript_origin, audio=args.audio_evidence,
            source_type="local_existing_materials", input_fingerprint=fingerprint,
            **common, **slides)
    prepared = result.prepare if isinstance(result, BuildResult) else result
    payload = _build_payload(result) if isinstance(result, BuildResult) else _prepare_payload(result)
    payload["input"] = {
        "directory": str(Path(args.input_dir or ".").expanduser().resolve()),
        "source": str(source),
        "source_kind": actual_kind,
        "asset": str(source) if actual_kind in {"zhiyun_asset_zip", "local_asset_package"} else None,
    }
    payload["output_directory"] = str(Path(args.output_dir).expanduser().resolve())
    _progress_event("Agent/交付", 0, "正在准备最终交付")
    interaction, code = _apply_agent_interaction(
        prepared.workspace,
        agent=args.agent,
        agent_model=args.agent_model,
        bundle_output=args.bundle_output,
    )
    _progress_event("Agent/交付", 100, "完成")
    payload["agent_interaction"] = interaction
    payload["agent_was_run"] = bool(interaction.get("agent_was_run"))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


def _prepare(args: argparse.Namespace) -> int:
    course_title, output_root, aliases, folder_mode = _course_output_context(
        args.course_title, explicit=args.workspace_root)
    result = prepare_lecture(
        output_root,
        lecture_id=args.lecture_id,
        course_title=course_title,
        transcript=args.transcript,
        transcript_origin=args.transcript_origin,
        segments=args.segments,
        raw_asr=args.raw_asr,
        audio=args.audio,
        slides_dir=args.slides_dir,
        slides_meta=args.slides_meta,
        slides_zip=args.slides_zip,
        mode=args.mode,
        modes=_selected_modes(args),
        use_slides_as_source=args.use_slides,
        embed_slides=args.embed_slides,
        allow_web=args.allow_web,
        format_profile=args.format_profile,
        skill_path=args.skill_path,
        organized=not args.legacy_layout, lecture_date=args.lecture_date,
        course_aliases=aliases, folder_name_mode=folder_mode,
    )
    print(json.dumps(_prepare_payload(result), ensure_ascii=False, indent=2))
    return 0


def _build_payload(result: BuildResult) -> dict:
    payload = _prepare_payload(result.prepare)
    payload["asr"] = {
        "segments": result.asr.segment_count,
        "timestamp_kind": result.asr.timestamp_kind,
        "device": result.asr.device,
        "elapsed_seconds": round(result.asr.elapsed_seconds, 3),
        "raw": str(result.asr.raw_path),
        "segments_path": str(result.asr.segments_path),
    }
    if result.media is not None:
        payload["media"] = {
            "audio": str(result.media.output),
            "duration_ms": result.media.duration_ms,
            "bytes": result.media.bytes,
            "extract_mode": result.media.mode,
        }
    return payload


def _build_audio(args: argparse.Namespace) -> int:
    course_title, output_root, aliases, folder_mode = _course_output_context(
        args.course_title, explicit=args.workspace_root)
    result = build_from_audio(
        output_root,
        lecture_id=args.lecture_id,
        course_title=course_title,
        audio=args.audio_input,
        slides_dir=args.slides_dir,
        slides_meta=args.slides_meta,
        slides_zip=args.slides_zip,
        mode=args.mode,
        modes=_selected_modes(args),
        use_slides_as_source=args.use_slides,
        embed_slides=args.embed_slides,
        allow_web=args.allow_web,
        format_profile=args.format_profile,
        skill_path=args.skill_path,
        device=args.device,
        language=args.language,
        model_path=args.model_path,
        organized=not args.legacy_layout, lecture_date=args.lecture_date,
        course_aliases=aliases, folder_name_mode=folder_mode,
    )
    print(json.dumps(_build_payload(result), ensure_ascii=False, indent=2))
    return 0


def _build_asset(args: argparse.Namespace) -> int:
    if args.slides_dir or args.slides_meta or args.slides_zip:
        raise ValueError("build-asset 已从 asset_zip 获取 PPT，不要再传 slides-dir/slides-meta/slides-zip")
    course_title, output_root, aliases, folder_mode = _course_output_context(
        args.course_title, explicit=args.workspace_root)
    result = build_from_asset_zip(
        output_root,
        lecture_id=args.lecture_id,
        course_title=course_title,
        asset_zip=args.asset_zip,
        mode=args.mode,
        modes=_selected_modes(args),
        use_slides_as_source=args.use_slides,
        embed_slides=args.embed_slides,
        allow_web=args.allow_web,
        format_profile=args.format_profile,
        skill_path=args.skill_path,
        device=args.device,
        language=args.language,
        model_path=args.model_path,
        organized=not args.legacy_layout,
        lecture_date=args.lecture_date or (inspect_asset_zip(args.asset_zip).lecture_date if not args.legacy_layout else None),
        course_aliases=aliases, folder_name_mode=folder_mode,
    )
    print(json.dumps(_build_payload(result), ensure_ascii=False, indent=2))
    return 0


def _build_media(args: argparse.Namespace) -> int:
    course_title, output_root, aliases, folder_mode = _course_output_context(
        args.course_title, explicit=args.workspace_root)
    result = build_from_media(
        output_root,
        lecture_id=args.lecture_id,
        course_title=course_title,
        media_source=args.media_source,
        slides_dir=args.slides_dir,
        slides_meta=args.slides_meta,
        slides_zip=args.slides_zip,
        mode=args.mode,
        modes=_selected_modes(args),
        use_slides_as_source=args.use_slides,
        embed_slides=args.embed_slides,
        allow_web=args.allow_web,
        format_profile=args.format_profile,
        skill_path=args.skill_path,
        device=args.device,
        language=args.language,
        model_path=args.model_path,
        organized=not args.legacy_layout, lecture_date=args.lecture_date,
        course_aliases=aliases, folder_name_mode=folder_mode,
    )
    print(json.dumps(_build_payload(result), ensure_ascii=False, indent=2))
    return 0


def _extract(args: argparse.Namespace) -> int:
    result = extract_audio(args.source, args.output)
    print(json.dumps({
        "status": "audio_ready",
        "output": str(result.output),
        "duration_ms": result.duration_ms,
        "bytes": result.bytes,
        "mode": result.mode,
    }, ensure_ascii=False, indent=2))
    return 0


def _transcribe(args: argparse.Namespace) -> int:
    result = transcribe_sensevoice(
        args.audio,
        args.output_dir,
        device=args.device,
        language=args.language,
        model_path=args.model_path,
    )
    print(json.dumps({
        "status": "transcript_ready",
        "output_dir": str(result.output_dir),
        "segments": result.segment_count,
        "timestamp_kind": result.timestamp_kind,
        "device": result.device,
        "elapsed_seconds": round(result.elapsed_seconds, 3),
    }, ensure_ascii=False, indent=2))
    return 0


def _revise(args: argparse.Namespace) -> int:
    _require_delivery(args.agent)
    revision = read_revision_note(args.source_note, action=args.revision_action, note_only=True)
    course_title, output_root, aliases, folder_mode = _course_output_context(
        args.course_title or Path(args.source_note).stem, explicit=args.output_dir)
    with TemporaryDirectory(prefix="handouter-note-") as temporary:
        snapshot = Path(temporary) / "submitted.txt"
        snapshot.write_text(revision["text"], encoding="utf-8")
        result = prepare_lecture(
            output_root, lecture_id=args.lecture_id,
            course_title=course_title,
            transcript=snapshot, slides_dir=args.slides_dir, slides_zip=args.slides_zip,
            modes=_selected_modes(args), use_slides_as_source=args.use_slides,
            embed_slides=args.embed_slides or args.revision_action == "illustrate",
            allow_web=args.allow_web, format_profile=args.format_profile, skill_path=args.skill_path,
            source_type="provided_note", revision=revision,
            organized=not args.legacy_layout, lecture_date=args.lecture_date,
            course_aliases=aliases, folder_name_mode=folder_mode,
        )
    interaction, code = _apply_agent_interaction(
        result.workspace, agent=args.agent, agent_model=args.agent_model, bundle_output=args.bundle_output,
    )
    print(json.dumps({"status": "revision_ready", "workspace": str(result.workspace),
                      "agent_interaction": interaction}, ensure_ascii=False, indent=2))
    return code


def _refresh_prompt(args: argparse.Namespace) -> int:
    _require_delivery(args.agent)
    result = refresh_handoff(
        args.workspace,
        mode=args.mode,
        modes=args.modes,
        use_slides_as_source=args.use_slides,
        embed_slides=args.embed_slides,
        allow_web=args.allow_web,
        format_profile=args.format_profile,
        skill_path=args.skill_path,
        source_note=args.source_note,
        revision_action=args.revision_action,
        clear_source_note=args.clear_source_note,
        authoring_version=args.authoring_version,
        staged_authoring=args.staged_authoring,
        compose_blocks=args.compose_blocks,
    )
    artifacts = product_artifacts(result.workspace)
    interaction, code = _apply_agent_interaction(
        result.workspace,
        agent=args.agent,
        agent_model=args.agent_model,
        bundle_output=args.bundle_output,
    )
    print(json.dumps({
        "status": "prompt_ready",
        "workspace": str(result.workspace),
        "mode": result.handoff.mode,
        "modes": list(result.handoff.modes),
        "format_profile": result.handoff.format_profile,
        "prompt": str(artifacts.prompt),
        "expected_agent_output": str(artifacts.expected_note),
        "expected_agent_outputs": {mode: str(path) for mode, path in artifacts.expected_notes.items()},
        "archived_prompt": str(result.archived_prompt),
        "artifacts": artifacts.as_dict(),
        "validation": "passed",
        "agent_was_run": bool(interaction.get("agent_was_run")),
        "agent_interaction": interaction,
    }, ensure_ascii=False, indent=2))
    return code


def _agents_status(_args: argparse.Namespace) -> int:
    payload = {
        "manual": {"available": True, "mode": "gui_bundle"},
        "web": {"available": True, "mode": "web_chat"},
        "cli": [
            {
                "name": item.name,
                "available": item.available,
                "executable": item.executable,
                "mode": item.mode,
            }
            for item in available_cli_agents()
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _agent_run(args: argparse.Namespace) -> int:
    payload, code = _continue_payload(args.workspace, agent=args.agent, agent_model=args.agent_model)
    interaction = payload.get('agent_interaction', {})
    payload.update(agent=args.agent, returncode=interaction.get('returncode', 0),
        outputs=interaction.get('expected_outputs', {**payload['task']['completed'], **payload['task']['pending']}),
        validation_ok=interaction.get('validation_ok', payload['status'] in {'complete', 'html_failed'}),
        validation_errors=interaction.get('validation_errors', payload['task']['errors']),
        html_delivery=interaction.get('html_delivery', payload.get('html_delivery', payload['task']['html'])))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


def _continue_payload(workspace, *, agent='web', agent_model=None, bundle_output=None):
    from .organization import resolve_workspace
    workspace = resolve_workspace(workspace)
    from .tasks import plan_task, retry_html
    from .safe_io import workspace_lock
    from .transactions import recover_refresh
    with workspace_lock(Path(workspace)):
        recover_refresh(Path(workspace))
        plan = plan_task(workspace)
        payload = {'workspace': str(Path(workspace).resolve()), 'task': plan.as_dict(),
                   'status': plan.action, 'agent_was_run': False, 'semantic_review': 'required'}
        if plan.action in {'invalid', 'busy', 'refresh_required', 'recovery_required'}:
            print('course-compiler: error: 当前任务暂不可继续；原成果保留。使用 task-status 查看原因和恢复操作。', file=sys.stderr)
            return payload, 2
        if plan.action in {'complete', 'publish_only'}:
            from .tasks import _publish_results
            publication = _publish_results(Path(workspace))
            payload['publication'] = publication
            payload['status'] = 'publication_failed' if publication['errors'] else 'complete'
            return payload, 3 if publication['errors'] else 0
        if plan.action == 'html_only':
            delivery = retry_html(workspace, requested_only=True)
            if delivery['errors']:
                print('course-compiler: error: Markdown 已保存；HTML 导出失败；使用 retry-html 重试。', file=sys.stderr)
            payload.update(html_delivery=delivery, status='complete' if delivery['status'] == 'ready' else 'html_failed')
            return payload, 0 if delivery['status'] == 'ready' else 3
        interaction, code = _apply_agent_interaction(workspace, agent=agent, agent_model=agent_model, bundle_output=bundle_output)
        payload.update(agent_interaction=interaction, agent_was_run=interaction.get('agent_was_run', False))
        payload['task'] = plan_task(workspace).as_dict()
        payload['status'] = payload['task']['action']
        return payload, code


def _bundle(args: argparse.Namespace) -> int:
    result = create_manual_bundle(args.workspace, output=args.output)
    print(json.dumps({
        "status": "gui_handoff_ready",
        "bundle": str(result.bundle),
        "prompt": str(result.prompt),
        "expected_outputs": result.expected_outputs,
        "included_files": list(result.included_files),
        "agent_was_run": False,
    }, ensure_ascii=False, indent=2))
    return 0


def _validate(args: argparse.Namespace) -> int:
    report = validate_workspace(Path(args.workspace), update_state=False)
    payload = {
        "ok": report.ok,
        "checked_files": report.checked_files,
        "slide_events": report.slide_events,
        "errors": list(report.errors),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if report.ok else 1


def _validate_note(args: argparse.Namespace) -> int:
    from .editorial_review import quality_summary
    if args.output is None:
        root, sources, _prompt = read_handoff(args.workspace)
        outputs = expected_outputs(sources)
        if len(outputs) > 1:
            reports = {
                mode: validate_note_output(root, output=relative, update_state=False)
                for mode, relative in outputs.items()
            }
            payload = {
                "ok": all(report.ok for report in reports.values()),
                "outputs": outputs,
                "reports": {
                    mode: {
                        "ok": report.ok,
                        "output": report.output_path,
                        "sha256": report.sha256,
                        "image_links": report.image_links,
                        "errors": list(report.errors),
                        "warnings": list(report.warnings),
                        "quality": quality_summary(root, sources, mode, report),
                    }
                    for mode, report in reports.items()
                },
                "errors": [f"{mode}: {error}" for mode, report in reports.items() for error in report.errors],
                "warnings": [f"{mode}: {warning}" for mode, report in reports.items() for warning in report.warnings],
                "semantic_review": "required",
            }
            if args.update_state:
                state_path = root / "state.json"
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state["agent_output"] = {
                    **payload,
                    "status": "validated" if payload["ok"] else "invalid",
                }
                state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0 if payload["ok"] else 1
    report = validate_note_output(args.workspace, output=args.output, update_state=args.update_state)
    root, sources, _ = read_handoff(args.workspace)
    mode = next((m for m, p in expected_outputs(sources).items() if p == report.output_path), sources.get('mode'))
    print(json.dumps({
        "ok": report.ok,
        "output": report.output_path,
        "sha256": report.sha256,
        "image_links": report.image_links,
        "errors": list(report.errors),
        "warnings": list(report.warnings),
        "semantic_review": "required",
        "quality": quality_summary(root, sources, mode, report),
    }, ensure_ascii=False, indent=2))
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        from .organization import resolve_workspace, cache_root_for
        from .settings import (load_directory_settings, save_directory_settings, save_course_preferences,
                               configured_course_output, resolve_output_directory, resolve_course_title,
                               aliases_for_course, course_folder_mode)
        if args.command == 'directories':
            course_action = bool(args.course_output_dir or args.clear_course_output or args.course_alias
                                 or args.clear_course_aliases or args.course_folder_name)
            if course_action and not args.course_title:
                raise ValueError('设置课程目录/别名时须提供 --course-title')
            if course_action and (args.input_dir or args.output_dir):
                raise ValueError('默认目录和课程专用设置请分开保存，避免部分更新')
            if course_action:
                current = load_directory_settings()
                canonical = resolve_course_title(args.course_title, settings=current)
                current_aliases = aliases_for_course(canonical, settings=current)
                aliases = ([] if args.clear_course_aliases else
                           list(dict.fromkeys([*current_aliases, *(args.course_alias or [])])))
                kwargs = {}
                if args.course_output_dir:
                    kwargs['output_dir'] = args.course_output_dir
                elif args.clear_course_output:
                    kwargs['output_dir'] = None
                if args.course_alias or args.clear_course_aliases:
                    kwargs['aliases'] = aliases
                if args.course_folder_name:
                    kwargs['folder_name_mode'] = args.course_folder_name
                result = save_course_preferences(canonical, **kwargs)
                if args.course_output_dir:
                    Path(resolve_output_directory(canonical, settings=result)).mkdir(parents=True, exist_ok=True)
            elif args.input_dir is not None or args.output_dir is not None:
                result = save_directory_settings(args.input_dir, args.output_dir)
                for name in ('input_dir', 'output_dir'):
                    Path(result[name]).mkdir(parents=True, exist_ok=True)
            else:
                result = load_directory_settings()
            if args.course_title:
                canonical = resolve_course_title(args.course_title, settings=result)
                result = {**result,
                          'course_title': canonical,
                          'course_aliases_for_title': aliases_for_course(canonical, settings=result),
                          'course_folder_name': course_folder_mode(canonical, settings=result),
                          'course_output_dir': configured_course_output(canonical, settings=result),
                          'effective_output_dir': resolve_output_directory(canonical, settings=result)}
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        settings = None
        if hasattr(args, 'input_dir') and getattr(args, 'input_dir') is None:
            settings = load_directory_settings()
            args.input_dir = settings['input_dir']
        new_task_output_commands = {'run', 'prepare', 'build-audio', 'build-media', 'build-asset', 'revise'}
        for field in ('output_dir', 'workspace_root'):
            if (hasattr(args, field) and getattr(args, field) is None
                    and args.command not in new_task_output_commands):
                settings = settings or load_directory_settings()
                setattr(args, field, settings['output_dir'])
        if hasattr(args, 'workspace'):
            args.workspace = str(resolve_workspace(args.workspace))
        if args.command == 'export-html' and Path(args.source).is_dir():
            args.source = str(resolve_workspace(args.source))
        if args.command == "run":
            return _run_product(args)
        if args.command == 'task-status':
            from .tasks import plan_task
            plan = plan_task(args.workspace)
            payload = plan.as_dict()
            if (Path(args.workspace) / 'state.json').is_file():
                from .safe_io import read_json as read_safe_json
                payload['publication'] = read_safe_json(Path(args.workspace), 'state.json').get('publication', {})
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 2 if plan.action in {'invalid', 'busy', 'recovery_required'} else 0
        if args.command == 'continue':
            if args.new_version:
                refresh_handoff(args.workspace)
            payload, code = _continue_payload(args.workspace, agent=args.agent,
                agent_model=args.agent_model, bundle_output=args.bundle_output)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return code
        if args.command == 'retry-html':
            from .tasks import retry_html
            result = retry_html(args.workspace)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result['status'] == 'ready' else 3
        if args.command == 'import-note':
            from .tasks import import_note
            result = import_note(args.workspace, args.returned_note, mode=args.mode, task_id=args.task_id,
                                 html=not args.no_html, authoring_record=args.authoring_record)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 3 if (result['status'] == 'needs_images' or result.get('html_delivery', {}).get('status') == 'failed'
                         or result.get('publication', {}).get('errors')) else 0
        if args.command in {'cache-status', 'cache-clean'}:
            from .tasks import inspect_cache, clean_cache
            cache_root = cache_root_for(args.output_dir)
            result = inspect_cache(cache_root) if args.command == 'cache-status' else clean_cache(
                cache_root, entry=args.entry, confirm=args.confirm)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == 'export-typora':
            from .typora_export import export_typora
            path = export_typora(args.source, repair_escapes=args.repair_latex_escapes)
            print(json.dumps({'typora_markdown': str(path), 'content_review': 'not_performed',
                              'latex_escape_repair_requested': args.repair_latex_escapes,
                              'note': '含公式的折叠块展开为普通 Markdown；图组适配 Typora，保留相对图片目录与原稿。精确自适应排版请用 export-html。'}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "export-html":
            from .html_export import export_html
            print(json.dumps({"html": [str(path) for path in export_html(args.source)]}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "prepare":
            return _prepare(args)
        if args.command == "build-audio":
            return _build_audio(args)
        if args.command == "build-media":
            return _build_media(args)
        if args.command == "build-asset":
            return _build_asset(args)
        if args.command == "extract-audio":
            return _extract(args)
        if args.command == "transcribe":
            return _transcribe(args)
        if args.command == "validate":
            return _validate(args)
        if args.command == "prompt":
            return _refresh_prompt(args)
        if args.command == 'editorial-calculate':
            from .editorial_context import calculate
            print(json.dumps(calculate(args.expression, domain=args.domain), ensure_ascii=False, indent=2))
            return 0
        if args.command == 'editorial-context':
            from .editorial_context import context
            ref = args.source_ref
            if ref and ref.lstrip().startswith('{'):
                from .authoring_v2 import strict_json
                ref = strict_json(ref.encode('utf-8'))
            result = context(args.workspace, source_ref=ref, mode=args.mode, query=args.query,
                             start=args.start, max_bytes=args.max_bytes, quote=args.quote)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == 'editorial-review-template':
            from .editorial_review import template, review_schema
            root, sources, _ = read_handoff(args.workspace)
            print(json.dumps({'template': template(root, sources, args.mode), 'schema': review_schema()},
                             ensure_ascii=False, indent=2))
            return 0
        if args.command == 'editorial-review-import':
            from .editorial_review import import_review
            print(json.dumps(import_review(args.workspace, args.returned_review), ensure_ascii=False, indent=2))
            return 0
        if args.command == "validate-note":
            return _validate_note(args)
        if args.command == "agents":
            return _agents_status(args)
        if args.command == "agent-run":
            return _agent_run(args)
        if args.command == 'authoring-audit':
            from .audit_stages import run_audit
            result = run_audit(args.workspace, agent=args.agent, mode=args.mode, model=args.agent_model,
                repair=args.repair, max_repairs=args.max_repairs, run_id=args.run_id,
                continue_run=args.continue_run, max_calls_per_run=args.max_calls_per_run,
                max_calls_total=args.max_calls_total, max_total_bytes=args.max_total_bytes)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result['status'] == 'completed' else 3
        if args.command == "bundle":
            return _bundle(args)
        if args.command == "web-handoff":
            payload, code = _apply_agent_interaction(args.workspace, agent="web")
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return code
        if args.command == "revise":
            return _revise(args)
        if args.command == "doctor":
            report = run_doctor(features=args.require, agent=args.agent, native_asr=args.native_asr)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            okay = all(report.get('ok_' + feature, False) for feature in (args.require or ['core']))
            if args.native_asr:
                okay = okay and report['asr_native']['status'] == 'passed'
            if args.agent:
                okay = okay and bool(report['agent']['available'])
            return 0 if okay else 1
        if args.command == "tui":
            from .tui import run_tui
            run_tui()
            return 0
        parser.error("unknown command")
    except (OSError, ValueError, RuntimeError, KeyError, json.JSONDecodeError) as exc:
        print(f"course-compiler: error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
