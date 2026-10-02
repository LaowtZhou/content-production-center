# 内容生产中心

内容生产中心负责把每日资料变成可追踪的创作任务，并把发布后的有效经验沉淀为 SKILL 候选。

```text
OpenClaw 原始 Markdown
  → 选题池 / 三维评分
  → 创作任务契约（Skill 版本、回传 token）
  → Codex 按 Obsidian 内容工作流生产
  → Obsidian 项目与发布稿
  → 人工发布
  → OPC-ERP 发布数据与 PDCA（事实源）
  → Codex 运营判断（调整采集 / 主编主动选题）
  → 发布观察中的自动表现提示
  → 复盘案例 / 再分发任务 / 方法候选 / 待更新 SKILL
```

生产任务由 `/api/production/tasks/claim-next` 提供统一领取入口。执行器领取后使用返回的任务契约，调用现有 `start` 和 `callback` 接口更新运行状态；任务契约、Skill 版本和 Obsidian 产物路径因此保持在同一条生产主线上。

## 边界

- 内容中心不保存 OpenClaw 原始新闻副本，不直接发布，不管理 KR。
- Obsidian 管文章、图片、视频稿和项目目录。
- OPC-ERP 管真实发布结果、指标、经营目标和 PDCA。
- 运营主线中的自动表现提示只读取 OPC-ERP 的发布观察，生成带证据和规则版本的派生信号，不复制或覆盖 ERP 指标。

## 配置顺序

在“系统设置”中依次填入：

1. OpenClaw 输出目录；
2. Obsidian 内容 vault 根目录；
3. 内容 SKILL 目录；
4. 任务契约目录（留空则使用 `data/task-contracts`）；
5. OPC-ERP API 地址（只读同步发布表现）。

选择模式：

执行器分工：

- OpenClaw 只负责热点、公众号和 B 站数据采集，以及原始材料落盘；
- Codex 负责 ERP 表现判断、白名单采集任务调整、主编主动选题、选题评分、任务领取、Skill 执行、文章生产、发布准备、任务回传和复盘；
- 内容中心负责状态、契约、审计和 ERP 数据回流，不直接写文章；
- 平台最终发布仍由人确认。

- **人工选择**：你确认已评分选题后创建任务；
 - **自主生产**：内容中心每 30 分钟只唤醒一次 Codex 运营器；Codex 按最高评分和每日上限生成 1–3 个任务契约并自动领取，只有主编终审通过后进入待人工发布，不会自动点击平台发布。

打开首页即可查看从资料进入到发布回流的运营主线。同步 OPC-ERP 后，表现提示显示在“生产任务”页的发布观察区域；反馈计算每小时尝试同步一次。ERP 未配置或接口不可达时自动降级，历史内容中心仍可使用。内容中心脚本每 30 分钟只负责唤醒 Codex 运营器；Codex 通过统一运营计划、评分、领取和阶段回传接口消费任务契约，脚本不评分、不派单、不写稿。运营计划会更新 OpenClaw 白名单采集任务的反馈指令区，并最多主动创建一个带证据的主编选题，所有变更可在运营周期审计中追溯。

Codex 内容运营执行器固定使用 `gpt-5.6-luna`，推理档位为 `high`。公众号发布准备必须使用 imagegen 生成图片并通过发布前自动检查；定时 CLI 运行需要运行账户配置 `OPENAI_API_KEY`，否则任务会阻塞，不会用本地脚本伪造图片。

## 运行

**推荐方式**（双击即可，启动后终端自动消失，服务在后台运行）：

- 启动：双击 `start.bat`（自动后台启动并打开浏览器）
- 停止：双击 `stop.bat`

启动完成后可打开 [系统状态](http://127.0.0.1:8774/status) 检查数据库、OpenClaw 资料监听和定时任务。若启动失败，先查看 `data/logs/launcher.log`；服务运行细节在 `data/logs/server.log`。启动器不会自动结束占用 8774 端口的其他程序，避免误杀。

**手动方式**（开发调试用）：

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8774 --reload
```

服务端口为 **8774**（所有 scripts 脚本也访问此端口）。

运行测试：

```bash
python -m unittest discover -s tests -v
```
