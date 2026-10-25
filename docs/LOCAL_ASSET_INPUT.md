# 本地与站点无关资产输入协议

Course Compiler 的普通入口不要求材料来自智云。用户可以直接传入本地音频、视频、UTF-8 转写，或把多种材料封装为站点无关的资产 ZIP。所有输入都进入同一套本地 ASR、工作区、Prompt、校验和成果发布流程；智云导出 ZIP 继续兼容。

## 1. 单文件入口

四种输入互斥，只选择一种：

```bash
# 本地音频：直接运行 SenseVoice
handouter run --audio lecture.m4a --course-title "课程名" --lecture-id lecture-01

# 本地视频：先用 ffmpeg 提取音频，再运行 SenseVoice
handouter run --media lecture.mp4 --course-title "课程名" --lecture-id lecture-01

# 已有转写：不运行 ASR
handouter run --transcript transcript.txt --transcript-origin provided_transcript \
  --course-title "课程名" --lecture-id lecture-01

# 智云 ZIP 或下述站点无关资产 ZIP
handouter run --asset lecture.zip
```

本地文件未指定标题时使用文件名；未指定 lecture ID 时使用内容 SHA-256 的稳定短 ID `local-<12 hex>`。授课日期不会从文件时间、下载时间或导出时间猜测，只接受显式 `--lecture-date YYYY-MM-DD` 或资产清单中的 `lecture.lecture_date`。

可选 PPT 参数只用于松散的音频、媒体或转写输入：

```text
--slides-dir DIR [--slides-meta FILE]
--slides-zip FILE
```

`--slides-dir` 与 `--slides-zip` 互斥。已有转写可用 `--audio-evidence FILE` 保存对应音频证据；音频不会进入 Agent 上传包。默认 `transcript-origin=provided_transcript`，只有明确知道 TXT 来自 ASR 时才选 `asr_text`。

支持的单文件扩展名：

- 音频：`.aac .flac .m4a .mp3 .ogg .opus .wav`
- 视频/媒体：`.avi .m2ts .m4v .mkv .mov .mp4 .mpeg .mpg .mts .ts .webm`，`--media` 也接受上述音频
- 转写：UTF-8 `.txt`
- 图片：`.jpg .jpeg .png .webp`

扩展名只是入口白名单。音频/媒体是否可用仍由实际解码、ASR 和现有运行环境检查决定。

## 2. 多资产 ZIP

ZIP 根目录必须有 `handouter-input.json`。所有路径都是 ZIP 内相对 POSIX 路径，不能使用绝对路径、`..`、反斜杠、符号链接或大小写/Unicode 重名。

推荐布局：

```text
lecture.zip
├── handouter-input.json
├── audio/
│   └── lecture.m4a
├── transcript/
│   ├── transcript.txt
│   ├── segments.json
│   └── raw.json
└── slides/
    ├── slides_meta.json
    └── images/
        ├── slide_001.png
        └── slide_002.jpg
```

完整 schema 1 示例：

```json
{
  "schema_version": 1,
  "lecture": {
    "id": "course-lecture-01",
    "course_title": "课程名称",
    "lecture_date": "2026-09-19"
  },
  "audio": {
    "path": "audio/lecture.m4a"
  },
  "transcript": {
    "path": "transcript/transcript.txt",
    "origin": "provided_transcript",
    "segments_path": "transcript/segments.json",
    "raw_asr_path": "transcript/raw.json"
  },
  "slides": {
    "images_dir": "slides/images",
    "metadata_path": "slides/slides_meta.json"
  }
}
```

字段规则：

