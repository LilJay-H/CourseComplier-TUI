# Course Compiler TUI

独立的本地 CLI/TUI：接收本地音视频、转写和标准资源 ZIP；浏览器导出 ZIP 经独立安装的采集插件确认媒体域名后在本机下载，再转写、整理工作区，再交给用户自己安装并登录的 Codex / Claude CLI 执行 Workflow，生成 Markdown 与离线 HTML。

保留 Complete / Deep / Full / Summary / Verbatim、手动交接、已有讲义续作和结构校验。本仓库包含本地 Workflow 必需规则；不包含 GUI、Server、团队高精度 Agent 服务、云客户端或团队 token。

这是面向开源方向的独立仓库，当前托管于 [GitHub 私有仓库](https://github.com/LilJay-H/CourseComplier-TUI)，尚未公开；许可证尚未确认，见 [来源与归属](NOTICE.md)。包名仍为 `handouter`，两个命令 `course-compiler`、`handouter` 兼容。请用独立虚拟环境，避免覆盖旧混合仓库的同名安装。

## 安装与启动

需要 Python >=3.11,<4。本轮在 Windows x64 / CPython 3.12.14 验证轻量安装；其他 Python/平台和真实模型未验证。

PowerShell（在仓库根目录）：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[html]"
.\.venv\Scripts\course-compiler.exe tui
```

macOS/Linux 对应使用 `.venv/bin/python` 和 `.venv/bin/course-compiler`。Windows 使用 `windows-curses`，其他平台使用系统/stdlib curses。请在至少 94 列 × 30 行的正常交互终端运行 TUI；不支持无终端的管道会话。

上述轻量安装支持已有转写/PPT、Agent 交接、校验、PDF/HTML，不下载模型。音视频处理还需要 PATH 中的 FFmpeg/ffprobe；ASR 需要 FunASR/Torch/torchaudio。

完整准备可复用安装器：

```powershell
python setup_handouter.py
python setup_handouter.py --start
```

安装器创建或复用项目 `.venv`，安装 ASR/HTML 依赖，检查 FFmpeg（缺失时尝试系统包管理器），下载并加载 SenseVoiceSmall + FSMN-VAD，再检查运行时。`--skip-model-download` 只跳过模型准备；`--without-asr` 不装 ASR、不下模型，但仍检查媒体工具。已有环境不会自动删掉或升级解释器。Windows 也可用 `setup-course-compiler.cmd` / `start-course-compiler.cmd`；macOS 对应 `.command`。Linux 若缺 FFmpeg，先由系统包管理器安装。

TUI 沿用 **FunASR + Torch 的 SenseVoiceSmall / FSMN-VAD**，支持 CPU 和可用的 MPS/CUDA。GUI 的 GGUF F16 组件体系不在本仓库；没有将两种模型格式混用。包的 Python 范围不代表所有 ASR 原生依赖在每个平台都有可用 wheel。

## 浏览器导出 ZIP 与采集插件

智云采集器及脚本已迁至 [独立插件仓库](https://github.com/Silhouette-my/zhiyun-collector)，不随 TUI 安装包分发。先按[外部采集器指南](docs/EXTERNAL_COLLECTOR.md)安装、核对身份并配置。TUI 显示域名，用户确认后才下载；标准 ZIP、TXT、音视频不需要插件。

## 用户自己的 Agent

先自行安装并登录 `codex` 或 `claude`，确保在当前终端 PATH 可见，且能够单独正常工作。

```powershell
.\.venv\Scripts\course-compiler.exe agents
.\.venv\Scripts\course-compiler.exe doctor --require core pdf html tui
.\.venv\Scripts\course-compiler.exe run --transcript lecture.txt --course-title "示例课程" --output-dir output --agent codex
```

`--agent none` 只准备交接；`manual` 生成 ZIP；`web` 生成文本和可选 PDF；`codex` / `claude` 显式启动对应 CLI。模型使用和费用由用户自己的工具/账号决定。本地 Agent 不等于本地模型，材料可能按其配置发送到服务商；本软件不读取或托管用户 LLM API Key，也不自动连接团队服务。当前适配器要求 Codex 支持 `exec --sandbox workspace-write --skip-git-repo-check --ephemeral`，Claude 支持 `--print --permission-mode acceptEdits`。本次验证适配器和合成子进程，未执行真实模型调用。

更多用法见 [使用指南](docs/LOCAL_USER_GUIDE.md)、[现有输入协议](docs/LOCAL_ASSET_INPUT.md)、[本地编辑流程](docs/LOCAL_EDITORIAL_FLOW.md)、[输出目录](docs/OUTPUT_ORGANIZATION.md)。原始材料和旧讲义不覆写；校验通过不代表语义正确。

## 开发与验证

```powershell
.\.venv\Scripts\python.exe scripts/check_release.py --profile full
.\.venv\Scripts\python.exe -c "import handouter_build as b; print(b.build_wheel('dist')); print(b.build_sdist('dist'))"
```

`core` 是较小开发门禁，测试前需要 PATH 中的 FFmpeg/ffprobe；浏览器脚本测试在独立插件仓库运行。`full` 包含本地输入、媒体/模拟 ASR、交接、续作、成品和 Windows 进程相关路径；全部保留测试用 `--exhaustive`，不包含云测试。构建源码白名单为 `release-files.json`，增加发行文件时同步更新。wheel 安装后可在任意目录运行，不要求原仓库存在。

从任意目录安装构建物（路径按实际位置调整）：

```powershell
python -m venv C:\Temp\course-compiler-env
C:\Temp\course-compiler-env\Scripts\python.exe -m pip install "E:\codex\CourseComplier-TUI\dist\handouter-0.2.0-py3-none-any.whl[html]"
C:\Temp\course-compiler-env\Scripts\course-compiler.exe tui
```

独立安装、源码包重建与合成链路说明见 [验证入口](docs/RELEASE.md)，实际范围见 [当前状态](PROJECT_STATUS.md)。
