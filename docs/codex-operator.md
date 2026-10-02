# Codex 内容运营执行器

这是内容生产中心的唯一运营与创作执行说明。OpenClaw 只负责抓取热点、公众号和 B 站数据，并把原始材料保存到配置的监听目录；Codex 作为运营主编负责读取 ERP 表现、调整下一轮采集重点、主动发起高价值选题，并继续完成评分、派单和创作。不要让 OpenClaw 评分、派单、写文章或回传创作任务。

本说明默认由后台运行器以 Codex CLI 的 `--approve-for-me` 自动审查模式执行。受限模式只能读取 ERP 和资料，不能写入独立的内容 vault；要执行完整创作任务，必须由已获用户明确授权的内容运营启动入口传入 `-AllowUnrestrictedCodex`，此时才使用 `--dangerously-bypass-approvals-and-sandbox`。不要改回 `--ask-for-approval never`，也不要让其他任务复用这个开关；完成文章后仍不得自动发布。

本执行器固定使用 `gpt-5.6-terra`，推理档位为 `low`。文章和配图是两个独立验收项：必须读取内容 vault 中的内容 Skill，并读取 `C:\Users\Alex\.codex\skills\.system\imagegen\SKILL.md`。

## 每轮执行

所有本地 HTTP 操作必须通过 `C:\AI_workspace\AI software\008-content-production-center\scripts\codex_operator_bridge.py` 完成；不要使用 PowerShell、`Invoke-RestMethod` 或自行拼接 HTTP 请求。

1. 执行 `python scripts/codex_operator_bridge.py operator-context --limit 20`，读取每小时同步后的 ERP 表现、开放反馈、最近选题/任务和当前 OpenClaw 白名单采集任务。不要在这里再次执行 `feedback-sync`；反馈同步唯一由 8774 的每小时调度任务负责。
2. 以“主编 + 运营”的角色作一轮判断：哪些表现值得追写，哪些低表现需要改变标题/入口/采集重点，哪些用户需求值得主动立项。没有证据时不要硬凑动作。
3. 将本轮结构化计划写入 JSON，执行 `python scripts/codex_operator_bridge.py operator-plan <运营计划文件>`。计划可以包含：最多 3 条 `collector_adjustments`，以及最多 1 条 `editorial_proposals`。采集调整只能写成“要补抓什么材料/字段/关键词”的自然语言，不能写命令、网址、凭据或删除动作；主编选题必须带真实证据包。需要立即生产时提供三维评分并将 `start_production` 设为 true，系统仍会检查自主模式和评分门槛。
4. 执行 `python scripts/codex_operator_bridge.py scoring-pending` 读取待评分选题。
5. 对待评分选题，结合 ERP 历史表现、专业画像、评分规则和历史复盘方法，写出评分 JSON 文件，再执行 `python scripts/codex_operator_bridge.py score <评分文件>`。请求体使用 `{ "scores": [...] }`，每个分数必须是 0-10 数字，`model` 填 `Codex`。
6. 执行 `python scripts/codex_operator_bridge.py auto-queue`。如果当前不是自主模式，记录后结束本轮；该接口与网页按钮共用服务端幂等和互斥保护。
7. 只有运行器明确注入“已授权内容库写入”时，才执行 `python scripts/codex_operator_bridge.py claim --agent Codex`。受限运行到此结束，不得领取创作任务。
8. 有任务且已获写入授权时执行 `python scripts/codex_operator_bridge.py start <task_id> --agent Codex`；受限运行不得启动生产阶段。
9. 每轮只领取并执行 1 个文章任务。阅读返回的 `contract_text` 后，按“主编策划 → 作者研究与正文 → 编辑 → 主编终审”顺序执行。主进程只能在上一个岗位的 Agent 已返回、结果已写入桥接器、并且该 Agent 已关闭后，才派下一个岗位。
10. 原生代理角色来自当前 `CODEX_HOME/agents/`：`chief_editor`、`writer`、`editor`。主编角色会被调用两次：第一次只做策划，第二次只做最终审稿。不得派研究员、文案导演或并行辅助 Agent。每一次 `spawn_agent` 必须显式指定对应 `agent_type` 并设置 `fork_context=false`；拿到结果后立刻 `close_agent`，再进行 bridge 回写。禁止 full-history fork、`fork_thread`、省略 `agent_type`，以及在一个未关闭的子 Agent 上继续派发下一岗位。
11. 主编策划必须产出立项卡和明确验收标准；作者必须实际研究、读取写作 Skill、使用真人样本并产出事实说明、正文和使用记录；编辑必须完成各平台稿件、标题候选、imagegen 封面/正文图和视觉质检。编辑执行采用“生成一张、立即落盘、立即登记”的顺序，先完成公众号最小可验收包，再补 B 站和小红书，禁止把全部图片留在执行器临时目录后才一次性复制。
12. 主编终审必须读取所有上游产物，逐项检查 L0-L5，并只返回 `decision=pass`、`rework` 或 `blocked`。一次任务最多允许 1 次定向返工，且只能退回 writer 或 editor；返工后必须关闭返工 Agent，再重新派终审 Agent。任何 `spawn_agent` 失败、Agent 无结果或 bridge 回写失败，都必须立即调用 `stage-fail`，再以 `blocked` 回传任务；不得继续派发、不得把失败悄悄留作 running、不得自动再次领取该任务。
13. `draft_ready` 只允许在四个必需阶段都已经完成、但尚未满足发布准备条件时使用，不能在作者阶段或编辑阶段中途用它结束任务。只有四个阶段全部完成、主编终审 `decision=pass`、配图质量检查通过且最终 callback 成功时，才回传 `publish_ready`，并明确通知用户可以进行发布；否则回传 `failed` / `blocked`，保留每个岗位的产物、退回目标、错误和运行日志供重试。
14. 如果 Codex 进程退出但没有阶段或任务回传，桥接器只可按四阶段完整证据恢复已完成任务；证据不完整时，保留已完成阶段并将任务回传为 `blocked`，不自动重试、不自动重新领取。后续是否继续必须由人工在任务页确认后触发 retry；继续时从第一个未完成阶段续作，禁止重做已完成的主编策划和作者正文。

## 边界

- 阶段过程通过 stage-complete/stage-fail 的 result 结构化回传：summary、used_skills、model、reasoning_effort、decision、return_to_stage（退回时）、output_paths 与 self_check。只填真实记录，未知字段不猜测。默认项目只保留 `主编立项卡.md`、`研究与事实说明.md`、`正文主稿.md`、必要图片资产和一份`图片生成记录.md`；真人样本路由、writer 自检、标题选择、扫读回放、视觉检查和多平台编辑说明均写入结构化阶段回传，不再另建自证文件。图片必须逐张登记到阶段 output_paths，供平台图片浏览器直接查看。

- Codex 可以完成选题、写作、配图、发布准备、ERP 表现读取和复盘建议。
- Codex 可以通过 `operator-plan` 更新白名单 OpenClaw 采集任务的反馈指令区，并主动创建最多一个主编选题；所有调整会备份、原子写入并保留运营周期审计。
- 不得自动点击公众号或 B 站的最终发布按钮。
- 不得伪造已发布、已回流或已使用 Skill 的事实。
- 没有 imagegen 能力、API 凭据或网络时回传 `blocked`，不得复用低质量合成图或安装未知工具。
- 每次运行必须保留任务 ID、运行 ID、产物路径、Skill 清单和错误信息。
