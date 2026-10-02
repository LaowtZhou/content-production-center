# AGENTS.md - 内容生产中心

## 系统定位

内容生产中心是内容创作的**编排与学习层**：管理 OpenClaw 原始资料指针、选题池、评分、创作任务、Agent 运行回传、发布观察、复盘案例与方法候选。

- OpenClaw 是原始新闻事实源；原始 Markdown 留在 OpenClaw 目录。
- Obsidian 是内容项目、文章、图片和视频稿事实源。
- OPC-ERP 是发布结果、指标、KR 与 PDCA 事实源。
- 内容中心不发布，也不把“发布稿完成”伪装为“已发布”。

## 核心对象与状态

1. **Topic**：`new → scoring → scored → selected`，也可 `expired / abandoned`。只表示选题决策。
2. **ProductionTask**：`queued → claimed → running → draft_ready / publish_ready`，也可 `blocked / failed`。只表示创作过程。
3. **TaskRun**：每次领取、执行、重试、回传的不可覆盖事实。
4. **PublicationObservation**：从 OPC-ERP 只读同步的 `observed → matched → review_candidate` 发布与指标观察；可选关联任务，不要求标题一一对应。
5. **ReviewCase / MethodCandidate**：人机复盘后沉淀的候选方法；`promoted` 只表示待写入指定 SKILL，不直接改动内容 vault 的 SKILL.md。
6. **SkillVersion**：任务创建时读取的 SKILL 文件哈希快照，是任务声明依据，不是对 AI 思考过程的证明。

## 数据规则

- 原始资料不覆盖：保存来源路径、哈希、解析结果和可访问性异常；不复制 OpenClaw 原始 MD。
- 评分不覆盖：每次评分独立保存。
- 运行和回传不覆盖：每次执行写 TaskRun；任务只保存当前派生状态。
- 发布不猜测：仅 OPC-ERP 同步可形成发布观察；人可做可追溯的关联。
- 关键写入都必须有状态校验、原因与事件记录。

## 创作任务契约

任务契约必须包含：任务 ID、选题证据、原始资料指针、目标交付、`content-creation-workflow` 入口、所需 SKILL 清单和哈希、回传 URL/token、Obsidian 产物要求。

软件只能校验任务声明、回传和产物存在性；不能声称能够证明 Agent 在内部真正“使用了”某个 Skill。
