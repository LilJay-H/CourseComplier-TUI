# 本地 TUI 使用指南

安装与验证范围见 [README](../README.md)。本仓库为独立本地 Workflow 产品，包含所需规则，不提供团队云服务。

# 第一次使用

## 1. 从智云页面导出课程资产

先按[采集插件安装指南](EXTERNAL_COLLECTOR.md)安装独立智云插件并核对身份。配套浏览器脚本在独立插件仓库，不包含在 TUI 发布包内。

在你已经登录且有权访问的课程回放页面运行该脚本，导出课程资产 ZIP。

ZIP 中会包含课程信息、PPT 和课程媒体的私有访问信息。**请把这个 ZIP 当作私有文件保存，不要公开分享。**

## 2. 启动 Course Compiler 本地 TUI

启动后进入 ASCII TUI。

本地源码版现已接入新版来源优先提示词流程：扫描材料 → 大纲回源核对 → 分单元写作 →
来源/覆盖/技术/图文分项复核 → 全稿检查。使用仓库内 `start-course-compiler.command`
（Windows 为 `.cmd`）启动，Agent 选择已安装并登录的 `Codex CLI` 或 `Claude CLI`；
沿用所选 CLI 的登录与模型配置，无需向 Course Compiler 提供模型 API Key。
请在 CLI 中确认使用的是自己的订阅登录方式；程序不读取凭证或验证订阅额度。
本机 Agent 仍可能把授权材料交给其模型服务处理，不等于离线生成。

新建任务默认带上该工作流；已有讲次先按 **P 刷新 Prompt**，再运行，才能采用新规则。
刷新保留旧提示词快照和已有成果；只续作旧任务会继续使用其原快照。TUI 默认仍是 Web Chat，
需主动切换 Agent。该迁移是 Agent 会话内的编辑流程，尚不代表云端 Host 分阶段控制器或
独立语义审核已在本地默认启用；真实课程生成质量须另行检验。

最常见的使用方式：

1. 把课程 ZIP 拖入 `Asset ZIP`
2. 选择输出目录
3. 勾选想要的讲义类型
4. 决定是否参考 / 嵌入 PPT
5. 选择 Agent 交付方式
6. 运行；浏览器 ZIP 先确认预检展示的全部精确媒体域名，确认后才下载

Course Compiler 会自动完成课程材料处理，并在之后重复使用已经完成的转写和课件，不需要每次都重新跑 ASR。

## 3. 如果使用 Web Chat

TUI 默认可以选择：

```text
Web Chat (TXT/PDF)
```

完成后打开上传目录，把：

```text
Materials.txt
Slides.pdf   # 有 PPT 时
```

上传到你选择的网页 AI。

AI 返回 Markdown 后，可以再导入 Course Compiler，继续校验和生成离线 HTML 阅读版。

---

# 五种讲义模式

一次任务可以选择一种或多种成果，每种成果独立保存，不会互相覆盖。

| 模式 | 适合什么场景 |
| --- | --- |
| **Verbatim / 忠实逐字版** | 想保留老师原本讲述、例子、问答和课堂顺序，只做必要清理 |
| **Full / 完整课堂笔记** | 希望尽量保全课堂信息，同时明显去口语、去重复、改善段落 |
| **Deep / 深度图文讲义** | 希望沿课堂教学路线形成更适合学习的解释、推导和图文讲义 |
| **Summary / 速览讲义** | 没上课或要快速回顾时，用 5–10 分钟弄懂这节课讲了什么、哪些是有依据的重点、要做什么；可配核心课件图 |
| **Complete / 可展开完整讲义** | 默认阅读 Deep 主文，需要时展开课堂细节和有依据的逐字内容 |

其中：

- Verbatim / Full / Deep 默认保持课堂教学顺序；
- Summary 一般沿课堂顺序，内容跳跃时可以按主题合并；
- Complete 把不同细节层级放进一个可展开的成品中。

如果课程包含 PPT，可以单独控制：

- **Use PPT as source**：允许 Agent 阅读 PPT；
- **Embed PPT**：是否把真实课件图片放进最终讲义。

这两个选项不是同一件事。

---

# 输出结果

首次打开 TUI 时，使用 **DIRECTORIES** 确认或修改输入/输出根目录并保存。默认是系统实际桌面的 `Handouter/输入`、`Handouter/输出`；Windows 支持系统重定向的桌面。设置跨启动目录保存，仍可从任意位置拖入 ZIP。

课程名称从 ZIP 读取，也可修改。**Lecture Date** 填真实授课日期（`YYYY-MM-DD`）；新版智云导出脚本会从讲次 `sub_title` / `lectureDate` 等明确元数据提取并写入 `course_info.json.lectureDate`。缺失时仍明确标记日期待确认，不使用 `exportDate`、导出日或生成日代替。

新任务布局如下，同课同日不同讲次会加稳定 ID 摘要消歧：

```text
桌面/Handouter/输出/课程名称-09-17/
├── 成果/
│   ├── notes/               # Markdown / 离线 HTML
│   └── slides/images/       # Markdown 实际引用的图片
├── 资料与交接/<稳定讲次ID>/   # 音频、转写、课件、Prompt、状态及原稿历史
└── 中间产物_可清理/          # 可复用音频/ASR 缓存；通过命令预览后清理
```

Course Compiler 的结果采用版本化文件名，例如：

