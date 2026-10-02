# Agent 执行器接入说明

内容中心已经把任务生产、产物校验和 ERP 回流放在同一条主线上。执行器只消费生产任务，不需要自己扫描选题目录。

## 一次执行

1. 调用 `GET http://127.0.0.1:8774/api/scoring-pending?limit=5` 获取待评分选题和上下文。
2. 将评分结果提交到 `POST /api/topics/agent-score`。请求体使用 `{ "scores": [...] }`；接口会逐条返回字段错误，修正后再提交。不要用 GET 触发写操作。
3. 调用 `POST http://127.0.0.1:8774/api/production/tasks/auto-queue`，自主模式下按每日上限生成任务契约。GET 只会返回方法提示，不会派单。
4. 调用 `POST http://127.0.0.1:8774/api/production/tasks/claim-next`，请求体为 `{"agent_name":"Codex"}`。
5. 如果返回 `claimed=false`，本轮没有待执行任务，结束本轮。
6. 如果返回 `claimed=true`，先调用 `POST /api/production/tasks/{task_id}/start`，请求体为 `{"agent_name":"Codex"}`。
7. 读取返回的 `contract_text`。契约唯一要求使用 `$content-creation-workflow`，并按内容 vault 内已有 Skill 连续完成创作；公众号任务还必须读取并调用 `imagegen`。
8. 完成后调用 `POST /api/production/tasks/{task_id}/callback`，必须带回契约中的 `callback_token`、真实项目路径、主稿路径、产物清单、使用的 Skill 和自检结果。图片产物必须标记 `generator=imagegen` 及生成记录，未通过自动检查不能回传 `publish_ready`。

执行器如果在领取后失联，任务不会再永久显示“执行中”：下一次 `claim-next` 会自动将超过 30 分钟未回传的 `claimed/running` 任务标记为 `failed`，保留产物和错误原因，但绝不自动重跑。也可人工调用 `POST /api/production/tasks/recover-stale?max_age_minutes=30` 进行恢复；确认后再使用 retry。

## 状态边界

- `publish_ready` 只表示内容达到发布准备标准，不表示平台已经发布。
- 平台发布仍由人最终确认，发布事实由 OPC-ERP 回流。
- 遇到硬阻塞回传 `blocked`，遇到执行错误回传 `failed`，不要伪造产物路径或平台数据。
- 任务领取是排他动作；重复执行器会拿到“没有待执行任务”或明确的状态错误。

## 验证

任务完成后应在内容中心首页看到任务从“待生产”进入“生产中/待发布”，并在 Agent 活动日志看到领取、开始和回传记录。发布后由定时 ERP 同步把表现带回生产任务页的发布观察区域。
