# 内容生产中心设计说明

## 目的

内容生产中心是“市场资料—研发生产—经营反馈”之间的编排与学习层，不是文章编辑器，也不是 ERP。

| 系统 | 事实归属 | 内容中心如何交互 |
| --- | --- | --- |
| OpenClaw | 每日原始新闻 Markdown、HTML、截图 | 监听 Markdown；保留路径/哈希/解析结果，原文件仍留在 OpenClaw |
| 内容生产中心 | 选题、评分、任务契约、运行、发布观察、复盘与方法候选 | 编排与审计 |
| Obsidian | 内容项目、正文、封面、正文图、视频稿 | Agent 回传项目和产物路径；中心做存在性校验 |
| 自媒体平台 | 实际发布 | 人工最终操作 |
| OPC-ERP | 发布、指标、KR、PDCA | 只读同步内容表现；标记高价值内容进入复盘 |

## 状态模型

### Topic：选题判断

```text
new → scoring → scored → selected
                    ├→ expired → scored（重新打开）
                    └→ abandoned
```

Topic 永远不进入“已发布”。

### ProductionTask：创作执行

```text
queued → claimed → running → draft_ready → publish_ready
                         ├→ blocked → claimed
                         └→ failed
```

`publish_ready` 只是文章达到公众号发布稿标准，不是平台发布。

### PublicationObservation：发布反馈

```text
OPC-ERP 同步 → observed → matched → review_candidate
```

关联是可选的，因为选题标题、文章标题和发布标题可能不同。

## 任务契约

每个任务创建独立 Markdown 契约，包含：

- 任务 ID 和一次性 callback token；
- 选题与三维评分；
- OpenClaw 原始文件指针、来源、作者、日期、原文链接；
- 目标平台和交付清单；
- 必须从 `$content-creation-workflow` 开始的指令；
- 内容 SKILL 文件路径与 SHA-256 快照；
- Obsidian 项目/主稿/图片回传要求；
- 回传 API 和允许状态。

Agent 在回传 `draft_ready` 或 `publish_ready` 时，中心验证项目路径、主稿和资产是否存在；若配置了 vault 根目录，也检查路径没有逃逸。中心保存 Agent 对所用 Skill 的声明和自检，但不声称可读取或证明模型内部执行过程。

## 统一运营主线

首页是日常运营的唯一主入口，按“待处理 → 已评分 → 生产中 → 待发布 → 已回流 → 待复盘”展示现有对象的汇总和下一步动作。选题看板、生产任务和复盘页是同一条主线的详情页，不再为派生分析单独增加工作台或导航入口。

系统自动执行资料监听、AI 评分补齐、自主任务派发、发布结果同步和表现提示计算；人只保留选题确认、最终发布和方法升格三类判断。

## 学习闭环

1. OPC-ERP 内容表现只读同步成发布观察；
2. 人标记值得复盘的内容，形成 `ReviewCase`；
3. 人与 Codex 填写复盘笔记，提炼 `MethodCandidate`；
4. 人确认升格后，候选标为 `promoted`，指向待更新的 SKILL；
5. 实际修改 SKILL 后，下一次任务自动采集新哈希，形成可追溯版本。

## 安全与可靠性

- 所有历史评分、运行、回传和发布观察均追加保存；
- 同一选题同时只保留一个活动创作任务；
- 回传凭证错误、产物缺失或超出 vault 时拒绝 `draft_ready/publish_ready`；
- OPC-ERP 同步按 publication id 幂等更新；
- 原始资料缺失时创建来源异常，不覆盖解析内容。
