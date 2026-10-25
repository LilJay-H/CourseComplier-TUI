# TUI 抽取归属

| 范围 | 处置与理由 |
|---|---|
| CLI/TUI 及传递依赖的 64 个 Python 文件 | 原样抽取为主，包含材料、媒体、ASR、工作区、校验、HTML、Agent adapter |
| .agents/skills/zhiyun-lecture-notes | 保留 33 个 Markdown 规则/合成示例，wheel 内置资源；local_agent_workflow/editorial_review 的运行时规则也保留 |
| authoring_v2 / authoring_eval / composer / audit 等 | 存在 CLI 或其传递依赖，不按文件名排除；保留显式本地分阶段/审计能力 |
| handouter_local/sensevoice_normalize.py | 仅此标准库模块迁入 handouter，ASR 改为相对导入 |
| 站点采集器 / zhiyun_exporter.user.js | 迁至 Silhouette-my/zhiyun-collector；TUI 只保留通用、显式配置的外部工具宿主 |
| handouter_local 的 __init__ / assets / contract | 仅离线标准包校验、流式解包与错误类型；无 GUI RPC 或 ASR 组件 |
| GUI / handouter_server / handouter_client / handouter_protocol / 其余 handouter_local | 不抽取，属于原闭源 GUI/Server 产品 |
| authoring_experiments / experiment_evidence / experiment_invocations / review_evidence | 不在 CLI/TUI 依赖闭包中，不抽取实验工具 |
| .git / build / scratch / archive / deploy / 课程材料 / 私有配置 | 不从源仓库复制；新建 Git 历史；新 build 仅存本次合成证据 |
| 测试 | 保留本地适用测试，混合云测试与实验准备测试排除；旧 download_slides 包装测试改测实际 importer |

最小改造包括独立发行白名单和依赖、README/AGENTS/验证入口，以及 Windows 实测暴露的兼容修复：本地 ZIP 转发目录参数、ASR fsync 使用可写文件描述符、显式 POSIX 路径解析不受 Windows Path 影响。测试中的路径、环境变量大小写、权限与进程假设已按平台修正。

完整来源映射与原文件摘要见 ../provenance.json。根 NOTICE.md 保留来源提交与归属；没有新增许可证，也没有复制原 Git 历史。
