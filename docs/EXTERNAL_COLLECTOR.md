# 安装外部采集插件

TXT、音视频和标准资源 ZIP 直接进入本地流程，不需要插件。浏览器导出 ZIP 由单独安装的插件解析、下载，TUI 只接收安全摘要和标准包。

## 智云插件

插件和配套浏览器脚本位于独立仓库：https://github.com/Silhouette-my/zhiyun-collector 。当前配套版本为 `0.3.0.dev2`；按该仓库说明安装对应 macOS / Windows 构建，并从可信发布渠道核对工具身份及可执行文件 SHA-256。插件不随本仓库 wheel、源码包或启动包分发。

```bash
course-compiler collector-configure \
  --executable /absolute/path/to/zhiyun-collector \
  --sha256 <trusted-release-sha256> \
  --tool-id handouter-zhiyun-collector \
  --exporter-script /absolute/path/to/zhiyun_exporter.user.js
course-compiler collector-inspect /path/to/browser.zip
```

Windows 将可执行路径换成绝对 `.exe` 路径，路径有空格时加引号；参数含义不变。浏览器脚本也可由插件的 `export-script --output <new-path>` 导出，配置中的脚本仅用于提示路径，不会执行它。更新插件后需重新核对摘要并配置。

TUI 中拖入浏览器 ZIP，运行时会展示预检返回的精确媒体域名。按 **Y** 确认后才允许下载；**N / Esc** 保留原 ZIP 并取消。CLI 则先预检，再显式重复传入全部确认域名：

```bash
course-compiler run --asset /path/to/browser.zip \
  --allowed-media-host media.example.edu --agent manual
```

## 开发者接口与隐私边界

`handouter.external_collector` 通过参数数组启动用户明确配置、摘要固定的外部工具（`<executable> protocol`）。不扫描 EXE，不执行 ZIP 内程序，不拼接 shell。每次进程处理一条有界 NDJSON 请求，`protocol_version: 1`：

- `identity`：工具身份、版本、协议和 `inspect` / `convert` 能力。
- `inspect`：输入 `source_path`；返回 `inspection`，仅含工具身份、课程标题、授课日期、`media_hosts`、`source_sha256`，可选安全的 `source_id` / `source_fingerprint`。不返回媒体 URL、Cookie 或脚本。
- `convert`：输入原路径、已确认源摘要、全部精确域名和新的 `output_path`；返回 `result.output_path`。进度仅允许 preparing / downloading / packaging / publishing，最多 1000 帧，每帧最多 16 KiB；失败消息由宿主脱敏。

确认绑定工具配置和源 ZIP，变更后必须重新预检。插件负责 HTTPS、固定连接 IP、拒绝跳转和域名检查，宿主不绕过。生成包须通过完整离线标准包预检后才能流式解包（总量 8 GiB，单媒体 7 GiB，仍受本地预算限制），之后沿用转写、规则交接和用户 Agent 流程。插件超时或任务取消会清理子进程树；Windows 使用 Job Object。

原始浏览器 ZIP 只交给本机插件；宿主不读取 `private/source.json`，也不把原 ZIP、签名 URL、Cookie 放进工作区、日志、Agent 交接或成果。原 ZIP 不删除。站点协议与浏览器脚本测试由插件仓库维护；本仓库使用合成外部工具验证宿主契约。
