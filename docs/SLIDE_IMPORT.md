# PPT ZIP 导入与浏览器包转换

更新：2026-10-25，适用于 Handouter 0.2.0。PPT 导入位于 `src/handouter/importers.py`；浏览器包转换经 `external_collector.py` 调用独立插件。已有转写可用 `handouter prepare --slides-zip`；浏览器课程包先按插件指南配置并确认域名。

## 推荐使用

### 只导入 PPT 到交接工作区

```bash
PYTHONPATH=src .venv/bin/python -m handouter prepare \
  --lecture-id lecture-001 \
  --course-title "课程名称" \
  --transcript /path/to/transcript.txt \
  --slides-zip /path/to/资产包.zip
```

### 浏览器课程 ZIP

浏览器 ZIP 由[独立采集插件](EXTERNAL_COLLECTOR.md)预检，用户确认域名后下载并转换；TUI 不读取私有 URL。站点格式与脚本由插件仓库维护。`prepare --slides-zip` 仍可只导入图片和安全时间信息，不进行联网采集。

## 时间与展示事件

时间统一为**整数毫秒**：

| 字段 | 含义 |
| --- | --- |
| `start_ms` | 展示开始；未知为 null；真实零秒为 0 |
| `end_ms` | importer 不猜末尾，通常为 null；后续 alignment 可用下一次事件开始推导临时区间 |
| `timestamp_kind` | `metadata` / `filename` / `unknown` |
| `occurrence_id` | 展示事件 ID，而非唯一 PPT 图片 ID |

老师回翻同一张 PPT 时，同一图片可以对应多个 occurrence；不得为了“页码递增”删除回翻事件。完全没有元数据的旧 ZIP 才会兼容文件名 `slide__00-02-05.jpg`；普通文件名没有时间则保持 unknown。

## 图片与缺失

支持 JPEG、PNG、WebP，以**实际文件头**确定输出后缀，而不是盲信 ZIP 内扩展名。当前仅做文件头/体积级验证，不代表完整畸形图片安全解码审计。

元数据引用缺图时保留事件并标 `missing_image`；未被元数据引用的图片也保留为 `unreferenced_image`，不赋予虚构时间。

## ZIP 安全边界

只导入 PPT 的 importer（标准包经插件完整预检的流式入口见外部采集器指南）：

- 不使用 `extractall()`；
- 拒绝绝对路径、`..`、反斜线/跨平台歧义路径；
- 拒绝符号链接、加密成员、大小写/Unicode 重名；
- 最多 10,000 成员；
- 总未压缩体积 ≤ 512 MiB；
- 单图 ≤ 32 MiB；
- PPT 元数据 ≤ 8 MiB；
- 新目录排他写入；可捕获失败清理本次新产物。

这些保护不等于抵御恶意本地并发写入或强制断电的事务系统。

## 隐私边界

整个浏览器资产 ZIP 应视为私有文件，因为 `private/source.json` 可能含短期签名媒体 URL。Handouter 的公共工作区只保存：

- PPT 图片；
- 安全时间/缺失诊断；
- 本地抽取音频；
- ASR 结果；
- handoff。

不会把 `private/source.json`、完整 URL、Cookie、API Key 复制进 manifest/Prompt/sources。真实站点登录、下载与整课生成需另行验收。

## 与 ASR 的关联

当工作区同时有 `transcript/segments.json` 和 PPT 时间事件时，生成 `slides/alignment.json`。该文件只表示时间区间相交：

- 跨页句子可以关联多张 PPT；
- 未知 ASR/PPT 时间保持 unavailable；
- 不把时间相交表述成语义“精准对应”。

## 验证状态

PPT 安全路径、图片和时间元数据由 `tests/test_slide_import.py` 覆盖；外部工具确认、配置绑定、标准包检查与取消由 `tests/test_external_collection.py` 覆盖。实际平台和真实课程边界见[当前状态](../PROJECT_STATUS.md)。