| 字段 | 必需 | 含义 |
| --- | --- | --- |
| `schema_version` | 是 | 当前固定为整数 `1` |
| `lecture.id` | 否 | 稳定讲次 ID；仅字母、数字、`_`、`-` |
| `lecture.course_title` | 否 | 显示标题；CLI 参数可覆盖 |
| `lecture.lecture_date` | 否 | 真实授课日期 `YYYY-MM-DD`；未知省略 |
| `audio.path` | 条件 | 已有音频。若同时有 transcript，只保存为证据；否则运行本地 ASR |
| `media.path` | 条件 | 需要 ffmpeg 提取音频的音频/视频；不能与 audio 或 transcript 同时出现 |
| `transcript.path` | 条件 | UTF-8 TXT；存在时不重复运行 ASR |
| `transcript.origin` | 否 | `provided_transcript`（默认）或 `asr_text` |
| `transcript.segments_path` | 否 | 结构化 segments JSON；必须同时有 transcript |
| `transcript.raw_asr_path` | 否 | 原始 ASR JSON；必须同时有 transcript |
| `slides.images_dir` | 否 | 只读取该目录第一层的受支持图片 |
| `slides.metadata_path` | 否 | PPT 事件 JSON；必须同时有 images_dir |

至少提供 `audio`、`media` 或 `transcript` 之一。ZIP 中未被清单引用的文件不会进入工作区或交接包。

## 3. segments 与 PPT 时间

segments 继续使用 Course Compiler 的整数毫秒契约：

```json
[
  {
    "id": "seg-0001",
    "start_ms": 12000,
    "end_ms": 18000,
    "text": "这里是转写文本。",
    "timestamp_kind": "sentence_or_vad",
    "speaker": null
  }
]
```

没有可靠时间时 `start_ms/end_ms` 保持 `null`，不能按字数猜时间。

`slides_meta.json` 是对象列表，`filename` 指向 `images_dir` 第一层的图片。时间优先使用整数毫秒 `start_ms/end_ms`；也兼容现有 `seconds` 或 `HH:MM:SS(.mmm)` 字段，但同一事件的多个时间字段必须一致。

```json
[
  {
    "filename": "slide_001.png",
    "source_index": 1,
    "start_ms": 12000,
    "end_ms": 45000
  }
]
```

图片身份与展示事件分开：同一图片可以在多个事件中重复出现，不能因为回翻旧页而删除事件。

## 4. 处理优先级

```text
有 transcript → 直接准备工作区，不运行 ASR
无 transcript、有 audio → 本地 SenseVoice ASR
只有 media → ffmpeg 提取音频，再运行本地 SenseVoice ASR
```

包内 transcript 与 audio 同时存在时，audio 只作为证据保留。Course Compiler 不会用音频自动证明转写准确，也不会把用户提供的转写标成官方或已核音频内容。

## 5. 安全边界

- ZIP 最多 10,000 个成员、总未压缩大小 512 MiB；单张图片最多 32 MiB，清单/元数据最多 8 MiB。
- 拒绝路径穿越、绝对路径、空路径段、符号链接、加密成员和跨平台歧义文件名。
- 图片必须有真实 PNG/JPEG/WebP 文件头；JSON 与 TXT 必须是有效 UTF-8。
- 本地资产协议不接受 URL、Cookie、token、签名媒体地址或 `private/source.json`。需要联网媒体时继续使用明确授权的高级 `build-media` 入口。
- 输入只读，目标工作区排他创建；不会覆盖原始音频、转写、PPT、旧工作区或既有讲义。
- Agent handoff 仍排除音频、raw ASR、状态文件和私有材料；只提供经过筛选的转写、PPT 与规则快照。

## 6. 兼容与验收边界

旧的 `handouter run --asset zhiyun.zip`、`build-audio`、`build-media` 和 `prepare` 保持兼容。站点无关 ZIP 使用 `source_type=local_asset_package`；松散音频、媒体、转写分别保持现有本地来源语义。

合成测试只能证明格式解析、路径安全、分派、工作区复制和现有回归没有破坏。真实网站下载所得媒体的编码、真实长课 ASR、Windows 拖入路径、TUI 真人操作和语义质量仍须分别验收。
