# 本机 Handoff 编辑闭环

## 本地 Workflow

当前共享规则与本地工作流版本均为 `2026-10-14.2`。下文的流程能力是会话内协作和本机工具，不是新增模型调度器；实际执行的范围仍以本次记录为准。

已核实代码路径：`tui._result_rows / CLI run` → `service.prepare_lecture / refresh_handoff` → `handoff.create_handoff` → `skill_plan.plan_modules/materialize_skill_plan` → `sources.json.skill.modules` 唯一快照 → manual/Web bundle 或 `agents.runner`（Codex/Claude用户工具）→ `validation.validate_note_output` → `tasks` / `delivery` / `html_export`。`--agent none`只做准备，外部交互式Agent编写后由import/continue接回发布。

现有的 `local_agent_workflow` 是会话内编辑步骤，不是云端 `agent_workflow_v2/1`，也不是默认开启 staged authoring/composer。已有 `authoring` v1/v2负责单元草稿/来源/进度，`authoring_stages`与双向 `authoring_audit` 仍为独立显式路径。`revise`保留旧稿，以新任务/expected_outputs形成候选；本轮不另造自动改稿器。

模型真实身份、外部交互平台用量、ASR实际后端、图片是否看过和最终HTML实际浏览，没有本轮收据，均不据Agent自述认证。新配置不修改历史任务快照，须显式刷新才采用。

## 编辑与复核能力

共享规则保留课程独有信息、例题身份/终点、情态/归属/让步；纠正Complete将所有例题强制常显的冲突。局部执行步骤引用共享模块，不重复整套课程规则。

新增可选 `editorial_review` 仅给普通内置本地Handoff，不注入自定义Skill、仅补图、staged/composer或云端协议。复核JSON记录可由CLI Agent返回或手工显式导入，Host只校验当前任务、来源、稿件摘要、真实引用及字段；结论显示为Agent声明，不增加发布通过权限。有效发现显示`revision_required`，不丢掉安全成品；无报告保持未评估。历史报告不可覆写；改稿后旧报告失效。声明的完成只针对所列范围，不能代表全文认证；后续报告不构成对旧问题的独立裁定。

`editorial-context`只读取本任务的来源、选定稿件与图片事件导航；支持有界Unicode分页、精确回源和逐字块生成，不执行源文本、不上传、不计为已做语义/视觉检查。已有composer继续负责其显式路径；普通Handoff不要求云端标记。

## 分项完成信息

结构/资源与引文定位由Host报告；来源保全、成品断言、技术核验、HTML阅读作为分开的Agent声明。HTML导出成功不代表阅读检查；未提供对应HTML摘要时不能记录已读HTML。TUI与CLI保留原任务/发布状态，附加质量视图，不将静态全绿升级为总质量通过。

## 范围

这是独立 TUI 的用户自有 Agent 工作流。保留现有默认档位和 authoring 版本，不新增默认模型调用。服务器预算、鉴权、ACK/TTL 等不属于本仓库。本地合成验证不证明真实讲义质量；验证边界见 ../PROJECT_STATUS.md。