```text
课程名称_2026-09-17_full-001.md
课程名称_2026-09-17_full-002.md
课程名称_2026-09-17_complete-001.html
```

已有讲义不会因为再次运行而被直接覆盖。

分享 Markdown 时请一起复制整个“成果”文件夹；其中的图片相对位置保持不变。内嵌完成的 HTML 可单文件分享。网页导入、CLI Agent 或 HTML 导出后会发布成品副本；失败保留内部原稿，并在结果页显示发布状态。

CLI 可用 `handouter directories` 查看目录设置。默认输出目录可以单独保存：`handouter directories --output-dir <绝对路径>`；某门课可设置专用输出根目录：`handouter directories --course-title "课程名" --course-output-dir <绝对路径>`，用 `--clear-course-output` 恢复继承默认目录。还可以登记课程别名，例如 `handouter directories --course-title "计算机组成" --course-alias "计组"`、`handouter directories --course-title "高级数据结构" --course-alias "ADS"`。别名只用于识别同一门课，内部仍保存正式课程名。

课程文件夹名称可按课程设置为 `auto / always / never`：`handouter directories --course-title "计算机组成" --course-folder-name auto`。`auto` 是默认行为：如果实际输出根目录最后一级已经叫“计算机组成”或任一别名（例如 `/课程/计组`），讲次只创建 `09-17/`；否则创建 `计算机组成-09-17/`。`always` 永远带课程名，`never` 永远只用 `MM-DD`。因此可一次配置：`handouter directories --course-title "计算机组成" --course-alias "计组" --course-output-dir "/课程/计组" --course-folder-name auto`。

新任务的输出根目录优先级仍为“本次显式 `--output-dir` > 课程专用目录 > 默认输出目录”。TUI 的 `DIRECTORIES` 同时支持 Default Output、当前课程 Course Output、Aliases 和 Folder Name。新任务可传 `--lecture-date 2026-09-17`；旧脚本若必须维持原布局，可显式加 `--legacy-layout`。旧工作区不会自动迁移；打开外层课程文件夹即可续作。

Course Compiler 也可以直接使用其他网站下载到本地的材料，不要求先经过智云抓取：

```bash
handouter run --audio lecture.m4a --course-title "课程名" --lecture-id lecture-01
handouter run --media lecture.mp4 --course-title "课程名" --lecture-id lecture-01
handouter run --transcript transcript.txt --course-title "课程名" --lecture-id lecture-01
handouter run --asset portable-assets.zip
```

TUI 的 `Source File` 同样接受智云 ZIP、站点无关资产 ZIP、本地音频、视频和 UTF-8 TXT。多资产 ZIP 的 `handouter-input.json`、PPT 时间字段、扩展名和安全限制见 [本地资产输入协议](LOCAL_ASSET_INPUT.md)。

清理时先 `handouter cache-status <课程文件夹>`，再用 `cache-clean <课程文件夹> --entry <列出的条目>` 预览，确认后加 `--confirm`。尚未保存到正式资料的唯一文件拒绝清理。不要手动删除“资料与交接”。细节见 [输出组织](OUTPUT_ORGANIZATION.md)。

如果你只是修改讲义模式、PPT 策略或写作规则，Course Compiler 可以复用已经完成的课程材料，不重新执行整段音频转写。

---

# 已有讲义也可以继续使用

Course Compiler 不要求所有任务都从课程回放开始。

你可以提交已有的 Markdown / TXT 讲义，用于：

- 派生 Summary / Full / Deep 等版本；
- 在已有 PPT 的基础上补图；
- 导入网页 AI 返回的讲义；
- 生成离线 HTML 阅读版；
- 如需在 Typora 中查看 Course Compiler 的并排/半宽课件布局，可另导出 Typora 兼容阅读副本：`handouter export-typora <note.md>`。

如果只有讲义、没有原始课程转写，Course Compiler 会明确保留这个来源限制，不会把它当作真正的课堂逐字证据。

---

# 隐私与使用边界

Course Compiler 采用 local-first 设计，但不同交付方式的隐私边界不同：

- 音频提取、ASR、PPT 整理和工作区管理在本机完成；
- 默认不会自动把课程材料上传到其他服务；
- 使用 Web Chat 时，由你手动把生成的上传材料交给所选服务；
- 使用 Codex / Claude CLI 时，数据如何发送取决于你自己的 CLI 配置和服务商；
- 本地客户端不读取或托管你的 LLM API Key；
- 签名媒体 URL、Cookie 等私有信息不会写进公开讲义或普通 Agent 上传包。

请只处理自己有权访问的课程材料，不要绕过学校或课程平台的访问控制。

结构校验通过也不代表讲义内容在语义上绝对正确。公式、否定条件、老师更正、考核要求和 PPT 对应关系仍值得人工抽查。

---

# 命令行用户

新版本安装后使用 `course-compiler`；`handouter` 继续进入同一个 CLI main，没有额外弃用输出，不污染 JSON/stdout。下面说明中的包名、启动器和目录名继续有效，不会自动改名或搬迁。

例如：

```bash
course-compiler run \
  --audio "/path/to/lecture.m4a" \
  --output-dir output \
  --modes complete summary
```

查看所有命令：

```bash
course-compiler --help
handouter --help  # 兼容入口
```

检查安装环境：

```bash
course-compiler doctor
```

CLI 适合自动化和高级用户；普通用户建议直接使用 `start-handouter` 启动 TUI。
