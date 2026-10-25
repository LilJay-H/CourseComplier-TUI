# 本地构建与验证

`python scripts/check_release.py --profile full` 运行明确的本地测试模块。缺依赖、测试收集为空或意外 skip 均失败。平台跳过和 core 中的慢测试跳过会明确记录，full 不跳过慢测试。完整保留回归可加 `--exhaustive`；没有 cloud profile。

`python -c "import handouter_build as b; print(b.build_wheel('dist')); print(b.build_sdist('dist'))"` 构建 wheel/sdist，不需要安装构建 backend。两者以 release-files.json 为白名单。源码包包括规则、安装器、文档、测试和构建入口，可脱离两个 checkout 继续开发和重建。

验证安装时创建源码目录外的新 venv，以绝对路径安装 wheel 的 `[html]` extra，然后在临时空目录执行新环境的 `course-compiler --help`、`handouter --help` 和：

```text
<新环境的 python> -X utf8 -I <仓库>/scripts/check_release.py --installed-smoke --profile full
<新环境的 python> -X utf8 -I <仓库>/scripts/smoke_pipeline.py --output <新的空目录>
```

第一个检查安装位置、规则、PDF、HTML 数学与图片。第二个创建可审阅合成资产 ZIP，经真实 CLI 导入、交接、模拟本地 Agent、校验与 Markdown/HTML 交付。它不修改 sys.path，不调用真实模型；模拟 Agent 的进程边界有明示。输出包含 summary.json。

可选用户 ZIP 沿用 `python scripts/build_release.py --target windows-x64`，会下载并校验既有清单中的 FFmpeg；`--without-bundled-ffmpeg` 仅产生不带媒体工具的源码启动包，不能描述为离线完整发行包。跨平台打包成功不是平台运行通过。默认 wheel/sdist 不带第三方二进制和模型权重。

本仓库无远端发布自动化；公开许可和 GitHub 发布须另行确认。
