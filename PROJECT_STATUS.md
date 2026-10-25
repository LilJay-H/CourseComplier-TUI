# 当前状态

2026-10-25：TUI 已抽取为独立本地 Git 仓库，可安装、启动和继续开发。GUI、Server、云客户端/协议和 GUI runtime 未进入本仓库；本地 Workflow 规则、用户 Agent 适配器和材料链路保留。来源记录见 NOTICE.md / provenance.json，归属清单见 docs/SPLIT_SCOPE.md。

## 已验证

- Windows x64 / CPython 3.12.14：独立开发环境、依赖一致性、源码外 wheel 安装、解压源码包安装器 `--without-asr`、两个命令入口和内置规则/导出脚本资源。
- 原生 curses TUI 在 120×40 终端显示完整主界面；过小终端给出尺寸提示。本次未操作真实课程任务。
- 本地 full 门禁及 PPT importer 专项通过，覆盖输入、材料与旧稿保护、Agent 交接、续作、HTML/交付和 Windows Job Object 子进程清理。平台/权限不适用的用例明确跳过；保留测试均可收集，不宣称全量回归已执行。
- 独立安装包合成输入：本地资产 ZIP、既有导出包格式的 loopback HTTP 媒体下载、真实 FFmpeg 抽音频、模拟 ASR 经过真实归一化和落盘、真实模拟 Agent 子进程、规则交接、讲义校验、Markdown 与离线 HTML 成品。
- wheel/sdist 不含 GUI/Server/云 SDK；源码包在临时目录重建的 wheel 各成员内容一致。构建物和机器证据在 dist/、build/，不入 Git。

## 本次修复

本地资产 ZIP 转发课程别名/目录模式参数；Windows ASR fsync 使用可写描述符；显式 POSIX 路径解析不被 Windows Path 转换。测试修正宿主路径、大小写、编码、符号链接权限、ZIP 原始路径及 venv redirector 与 Job Object 的区别。

## 限制与未验证

- TUI 保留原 FunASR/Torch SenseVoiceSmall + FSMN-VAD 路线；未安装或运行真实 ASR 原生依赖/模型，未宣称语音识别准确率。完整 setup 的真实模型准备阶段未运行。
- 未调用真实 Codex/Claude 模型，登录、额度、CLI 上游兼容性及讲义语义质量待真实使用验证。没有团队云依赖。
- macOS/Linux、其他 Python 版本、真实课程平台 CDN/签名链接均未实测。当前用户权限不能创建 symlink，因此对应实际文件系统用例未执行。
- 本次 wheel/sdist 和 Windows 源码启动 ZIP 不附带 FFmpeg、模型或完整离线依赖。媒体测试复用了机器现有 FFmpeg/ffprobe；其他机器需按 README 准备。
- 未设置新仓库远端、未推送/公开；没有新增许可证。GitHub 名称、可见性、公开授权/许可证和推送由用户另行决定。
