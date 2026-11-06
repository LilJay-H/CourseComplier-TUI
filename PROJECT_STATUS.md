# 当前状态

2026-10-25：TUI 已抽取为独立本地 Git 仓库，可安装、启动和继续开发。GUI、Server、云客户端/协议和 GUI runtime 未进入本仓库；本地 Workflow 规则、用户 Agent 适配器和材料链路保留。来源记录见 NOTICE.md / provenance.json，归属清单见 docs/SPLIT_SCOPE.md。

## Windows 真实课程实测与修复（2026-11-01 至 11-06，分支 fix/windows-round1）

- Windows 11 / CPython 3.14：真实 SenseVoiceSmall + FSMN-VAD（CPU）、真实 Codex 与 Claude CLI、智云插件本机构建版，跑通 8 节真实课程（CLI 7 节、交互 TUI 1 节）的 ZIP → 下载 → ASR → Agent → 校验 → 离线 HTML。讲义语义质量未评审。
- 第一轮修复：Codex 在 Windows 上 workspace-write 静默变只读（改用 bypass 参数）；Agent 登录预检与失败原因提示；CLI 相对路径；ASR 内存不足提示；CRLF 讲义；TUI 中文界面、PDCurses 宽字符显示、粘贴/拖入、目录对话框；测试套件在中文 Windows 上全绿。
- 第二轮修复：Agent 阶段每 30 秒显示已用时间；插件 `progress_bytes` 字节进度（可选能力，旧插件不受影响）；同一浏览器 ZIP 的音频已缓存时跳过视频下载；启动采集时清理被强杀遗留的临时目录；缓存音频硬链接进工作区；TUI 的 W 支持拖入原始 ZIP、错误提示中文化、只预填真实讲次、空资料框单按 W 即打开讲次；输出目录拒绝相对路径；尚未运行的讲次显示实际将创建的位置；已有讲次加模式时可保留已完成的讲义。
- 第二轮改动只有合成测试；交互 TUI 和真实下载未在第二轮人工复测。详见测试根目录的 TEST_LOG.md / ISSUES.md / TUI_ITERATION_REPORT.md。

## 本次外部采集器迁移（2026-10-25）

- 已将原仓 `0a21e87` 的通用外部采集宿主同步到协作分支 `codex/external-collector-sync`；已经 PR #1 合并到 `main`（`5280a2c`）。
- 新增 `collector-configure` / `collector-inspect`；浏览器 ZIP 在 TUI 中先确认域名，再由独立插件下载转换。TXT、音视频、标准 ZIP 和两个命令入口保留。
- 智云页面解析、私有媒体字段读取、默认站点 Referer 与浏览器脚本不再内置。插件与脚本由 `Silhouette-my/zhiyun-collector`（0.3.0.dev2）单独提供，安装见 `docs/EXTERNAL_COLLECTOR.md`。
- 仅复用 `handouter_local` 三个离线标准包校验文件；无 GUI runtime、GGUF、组件管理、云客户端。保留同伴课程别名/目录参数和 POSIX 路径修复。
- macOS / Python 3.11.16：采集宿主专项 12 项通过；full 门禁 220 项，零失败/错误，1 项真实 Windows Job Object 用例按平台跳过。覆盖域名确认、工具/材料绑定、超时子进程树清理、完整标准包预检与流式解包、缓存复用、旧输入和发行白名单。
- 临时虚拟环境源码外安装 wheel 后，`--installed-smoke --profile full` 通过；标准转写包及标准音频包经过真实 FFmpeg、模拟 ASR/Agent 子进程，获得两份 Markdown 和内嵌数学/课件图的离线 HTML。未使用真实模型。证据：`build/tests-full.json` 与 `/private/tmp/tui-sync-wheel-example-complete/summary.json`（本机临时文件，不随发行）。
- 新采集桥接尚未在 Windows 原生环境或真实课程平台验证；下方 Windows 证据属于拆分前的原行为，不代表新插件迁移的 Windows 验收。未调用真实模型或学校媒体。

## 拆分时已验证（历史证据）

- Windows x64 / CPython 3.12.14：独立开发环境、依赖一致性、源码外 wheel 安装、解压源码包安装器 `--without-asr`、两个命令入口和内置规则/导出脚本资源。
- 原生 curses TUI 在 120×40 终端显示完整主界面；过小终端给出尺寸提示。本次未操作真实课程任务。
- 本地 full 门禁及 PPT importer 专项通过，覆盖输入、材料与旧稿保护、Agent 交接、续作、HTML/交付和 Windows Job Object 子进程清理。平台/权限不适用的用例明确跳过；保留测试均可收集，不宣称全量回归已执行。
- 独立安装包合成输入：本地资产 ZIP、既有导出包格式的 loopback HTTP 媒体下载、真实 FFmpeg 抽音频、模拟 ASR 经过真实归一化和落盘、真实模拟 Agent 子进程、规则交接、讲义校验、Markdown 与离线 HTML 成品。
- wheel/sdist 不含 GUI/Server/云 SDK；源码包在临时目录重建的 wheel 各成员内容一致。构建物和机器证据在 dist/、build/，不入 Git。

## 拆分时修复

本地资产 ZIP 转发课程别名/目录模式参数；Windows ASR fsync 使用可写描述符；显式 POSIX 路径解析不被 Windows Path 转换。测试修正宿主路径、大小写、编码、符号链接权限、ZIP 原始路径及 venv redirector 与 Job Object 的区别。

## 限制与未验证

- TUI 沿用 FunASR/Torch SenseVoiceSmall + FSMN-VAD。真实模型与真实 Codex/Claude CLI 已在 Windows 上跑通（见文首 2026-11 一节）；语音识别准确率与讲义语义质量没有评审。没有团队云依赖。
- Linux、macOS 真实课程链路未实测。历史 Windows 环境无法创建 symlink；本次 macOS 合成测试的权限边界与其不同。
- 本次 wheel/sdist 和 Windows 源码启动 ZIP 不附带 FFmpeg、模型或完整离线依赖。媒体测试复用了机器现有 FFmpeg/ffprobe；其他机器需按 README 准备。
- 独立 TUI 已托管于 `LilJay-H/CourseComplier-TUI`，仓库已公开（2026-10-30 核对）；协作者有 WRITE 权限。尚未新增许可证，公开授权和许可证仍需另行决定。
