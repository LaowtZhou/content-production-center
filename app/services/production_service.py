"""内容生产编排服务。

这里管理“选题被选中之后”的任务和运行事实；文章项目与发布事实仍分别属于
Obsidian 与 OPC-ERP。所有状态变更都留下 topic_events / task_runs 审计痕迹。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from app.config import BASE_DIR, get_db, get_int_setting, get_setting
from app.runtime import APP_BASE_URL
from app.services.publish_validator import validate_publish_bundle


REQUIRED_SKILLS = [
    "content-creation-workflow",
    "content-topic-editor",
    "content-project-intake",
    "content-evidence-director",
    "laowantong-writer",
    "laowantong-title",
    "content-publish-prepare",
    "content-visual-design",
    "content-cover-producer",
]
WORKFLOW_VERSION = "chief-editor-v1"
PRODUCTION_STAGES = [
    {"key": "chief_editor_plan", "role": "主编 Agent", "label": "主编策划", "required": True},
    {"key": "writer", "role": "作者 Agent", "label": "研究与正文", "required": True},
    {"key": "editor", "role": "编辑 Agent", "label": "多平台编辑与视觉", "required": True},
    {"key": "chief_editor_review", "role": "主编 Agent", "label": "主编终审", "required": True},
]
STAGE_PREREQUISITES = {
    "writer": "chief_editor_plan",
    "editor": "writer",
    "chief_editor_review": "editor",
}
ACTIVE_TASK_STATUSES = ("queued", "claimed", "running", "blocked", "draft_ready", "publish_ready")
DEFAULT_EXECUTION_LEASE_MINUTES = 30
CALLBACK_STATUSES = ("blocked", "draft_ready", "publish_ready", "failed")
_AUTO_QUEUE_LOCK = threading.Lock()


def now_iso() -> str:
    return datetime.now().isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _record_topic_event(conn, topic_id: int, event_type: str, metadata: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO topic_events (topic_id, event_type, metadata) VALUES (?, ?, ?)",
        (topic_id, event_type, _json(metadata)),
    )


def _record_activity(conn, agent_name: str, activity_type: str, target_type: str, target_id: str, summary: str, metadata: dict[str, Any] | None = None) -> None:
    conn.execute(
        """INSERT INTO agent_activities
        (agent_name, activity_type, target_type, target_id, summary, metadata)
        VALUES (?, ?, ?, ?, ?, ?)""",
        (agent_name, activity_type, target_type, str(target_id), summary, _json(metadata or {})),
    )


def _stage_definition(stage_key: str) -> dict[str, Any]:
    for stage in PRODUCTION_STAGES:
        if stage["key"] == stage_key:
            return stage
    raise ValueError(f"未知生产阶段：{stage_key}")


def _ensure_stage_runs(conn, task_id: str) -> None:
    """为新任务创建可重试的阶段骨架；重复调用保持幂等。"""
    timestamp = now_iso()
    for stage in PRODUCTION_STAGES:
        conn.execute(
            """INSERT OR IGNORE INTO production_stage_runs
            (id, task_id, stage_key, role_name, status, attempt, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'queued', 1, ?, ?)""",
            (str(uuid.uuid4()), task_id, stage["key"], stage["role"], timestamp, timestamp),
        )


def _stage_rows(conn, task_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT * FROM production_stage_runs
        WHERE task_id = ? ORDER BY attempt ASC, created_at ASC""",
        (task_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _stage_summary(conn, task_id: str) -> dict[str, Any]:
    rows = _stage_rows(conn, task_id)
    if not rows:
        return {"workflow_version": "legacy", "stages": [], "completed": 0, "required": 0}
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        latest[row["stage_key"]] = row
    return {
        "workflow_version": WORKFLOW_VERSION,
        "stages": [
            {**definition, "status": latest.get(definition["key"], {}).get("status", "not_created"),
             "attempt": latest.get(definition["key"], {}).get("attempt", 0),
             "error_detail": latest.get(definition["key"], {}).get("error_detail"),
             "output_paths": json.loads(latest.get(definition["key"], {}).get("output_paths_json", "[]"))
             if latest.get(definition["key"]) else []}
            for definition in PRODUCTION_STAGES
        ],
        "completed": sum(1 for definition in PRODUCTION_STAGES if latest.get(definition["key"], {}).get("status") == "completed"),
        "required": sum(1 for definition in PRODUCTION_STAGES if definition["required"]),
    }


def _read_skill_manifest(conn) -> list[dict[str, Any]]:
    """对任务需要的 Skill 创建可重复核验的文件哈希清单。"""
    root_value = get_setting("content_skills_dir", "").strip()
    root = Path(root_value) if root_value else None
    manifest: list[dict[str, Any]] = []
    for skill_name in REQUIRED_SKILLS:
        skill_file = root / skill_name / "SKILL.md" if root else None
        if not skill_file or not skill_file.is_file():
            manifest.append({"skill_name": skill_name, "available": False, "path": str(skill_file or "")})
            continue
        digest = hashlib.sha256(skill_file.read_bytes()).hexdigest()
        version = conn.execute(
            "SELECT id FROM skill_versions WHERE skill_path = ? AND content_hash = ?",
            (str(skill_file), digest),
        ).fetchone()
        version_id = version["id"] if version else str(uuid.uuid4())
        if not version:
            conn.execute(
                """INSERT INTO skill_versions
                (id, skill_name, skill_path, content_hash, status, discovered_at, notes)
                VALUES (?, ?, ?, ?, 'active', ?, '任务创建时自动发现')""",
                (version_id, skill_name, str(skill_file), digest, now_iso()),
            )
        manifest.append({
            "skill_name": skill_name,
            "available": True,
            "path": str(skill_file),
            "content_hash": digest,
            "version_id": version_id,
        })
    return manifest


def _verify_source_reference(conn, topic: Any) -> dict[str, Any]:
    """校验外部原始 Markdown 仍存在，缺失时建异常而不是复制一份“原始文件”。"""
    source_ref = (topic["source_ref"] or "").strip()
    source_id = topic["source_id"]
    if not source_id or not source_ref or not os.path.isabs(source_ref):
        return {"status": "not_applicable"}
    path = Path(source_ref)
    checked_at = now_iso()
    if not path.is_file():
        conn.execute(
            "UPDATE topic_sources SET availability_status = 'missing', last_verified_at = ? WHERE id = ?",
            (checked_at, source_id),
        )
        existing = conn.execute(
            "SELECT id FROM source_issues WHERE source_id = ? AND issue_type = 'source_missing' AND status = 'open'",
            (source_id,),
        ).fetchone()
        if not existing:
            conn.execute(
                """INSERT INTO source_issues
                (id, source_id, issue_type, severity, status, detail, created_at)
                VALUES (?, ?, 'source_missing', 'warning', 'open', ?, ?)""",
                (str(uuid.uuid4()), source_id, f"原始文件不可访问: {source_ref}", checked_at),
            )
        return {"status": "missing", "path": source_ref}

    stat = path.stat()
    conn.execute(
        """UPDATE topic_sources
        SET availability_status = 'available', source_format = ?, source_size_bytes = ?, source_mtime = ?, last_verified_at = ?
        WHERE id = ?""",
        (path.suffix.lower().lstrip("."), stat.st_size, datetime.fromtimestamp(stat.st_mtime).isoformat(), checked_at, source_id),
    )
    return {"status": "available", "path": source_ref, "size_bytes": stat.st_size}


def _contract_directory() -> Path:
    configured = get_setting("task_contract_dir", "").strip()
    return Path(configured) if configured else BASE_DIR / "data" / "task-contracts"


def _recent_learning_methods(conn, limit: int = 8) -> str:
    rows = conn.execute(
        "SELECT title, method_text, status FROM method_candidates "
        "WHERE status IN ('candidate', 'promoted') ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    if not rows:
        return "暂无已沉淀方法候选。"
    return "\n".join(f"- {row['title']}（{row['status']}）：{row['method_text'][:260]}" for row in rows)


def _write_contract(conn, task_id: str) -> str:
    task = conn.execute("SELECT * FROM production_tasks WHERE id = ?", (task_id,)).fetchone()
    topic = conn.execute("SELECT * FROM topics WHERE id = ?", (task["topic_id"],)).fetchone()
    score = conn.execute(
        "SELECT * FROM topic_scores WHERE topic_id = ? ORDER BY scored_at DESC, id DESC LIMIT 1",
        (topic["id"],),
    ).fetchone()
    manifest = json.loads(task["skill_manifest_json"] or "[]")
    deliverables = json.loads(task["requested_deliverables"] or "[]")
    is_wechat_only = task["requested_platform"] == "wechat" and set(deliverables or ["wechat_publish_ready"]) <= {"wechat_publish_ready"}
    editor_scope = (
        "完成公众号正文的标题兑现、扫读排版、封面/正文图、图片生成记录和视觉检查。不得生成 B 站、小红书或其他平台稿。"
        if is_wechat_only
        else "完成请求交付中列明的平台版本、标题兑现、排版与视觉检查；不得擅自增加未请求的平台稿。"
    )
    source_check = _verify_source_reference(conn, topic)
    learning_methods = _recent_learning_methods(conn)
    base_url = get_setting("production_center_base_url", APP_BASE_URL).rstrip("/")
    vault_dir = get_setting("content_vault_dir", "").strip() or "未配置"
    skills_dir = get_setting("content_skills_dir", "").strip() or "未配置"
    stage_snapshot = _stage_summary(conn, task_id)
    stage_lines = "\n".join(
        f"- `{stage['key']}`：{stage['status']}（第 {stage['attempt']} 次；{stage['label']}）"
        for stage in stage_snapshot["stages"]
    )
    score_lines = "未评分"
    if score:
        score_lines = (
            f"- 流量潜力：{score['traffic_score']}/10 — {score['traffic_reasoning'] or '无'}\n"
            f"- 观点匹配：{score['opinion_score']}/10 — {score['opinion_reasoning'] or '无'}\n"
            f"- 新颖度：{score['novelty_score']}/10 — {score['novelty_reasoning'] or '无'}\n"
            f"- 综合推荐度：{score['total_score']}/10\n"
            f"- 建议角度：{score['key_angle'] or '无'}"
        )
    skill_lines = "\n".join(
        f"- {'✓' if item.get('available') else '⚠'} `{item['skill_name']}`"
        + (f" · 路径：`{item.get('path')}` · 版本：{item.get('content_hash', '')[:12]}" if item.get("available") else f" · 未找到 `{item.get('path') or '未配置内容 SKILL 目录'}`")
        for item in manifest
    )
    source_lines = (
        f"- OpenClaw 原始文件：`{topic['source_ref'] or '无'}`\n"
        f"- 原文链接：{topic['original_url'] or '无'}\n"
        f"- 来源 / 作者 / 日期：{topic['source_name'] or '未知'} / {topic['source_author'] or '未知'} / {topic['published_date'] or topic['news_date'] or '未知'}\n"
        f"- 原始文件校验：{source_check['status']}\n"
        "- 注意：内容中心不复制原始 MD；请以以上 OpenClaw 文件作为原始资料事实。"
    )
    deliverable_text = "、".join(deliverables) if deliverables else "公众号发布稿（正文、排版、封面、正文配图）"
    content = f"""# 内容创作任务契约

> 任务 ID：`{task_id}`  
> 模式：`{task['mode']}`  
> 当前状态：`{task['status']}`  
> 目标平台：`{task['requested_platform']}`  
> 交付：{deliverable_text}

## 唯一工作流入口

在内容 vault 根目录执行：`开始内容创作工作流`。  
必须先使用 `$content-creation-workflow`，再按其规则连续调用下列节点 Skill。选题已确认；除非遇到 Skill 定义的硬阻塞，不要在中途等待普通确认。

## 当前阶段与续作规则

任务契约生成时的阶段快照如下：

{stage_lines}

从第一个状态不是 `completed` 的必需阶段继续。已经 `completed` 的阶段只读取其产物和结果，不要重做；状态为 `running` 的旧阶段视为可能中断的执行，先读取已生成产物，必要时由新 Agent 接管并重新开始该阶段。不要因为阶段回传过 `draft_ready` 就停止，四个必需阶段完成前不得回传任务 `draft_ready`。

## 多代理岗位链

本任务不是单 Agent 一次性写稿，必须按以下四个阶段顺序执行并在 ERP 留下阶段回传：

1. 主编 Agent（策划）：确认读者需求、市场入口、核心冲突、文章目标和验收标准，输出 `主编立项卡.md`。
2. 作者 Agent（研究与写作）：自行补充和核验资料，完成文章结构、`研究与事实说明.md`与`正文主稿.md`；真人样本路由和自检通过阶段 self_check 回传，不另建记录文件。
3. 编辑 Agent（平台编辑与视觉）：{editor_scope} 对仅请求公众号的任务，直接编辑`正文主稿.md`，不得生成同内容的公众号副本；标题选择、扫读回放和视觉检查通过 self_check 回传，不另建自证文件。公众号稿必须执行 content-publish-prepare 的扫读排版：短段落、每节主判断、有限加粗、引用或列表呼吸点，并在 self_check 中记录 scan_layout；不能只保留普通段落和 H2。
4. 主编 Agent（终审）：对全部内容负责，依据 L0-L5 检查事实、判断、结构、标题、平台版本和视觉；通过才输出 `质检报告.md`。

主编终审不通过时必须返回 `decision=rework`，并明确 `return_to_stage=writer` 或 `return_to_stage=editor`；目标阶段完成后必须重新经过后续阶段和主编终审。任何必要阶段没有真实产物或被退回，任务都不能回传 `publish_ready`。

## 固定工作目录

- 内容 vault：`{vault_dir}`
- Skill 根目录：`{skills_dir}`
- 读取 Skill 时优先使用上方清单中的绝对路径；不要在当前项目的 `.agents/skills` 中猜测。
- 图像能力要求：必须读取 `C:\\Users\\Alex\\.codex\\skills\\.system\\imagegen\\SKILL.md` 并使用 `imagegen` 生成图片；没有 imagegen 工具、API 凭据或网络时只能回传 `blocked`，不得用 PIL、SVG 或 HTML/CSS 合成图冒充。
- 公众号 `publish_ready` 必须在每个图片产物中记录 `generator=imagegen` 与 `generation_id` 或 `prompt_hash`，并让 `self_check.image_generation` 确认已复核且质量通过。

## 本次 Skill 清单（版本快照）

{skill_lines}

## 选题

- 标题：{topic['title']}
- 选择备注：{task['selected_note'] or '无'}
- 选题摘要：{topic['summary'] or '无'}

### 评分依据

{score_lines}

### 原始资料指针

{source_lines}

### 历史复盘方法参考

{learning_methods}

### 已解析正文（仅为工作副本）

---
{topic['raw_content'] or '无'}
---

## 交付与回传

1. 通过 `$content-project-intake` 在 Obsidian 的 `03-projects/` 创建或接管内容项目；不要把完成文章放入临时笔记目录。
2. 完成后回传项目路径、主稿路径、封面/正文配图等产物路径，并声明已使用的 Skill 和自检结果。
3. 发布稿完成不等于已发布：不得把平台发布事实写回本任务。最终发布仍由人确认，结果由 OPC-ERP 数据回流观察。
4. 回传接口：`POST {base_url}/api/production/tasks/{task_id}/callback`，请求中必须包含本任务 callback_token。
5. callback_token：`{task['callback_token']}`

### 回传状态

- `blocked`：必须写明真正的硬阻塞原因；
- `draft_ready`：主稿已完成，仍待发布准备或人工修改；
- `publish_ready`：达到公众号发布稿验收标准；
- `failed`：本轮执行失败，写明错误与可重试建议。
"""
    directory = _contract_directory()
    directory.mkdir(parents=True, exist_ok=True)
    safe_title = "".join(char for char in topic["title"] if char.isalnum() or char in (" ", "-", "_"))[:42].strip() or "topic"
    path = directory / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{task_id[:8]}_{safe_title}.md"
    path.write_text(content, encoding="utf-8")
    conn.execute("UPDATE production_tasks SET contract_path = ?, updated_at = ? WHERE id = ?", (str(path), now_iso(), task_id))
    conn.execute(
        "INSERT INTO task_files (topic_id, file_path, template_used, generated_at) VALUES (?, ?, 'contract_v2', ?)",
        (topic["id"], str(path), now_iso()),
    )
    return str(path)


def create_task(topic_id: int, *, mode: str | None = None, requested_platform: str = "wechat", requested_deliverables: list[str] | None = None, executor_hint: str = "Codex", selected_note: str = "", created_by: str = "human") -> dict[str, Any]:
    """从已评分候选创建唯一活动任务，并生成给自动化消费的 Markdown 契约。"""
    conn = get_db()
    try:
        topic = conn.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()
        if not topic:
            raise ValueError("选题不存在")
        # 建创作任务的前提是"这条素材值得写"，即已经有评分记录。
        # 素材的使用状态（未用/已用/弃用）由人在看板上决定，建任务不再改它。
        scored = conn.execute(
            "SELECT 1 FROM topic_scores WHERE topic_id = ? LIMIT 1", (topic_id,)
        ).fetchone()
        if not scored:
            raise ValueError(f"选题「{topic['title'][:30]}」还没有评分，先评分再创建创作任务")
        existing = conn.execute(
            f"SELECT * FROM production_tasks WHERE topic_id = ? AND status IN ({','.join('?' for _ in ACTIVE_TASK_STATUSES)}) ORDER BY created_at DESC LIMIT 1",
            (topic_id, *ACTIVE_TASK_STATUSES),
        ).fetchone()
        if existing:
            return {"task": dict(existing), "created": False, "message": "该选题已有进行中的创作任务"}

        selected_mode = mode or get_setting("production_mode", "manual")
        if selected_mode not in ("manual", "autonomous"):
            raise ValueError("生产模式无效")
        timestamp = now_iso()
        task_id = str(uuid.uuid4())
        manifest = _read_skill_manifest(conn)
        conn.execute(
            """INSERT INTO production_tasks
            (id, topic_id, mode, status, requested_platform, requested_deliverables, executor_hint,
             created_by, selected_note, callback_token, skill_manifest_json, artifact_manifest_json, created_at, updated_at)
            VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?)""",
            (
                task_id, topic_id, selected_mode, requested_platform, _json(requested_deliverables or ["wechat_publish_ready"]),
                executor_hint, created_by, selected_note, secrets.token_urlsafe(24), _json(manifest), timestamp, timestamp,
            ),
        )
        _ensure_stage_runs(conn, task_id)
        # 只补记"被选去创作"的时间与备注；不写 topics.status ——
        # 那是"素材用没用"，由人勾选，机器不能替人决定。
        conn.execute(
            "UPDATE topics SET selected_at = COALESCE(selected_at, ?), selected_note = ?, updated_at = ? WHERE id = ?",
            (timestamp, selected_note, timestamp, topic_id),
        )
        _record_topic_event(conn, topic_id, "selected", {"mode": selected_mode, "task_id": task_id, "note": selected_note})
        _record_topic_event(conn, topic_id, "production_task_queued", {"task_id": task_id, "mode": selected_mode})
        _record_activity(conn, created_by, "task_queued", "production_task", task_id, f"已创建创作任务：{topic['title'][:50]}", {"topic_id": topic_id})
        contract_path = _write_contract(conn, task_id)
        conn.commit()
        task = conn.execute("SELECT * FROM production_tasks WHERE id = ?", (task_id,)).fetchone()
        return {"task": dict(task), "created": True, "contract_path": contract_path, "message": "创作任务已排队，等待 Codex 自动化领取"}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_tasks(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    conn = get_db()
    try:
        sql = """SELECT task.*, topic.title AS topic_title, topic.news_date, topic.original_url,
                 score.total_score AS latest_score
                 FROM production_tasks task
                 JOIN topics topic ON topic.id = task.topic_id
                 LEFT JOIN topic_scores score ON score.id = (
                    SELECT id FROM topic_scores WHERE topic_id = topic.id ORDER BY scored_at DESC, id DESC LIMIT 1
                 )"""
        params: list[Any] = []
        if status:
            sql += " WHERE task.status = ?"
            params.append(status)
        sql += " ORDER BY CASE task.status WHEN 'blocked' THEN 0 WHEN 'queued' THEN 1 WHEN 'running' THEN 2 ELSE 3 END, task.created_at DESC LIMIT ?"
        params.append(min(max(limit, 1), 500))
        result = []
        for row in conn.execute(sql, params).fetchall():
            item = dict(row)
            item["stage_summary"] = _stage_summary(conn, item["id"])
            result.append(item)
        return result
    finally:
        conn.close()


def list_publish_ready_notifications(since: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """返回指定时间后新进入发布准备状态的任务，供页面和桌面提醒轮询。"""
    conn = get_db()
    try:
        sql = """SELECT task.id, task.status, task.updated_at, task.primary_article_path,
                        task.vault_project_path, topic.title AS topic_title
                 FROM production_tasks task
                 JOIN topics topic ON topic.id = task.topic_id
                 WHERE task.status = 'publish_ready'"""
        params: list[Any] = []
        if since:
            sql += " AND task.updated_at > ?"
            params.append(since)
        sql += " ORDER BY task.updated_at DESC LIMIT ?"
        params.append(min(max(limit, 1), 100))
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def get_task(task_id: str) -> dict[str, Any] | None:
    conn = get_db()
    try:
        task = conn.execute(
            """SELECT task.*, topic.title AS topic_title, topic.summary AS topic_summary, topic.source_ref,
                      topic.original_url, topic.raw_content
               FROM production_tasks task JOIN topics topic ON topic.id = task.topic_id WHERE task.id = ?""",
            (task_id,),
        ).fetchone()
        if not task:
            return None
        result = dict(task)
        result["runs"] = [dict(row) for row in conn.execute("SELECT * FROM task_runs WHERE task_id = ? ORDER BY created_at DESC", (task_id,)).fetchall()]
        result["stages"] = _stage_rows(conn, task_id)
        result["stage_summary"] = _stage_summary(conn, task_id)
        return result
    finally:
        conn.close()


def recover_stale_tasks(max_age_minutes: int = DEFAULT_EXECUTION_LEASE_MINUTES, requested_by: str = "system") -> dict[str, Any]:
    """结束失去执行器的领取/运行任务，避免 UI 永久显示“执行中”。

    这是保守恢复：不自动重试、不伪造终审或最终 callback；所有已有阶段产物
    和失败原因均保留。用户可在确认后使用原有 retry 接口重新排队。
    """
    age = min(max(int(max_age_minutes), 5), 24 * 60)
    cutoff = (datetime.now() - timedelta(minutes=age)).isoformat()
    timestamp = now_iso()
    reason = f"执行器租约已超过 {age} 分钟且未回传；已自动结束，保留产物，需人工确认后重试。"
    conn = get_db()
    try:
        stale = conn.execute(
            """SELECT id, topic_id, status FROM production_tasks
               WHERE status IN ('claimed', 'running')
                 AND COALESCE(updated_at, started_at, claimed_at, created_at) < ?""",
            (cutoff,),
        ).fetchall()
        recovered: list[str] = []
        for task in stale:
            conn.execute(
                """UPDATE production_stage_runs
                   SET status = 'failed', error_detail = COALESCE(error_detail, ?),
                       completed_at = ?, updated_at = ?
                   WHERE task_id = ? AND status = 'running'""",
                (reason, timestamp, timestamp, task['id']),
            )
            conn.execute(
                """UPDATE task_runs
                   SET status = 'failed', error_detail = COALESCE(error_detail, ?),
                       finished_at = ?, updated_at = ?
                   WHERE task_id = ? AND status IN ('claimed', 'running')""",
                (reason, timestamp, timestamp, task['id']),
            )
            conn.execute(
                """UPDATE production_tasks
                   SET status = 'failed', blocked_reason = ?, completed_at = ?, updated_at = ?
                   WHERE id = ? AND status IN ('claimed', 'running')""",
                (reason, timestamp, timestamp, task['id']),
            )
            _record_topic_event(conn, task['topic_id'], 'task_lease_expired', {'task_id': task['id'], 'requested_by': requested_by, 'max_age_minutes': age})
            _record_activity(conn, requested_by, 'task_lease_expired', 'production_task', task['id'], reason, {'max_age_minutes': age})
            recovered.append(task['id'])
        conn.commit()
        return {'recovered_task_ids': recovered, 'count': len(recovered), 'max_age_minutes': age}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _claim_task_in_connection(conn, task: Any, agent_name: str) -> str:
    """在已开启写事务的连接中领取任务，调用方负责提交或回滚。"""
    timestamp = now_iso()
    run_id = str(uuid.uuid4())
    updated = conn.execute(
        """UPDATE production_tasks
        SET status = 'claimed', claimed_at = ?, blocked_reason = NULL, updated_at = ?
        WHERE id = ? AND (
            status IN ('queued', 'blocked')
            OR (
                status = 'draft_ready'
                AND mode = 'autonomous'
                AND EXISTS (
                    SELECT 1 FROM production_stage_runs stage
                    WHERE stage.task_id = production_tasks.id
                      AND stage.status IN ('queued', 'running', 'failed')
                )
            )
        )""",
        (timestamp, timestamp, task["id"]),
    )
    if updated.rowcount != 1:
        raise ValueError("任务已被其他 Agent 领取，请重新获取下一任务")
    conn.execute(
        """INSERT INTO task_runs (id, task_id, agent_name, status, created_at, updated_at)
        VALUES (?, ?, ?, 'claimed', ?, ?)""",
        (run_id, task["id"], agent_name, timestamp, timestamp),
    )
    _record_topic_event(conn, task["topic_id"], "task_claimed", {"task_id": task["id"], "run_id": run_id, "agent": agent_name})
    _record_activity(conn, agent_name, "task_claimed", "production_task", task["id"], "已领取创作任务", {"run_id": run_id})
    return run_id


def claim_task(task_id: str, agent_name: str) -> dict[str, Any]:
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        task = conn.execute("SELECT * FROM production_tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            raise ValueError("创作任务不存在")
        resumable = task["status"] == "draft_ready" and task["mode"] == "autonomous" and conn.execute(
            "SELECT 1 FROM production_stage_runs WHERE task_id = ? AND status IN ('queued', 'running', 'failed') LIMIT 1",
            (task_id,),
        ).fetchone()
        if task["status"] not in ("queued", "blocked") and not resumable:
            raise ValueError(f"当前任务状态 {task['status']} 不允许领取")
        run_id = _claim_task_in_connection(conn, task, agent_name)
        conn.commit()
        return {"task_id": task_id, "run_id": run_id, "status": "claimed"}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def claim_next_task(agent_name: str = "Codex") -> dict[str, Any]:
    """原子领取一条待执行任务，并返回可直接交给 Agent 的契约内容。"""
    # 下一次执行器领取前先收敛失联任务；失败任务不会被本函数自动重跑。
    recover_stale_tasks(requested_by="claim_next")
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        task = conn.execute(
            """SELECT task.*, topic.title AS topic_title, topic.summary AS topic_summary,
                      topic.source_ref, topic.original_url, topic.raw_content
               FROM production_tasks task
               JOIN topics topic ON topic.id = task.topic_id
               WHERE task.status IN ('queued', 'blocked')
                      OR (
                          task.status = 'draft_ready'
                          AND task.mode = 'autonomous'
                          AND EXISTS (
                              SELECT 1 FROM production_stage_runs stage
                              WHERE stage.task_id = task.id
                                AND stage.status IN ('queued', 'running', 'failed')
                          )
                      )
               ORDER BY CASE task.status WHEN 'blocked' THEN 0 WHEN 'draft_ready' THEN 1 ELSE 2 END,
                        (SELECT COUNT(*) FROM production_stage_runs completed_stage
                         WHERE completed_stage.task_id = task.id AND completed_stage.status = 'completed') DESC,
                        task.created_at ASC""",
        ).fetchone()
        if not task:
            conn.rollback()
            return {"claimed": False, "message": "当前没有待执行创作任务"}

        contract_path = Path(task["contract_path"] or "")
        if not contract_path.is_file():
            _write_contract(conn, task["id"])
            task = conn.execute(
                """SELECT task.*, topic.title AS topic_title, topic.summary AS topic_summary,
                          topic.source_ref, topic.original_url, topic.raw_content
                   FROM production_tasks task
                   JOIN topics topic ON topic.id = task.topic_id
                   WHERE task.id = ?""",
                (task["id"],),
            ).fetchone()
            contract_path = Path(task["contract_path"] or "")

        if not contract_path.is_file():
            raise ValueError("任务契约不存在，无法交给 Agent 执行")

        # 领取时重新快照 Skill，保证旧任务重试也不会继续消费过期契约。
        conn.execute("UPDATE production_tasks SET skill_manifest_json = ?, updated_at = ? WHERE id = ?", (_json(_read_skill_manifest(conn)), now_iso(), task["id"]))
        _write_contract(conn, task["id"])
        contract_path = Path(conn.execute("SELECT contract_path FROM production_tasks WHERE id = ?", (task["id"],)).fetchone()["contract_path"])
        run_id = _claim_task_in_connection(conn, task, agent_name)
        conn.commit()
        return {
            "claimed": True,
            "task_id": task["id"],
            "run_id": run_id,
            "status": "claimed",
            "agent_name": agent_name,
            "contract_path": str(contract_path),
            "contract_text": contract_path.read_text(encoding="utf-8"),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def start_task(task_id: str, agent_name: str) -> dict[str, Any]:
    conn = get_db()
    try:
        task = conn.execute("SELECT * FROM production_tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            raise ValueError("创作任务不存在")
        if task["status"] not in ("claimed", "queued"):
            raise ValueError(f"当前任务状态 {task['status']} 不允许开始")
        timestamp = now_iso()
        run = conn.execute("SELECT * FROM task_runs WHERE task_id = ? AND status = 'claimed' ORDER BY created_at DESC LIMIT 1", (task_id,)).fetchone()
        run_id = run["id"] if run else str(uuid.uuid4())
        if run:
            conn.execute("UPDATE task_runs SET status = 'running', started_at = ?, updated_at = ? WHERE id = ?", (timestamp, timestamp, run_id))
        else:
            conn.execute("INSERT INTO task_runs (id, task_id, agent_name, status, started_at, created_at, updated_at) VALUES (?, ?, ?, 'running', ?, ?, ?)", (run_id, task_id, agent_name, timestamp, timestamp, timestamp))
        conn.execute("UPDATE production_tasks SET status = 'running', started_at = COALESCE(started_at, ?), updated_at = ? WHERE id = ?", (timestamp, timestamp, task_id))
        _record_topic_event(conn, task["topic_id"], "task_started", {"task_id": task_id, "run_id": run_id, "agent": agent_name})
        _record_activity(conn, agent_name, "task_started", "production_task", task_id, "开始执行内容创作工作流", {"run_id": run_id})
        conn.commit()
        return {"task_id": task_id, "run_id": run_id, "status": "running"}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def retry_task(task_id: str, requested_by: str = "human") -> dict[str, Any]:
    """将失败或阻塞任务重新排队；历史运行和阶段结果均保留。"""
    conn = get_db()
    try:
        task = conn.execute("SELECT * FROM production_tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            raise ValueError("创作任务不存在")
        if task["status"] not in ("failed", "blocked"):
            raise ValueError(f"当前任务状态 {task['status']} 不允许重试")
        timestamp = now_iso()
        conn.execute(
            """UPDATE production_stage_runs
               SET status = 'failed', error_detail = COALESCE(error_detail, '上一轮执行已中断，重试时重新接管'),
                   completed_at = ?, updated_at = ?
               WHERE task_id = ? AND status = 'running'""",
            (timestamp, timestamp, task_id),
        )
        conn.execute(
            "UPDATE production_tasks SET status = 'queued', blocked_reason = NULL, completed_at = NULL, updated_at = ? WHERE id = ?",
            (timestamp, task_id),
        )
        conn.execute("UPDATE production_tasks SET skill_manifest_json = ?, updated_at = ? WHERE id = ?", (_json(_read_skill_manifest(conn)), timestamp, task_id))
        _write_contract(conn, task_id)
        _record_topic_event(conn, task["topic_id"], "production_task_retried", {"task_id": task_id, "requested_by": requested_by})
        _record_activity(conn, requested_by, "task_retried", "production_task", task_id, "任务已重新排队，等待多代理流水线", {"previous_status": task["status"]})
        conn.commit()
        return {"task_id": task_id, "status": "queued", "message": "任务已重新排队，下一轮将按多代理岗位链执行"}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _normalise_artifacts(artifacts: list[Any]) -> list[dict[str, Any]]:
    """兼容 Agent 直接回传路径字符串，同时保持统一的可审计产物结构。"""
    normalised = []
    for item in artifacts:
        if isinstance(item, str):
            normalised.append({"kind": "artifact", "path": item})
        elif isinstance(item, dict):
            normalised.append(item)
        else:
            raise ValueError("artifacts 中每项必须是路径字符串或对象")
    return normalised


def _check_output_paths(vault_project_path: str | None, primary_article_path: str | None, artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """校验回传产物存在；配置 vault 根目录时也防止路径逃逸。"""
    root_value = get_setting("content_vault_dir", "").strip()
    root = Path(root_value).resolve() if root_value else None
    checks: list[dict[str, Any]] = []
    candidates = [("vault_project", vault_project_path), ("primary_article", primary_article_path)]
    candidates.extend((str(item.get("kind") or "artifact"), item.get("path")) for item in artifacts)
    for kind, raw_path in candidates:
        if not raw_path:
            continue
        path = Path(raw_path)
        exists = path.exists()
        inside_vault = True
        if root and exists:
            try:
                path.resolve().relative_to(root)
            except ValueError:
                inside_vault = False
        checks.append({"kind": kind, "path": str(path), "exists": exists, "inside_vault": inside_vault})
    return checks


def start_stage(task_id: str, stage_key: str, agent_name: str) -> dict[str, Any]:
    """启动一个岗位阶段；同一阶段可在失败后以递增 attempt 重试。"""
    definition = _stage_definition(stage_key)
    conn = get_db()
    try:
        task = conn.execute("SELECT * FROM production_tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            raise ValueError("创作任务不存在")
        latest = conn.execute(
            "SELECT * FROM production_stage_runs WHERE task_id = ? AND stage_key = ? ORDER BY attempt DESC LIMIT 1",
            (task_id, stage_key),
        ).fetchone()
        if latest and latest["status"] in ("running", "completed"):
            # 终审可以把已经完成的下游阶段退回返工。此时保留历史事实，
            # 允许目标阶段创建新的 attempt；普通重复调用仍保持幂等。
            rework_requested = False
            if latest["status"] == "completed":
                review = conn.execute(
                    "SELECT result_json FROM production_stage_runs "
                    "WHERE task_id = ? AND stage_key = 'chief_editor_review' "
                    "ORDER BY attempt DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                if review:
                    review_result = json.loads(review["result_json"] or "{}")
                    rework_requested = (
                        str(review_result.get("decision") or "").strip().lower() == "rework"
                        and str(review_result.get("return_to_stage") or "").strip() == stage_key
                    )
            if not rework_requested:
                return {"stage_id": latest["id"], "stage_key": stage_key, "role_name": latest["role_name"], "status": latest["status"], "attempt": latest["attempt"], "idempotent": True}
        prerequisite = STAGE_PREREQUISITES.get(stage_key)
        if prerequisite:
            prerequisite_row = conn.execute(
                "SELECT status FROM production_stage_runs WHERE task_id = ? AND stage_key = ? ORDER BY attempt DESC LIMIT 1",
                (task_id, prerequisite),
            ).fetchone()
            if not prerequisite_row or prerequisite_row["status"] != "completed":
                prerequisite_label = _stage_definition(prerequisite)["label"]
                raise ValueError(f"阶段顺序错误：必须先完成{prerequisite_label}")
        attempt = (latest["attempt"] + 1) if latest else 1
        timestamp = now_iso()
        stage_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO production_stage_runs
            (id, task_id, stage_key, role_name, status, attempt, started_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?)""",
            (stage_id, task_id, stage_key, definition["role"], attempt, timestamp, timestamp, timestamp),
        )
        _record_activity(conn, agent_name or definition["role"], "stage_started", "production_stage", stage_id, f"{definition['label']}阶段开始", {"task_id": task_id, "stage_key": stage_key, "attempt": attempt})
        conn.commit()
        return {"stage_id": stage_id, "stage_key": stage_key, "role_name": definition["role"], "status": "running", "attempt": attempt, "idempotent": False}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def complete_stage(stage_id: str, output_paths: list[str], result: dict[str, Any], agent_name: str) -> dict[str, Any]:
    conn = get_db()
    try:
        stage = conn.execute("SELECT * FROM production_stage_runs WHERE id = ?", (stage_id,)).fetchone()
        if not stage:
            raise ValueError("生产阶段不存在")
        if str(result.get("decision") or "").strip().lower() != "pass":
            raise ValueError("只有 decision=pass 才能完成阶段；rework 或 blocked 必须回写为失败并保留退回目标")
        if stage["status"] == "completed":
            return {"stage_id": stage_id, "status": "completed", "idempotent": True}
        timestamp = now_iso()
        conn.execute(
            """UPDATE production_stage_runs SET status = 'completed', output_paths_json = ?, result_json = ?,
            completed_at = ?, updated_at = ? WHERE id = ? AND status = 'running'""",
            (_json(output_paths), _json(result), timestamp, timestamp, stage_id),
        )
        _record_activity(conn, agent_name or stage["role_name"], "stage_completed", "production_stage", stage_id, f"{stage['role_name']}完成", {"task_id": stage["task_id"], "stage_key": stage["stage_key"], "attempt": stage["attempt"], "output_paths": output_paths})
        conn.commit()
        return {"stage_id": stage_id, "status": "completed", "idempotent": False}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fail_stage(stage_id: str, error_detail: str, result: dict[str, Any] | None, agent_name: str) -> dict[str, Any]:
    conn = get_db()
    try:
        stage = conn.execute("SELECT * FROM production_stage_runs WHERE id = ?", (stage_id,)).fetchone()
        if not stage:
            raise ValueError("生产阶段不存在")
        timestamp = now_iso()
        conn.execute(
            """UPDATE production_stage_runs SET status = 'failed', error_detail = ?, result_json = ?,
            completed_at = ?, updated_at = ? WHERE id = ? AND status = 'running'""",
            (error_detail, _json(result or {}), timestamp, timestamp, stage_id),
        )
        _record_activity(conn, agent_name or stage["role_name"], "stage_failed", "production_stage", stage_id, f"{stage['role_name']}失败：{error_detail[:160]}", {"task_id": stage["task_id"], "stage_key": stage["stage_key"], "attempt": stage["attempt"]})
        conn.commit()
        return {"stage_id": stage_id, "status": "failed"}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reconcile_completed_task(task_id: str, reason: str = "执行器退出但四阶段已完成") -> dict[str, Any]:
    """用已落盘的四阶段证据恢复被执行器退出误收敛的任务。"""
    conn = get_db()
    try:
        task = conn.execute(
            "SELECT task.*, topic.title AS topic_title FROM production_tasks task JOIN topics topic ON topic.id = task.topic_id WHERE task.id = ?",
            (task_id,),
        ).fetchone()
        if not task:
            raise ValueError("创作任务不存在")
        if task["status"] not in ("running", "failed"):
            raise ValueError(f"当前任务状态 {task['status']} 不需要执行器恢复")

        latest: dict[str, dict[str, Any]] = {}
        for row in conn.execute(
            "SELECT * FROM production_stage_runs WHERE task_id = ? ORDER BY attempt ASC, created_at ASC",
            (task_id,),
        ).fetchall():
            latest[row["stage_key"]] = dict(row)
        missing = [stage["label"] for stage in PRODUCTION_STAGES if not latest.get(stage["key"]) or latest[stage["key"]]["status"] != "completed"]
        if missing:
            raise ValueError("阶段证据不完整，不能恢复：" + "、".join(missing))

        review_result = json.loads(latest["chief_editor_review"].get("result_json") or "{}")
        if str(review_result.get("decision") or "").lower() != "pass":
            raise ValueError("主编终审没有明确通过，不能恢复为发布稿")

        output_paths: list[str] = []
        used_skills: set[str] = set()
        editor_self_check: dict[str, Any] = {}
        for stage in latest.values():
            output_paths.extend(json.loads(stage.get("output_paths_json") or "[]"))
            result = json.loads(stage.get("result_json") or "{}")
            used_skills.update(str(item).strip() for item in result.get("used_skills", []) if str(item).strip())
            if stage["stage_key"] == "editor":
                editor_self_check = result.get("self_check") or {}
        output_paths = list(dict.fromkeys(output_paths))
        existing_paths = [Path(path) for path in output_paths if Path(path).is_file()]
        article = Path(task["primary_article_path"] or "")
        if not article.is_file():
            article = next(
                (path for path in existing_paths if path.name in {"公众号发布准备稿.md", "正文主稿.md", "article.md"}),
                Path(""),
            )
        if not article.is_file():
            raise ValueError("四阶段已完成，但找不到公众号发布准备稿.md")
        project_value = str(task["vault_project_path"] or "").strip()
        project = Path(project_value) if project_value else Path("")
        if not project_value or not project.is_dir():
            project = article.parent

        image_record = project / "图片生成记录与视觉质检.md"
        record_text = image_record.read_text(encoding="utf-8-sig") if image_record.is_file() else ""
        article_text = article.read_text(encoding="utf-8-sig")
        if (project / "assets").is_dir():
            existing_paths.extend((project / "assets").rglob("*"))
        existing_paths = list(dict.fromkeys(path for path in existing_paths if path.is_file()))
        artifacts: list[dict[str, Any]] = []
        for path in existing_paths:
            if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                continue
            matching_line = next((line for line in record_text.splitlines() if path.name in line), "")
            # 阶段重试可能留下旧图；只验收最终文章实际引用的图片，避免历史残留污染终态。
            if not matching_line and path.name not in article_text:
                continue
            generation_ids = re.findall(r"`(exec-[^`]+)`", matching_line)
            if not generation_ids:
                record_ids = re.findall(r"`(exec-[^`]+)`", record_text)
                if len(record_ids) == 1:
                    generation_ids = record_ids
            if not generation_ids:
                raise ValueError(f"图片缺少可追溯 imagegen 记录：{path.name}")
            kind = "cover" if "cover" in path.name.lower() or "封面" in path.name else "image"
            artifacts.append({"kind": kind, "path": str(path), "generator": "imagegen", "generation_id": generation_ids[0]})
        if not artifacts:
            raise ValueError("四阶段已完成，但没有图片产物")

        self_check = {
            "image_generation": {"mode": "imagegen", "reviewed": True, "quality": "pass"},
            "reconciled_from_stage_evidence": True,
            "reconcile_reason": reason,
            "editor_self_check": editor_self_check,
        }
        payload = {
            "callback_token": task["callback_token"],
            "status": "publish_ready",
            "agent_name": "Codex",
            "summary": "执行器退出后根据四阶段完成证据恢复为发布准备完成；未执行外部发布。",
            "vault_project_path": str(project),
            "primary_article_path": str(article),
            "artifacts": artifacts,
            "used_skills": sorted(used_skills | {"content-publish-prepare", "imagegen"}),
            "self_check": self_check,
        }
    finally:
        conn.close()

    return callback_task(task_id, payload)


def _required_stages_completed(conn, task_id: str) -> tuple[bool, list[str]]:
    latest = {}
    for row in conn.execute("SELECT * FROM production_stage_runs WHERE task_id = ? ORDER BY attempt ASC", (task_id,)).fetchall():
        latest[row["stage_key"]] = row
    missing = [stage["label"] for stage in PRODUCTION_STAGES if stage["required"] and (not latest.get(stage["key"]) or latest[stage["key"]]["status"] != "completed")]
    return not missing, missing


def callback_task(task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Agent 的幂等回传：记录声明、校验产物、更新任务状态，但不标记外部发布。"""
    conn = get_db()
    try:
        task = conn.execute("SELECT * FROM production_tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            raise ValueError("创作任务不存在")
        if not secrets.compare_digest(task["callback_token"], str(payload.get("callback_token") or "")):
            raise ValueError("任务回传凭证无效")
        status = payload.get("status")
        if status not in CALLBACK_STATUSES:
            raise ValueError("回传状态无效")
        agent_name = (payload.get("agent_name") or "Codex").strip()
        artifacts = _normalise_artifacts(payload.get("artifacts") or [])
        checks = _check_output_paths(payload.get("vault_project_path"), payload.get("primary_article_path"), artifacts)
        if status in ("draft_ready", "publish_ready"):
            if not payload.get("vault_project_path") or not payload.get("primary_article_path"):
                raise ValueError("草稿完成或发布稿完成时必须回传 Obsidian 项目路径和主稿路径")
            missing = [item for item in checks if not item["exists"] or not item["inside_vault"]]
            if missing:
                raise ValueError("回传产物不存在或不在配置的内容 vault 内")
        timestamp = now_iso()
        if status in ("failed", "blocked"):
            conn.execute(
                """UPDATE production_stage_runs
                   SET status = 'failed', error_detail = COALESCE(error_detail, ?),
                       completed_at = ?, updated_at = ?
                   WHERE task_id = ? AND status = 'running'""",
                (payload.get("error_detail") or payload.get("summary") or "任务执行中断，阶段待重试", timestamp, timestamp, task_id),
            )
        run = conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? AND status IN ('claimed', 'running') ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        run_id = run["id"] if run else str(uuid.uuid4())
        self_check = dict(payload.get("self_check") or {})
        used_skills = payload.get("used_skills") or []
        if status == "draft_ready":
            stage_count = conn.execute("SELECT COUNT(*) AS count FROM production_stage_runs WHERE task_id = ?", (task_id,)).fetchone()["count"]
            if stage_count:
                complete, missing_stages = _required_stages_completed(conn, task_id)
                if not complete:
                    raise ValueError("作者或编辑阶段尚未完成，不能以 draft_ready 收口：缺少 " + "、".join(missing_stages))
        if status == "publish_ready" and task["requested_platform"] == "wechat":
            stage_count = conn.execute("SELECT COUNT(*) AS count FROM production_stage_runs WHERE task_id = ?", (task_id,)).fetchone()["count"]
            if stage_count:
                complete, missing_stages = _required_stages_completed(conn, task_id)
                if not complete:
                    raise ValueError("多代理工作流尚未完成：缺少 " + "、".join(missing_stages))
            declared_skills = {
                str(item.get("skill_name") or item.get("name") or "").strip().lower()
                if isinstance(item, dict) else str(item).strip().lower()
                for item in used_skills
            }
            if "imagegen" not in declared_skills:
                raise ValueError("公众号发布准备必须回传 imagegen Skill 使用记录")
            validation = validate_publish_bundle(
                payload.get("vault_project_path"),
                payload.get("primary_article_path"),
                artifacts,
                self_check,
            )
            if not validation["ok"]:
                raise ValueError("发布前自动检查未通过：" + "；".join(validation["errors"]))
            self_check["publish_validation"] = validation
        self_check["artifact_checks"] = checks
        final_status = status
        update_fields = {
            "status": final_status,
            "vault_project_path": payload.get("vault_project_path") or task["vault_project_path"],
            "primary_article_path": payload.get("primary_article_path") or task["primary_article_path"],
            "artifact_manifest_json": _json(artifacts),
            "blocked_reason": payload.get("blocked_reason") if status == "blocked" else None,
            "completed_at": timestamp if status in ("draft_ready", "publish_ready", "failed") else task["completed_at"],
            "updated_at": timestamp,
        }
        conn.execute(
            """UPDATE production_tasks SET status = :status, vault_project_path = :vault_project_path,
            primary_article_path = :primary_article_path, artifact_manifest_json = :artifact_manifest_json,
            blocked_reason = :blocked_reason, completed_at = :completed_at, updated_at = :updated_at WHERE id = :id""",
            {**update_fields, "id": task_id},
        )
        if run:
            conn.execute(
                """UPDATE task_runs SET status = ?, finished_at = ?, summary = ?, error_detail = ?,
                used_skills_json = ?, self_check_json = ?, callback_payload_json = ?, updated_at = ? WHERE id = ?""",
                (status, timestamp, payload.get("summary"), payload.get("error_detail"), _json(used_skills), _json(self_check), _json(payload), timestamp, run_id),
            )
        else:
            conn.execute(
                """INSERT INTO task_runs (id, task_id, agent_name, status, finished_at, summary, error_detail,
                used_skills_json, self_check_json, callback_payload_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, task_id, agent_name, status, timestamp, payload.get("summary"), payload.get("error_detail"), _json(used_skills), _json(self_check), _json(payload), timestamp, timestamp),
            )
        _record_topic_event(conn, task["topic_id"], "task_callback", {"task_id": task_id, "run_id": run_id, "status": status, "agent": agent_name})
        _record_activity(conn, agent_name, f"task_{status}", "production_task", task_id, payload.get("summary") or f"任务回传：{status}", {"run_id": run_id})
        conn.commit()
        return {"task_id": task_id, "run_id": run_id, "status": status, "artifact_checks": checks}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def auto_queue_tasks(limit: int | None = None) -> dict[str, Any]:
    """自主模式只创建任务契约；统一锁住按钮和 Codex 的并发派单。"""
    with _AUTO_QUEUE_LOCK:
        if get_setting("production_mode", "manual") != "autonomous":
            raise ValueError("当前为人工选择模式，不能自动派单")
        daily_limit = min(max(limit or get_int_setting("daily_task_limit", 1), 1), 3)
        conn = get_db()
        try:
            today = datetime.now().date().isoformat()
            current_count = conn.execute("SELECT COUNT(*) AS n FROM production_tasks WHERE mode = 'autonomous' AND substr(created_at, 1, 10) = ?", (today,)).fetchone()["n"]
            remaining = max(daily_limit - current_count, 0)
            if remaining == 0:
                return {"created": [], "message": "今日自主任务已达到上限", "daily_limit": daily_limit, "existing_count": current_count}
            topics = conn.execute(
                """SELECT topic.id FROM topics topic
                JOIN topic_scores score ON score.id = (SELECT id FROM topic_scores WHERE topic_id = topic.id ORDER BY scored_at DESC, id DESC LIMIT 1)
                WHERE topic.status != 'dropped'
                ORDER BY score.total_score DESC, topic.news_date DESC, topic.id ASC LIMIT ?""",
                (remaining,),
            ).fetchall()
        finally:
            conn.close()
        created = []
        for row in topics:
            result = create_task(row["id"], mode="autonomous", selected_note="自主模式按最新评分推荐派单", created_by="system")
            if result["created"]:
                created.append(result)
        return {"created": created, "message": f"已创建 {len(created)} 个自主创作任务", "daily_limit": daily_limit, "existing_count": current_count}


def sync_opc_performance() -> dict[str, Any]:
    """只读拉取 OPC-ERP 的内容表现，作为发布观察事实。"""
    base_url = get_setting("opc_erp_api_base", "").strip().rstrip("/")
    if not base_url:
        raise ValueError("尚未配置 OPC-ERP API 地址")
    response = httpx.get(f"{base_url}/api/content-performance", timeout=20)
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        raise ValueError("OPC-ERP 内容表现接口返回格式无效")
    conn = get_db()
    try:
        inserted = 0
        updated = 0
        timestamp = now_iso()
        for item in rows:
            publication_id = str(item.get("latest_publication_id") or "").strip()
            if not publication_id:
                continue
            metrics = {key: value for key, value in item.items() if key.startswith("latest_") or key.startswith("total_") or key.endswith("_delta") or key in ("snapshot_count", "captured_at")}
            existing = conn.execute("SELECT id FROM publication_observations WHERE opc_publication_id = ?", (publication_id,)).fetchone()
            if existing:
                conn.execute(
                    """UPDATE publication_observations SET title = ?, platform = ?, external_url = ?, published_at = ?,
                    captured_at = ?, metrics_json = ?, source_payload_json = ?, updated_at = ? WHERE opc_publication_id = ?""",
                    (item.get("title") or "未命名内容", item.get("platform"), item.get("external_url"), item.get("published_at"), item.get("captured_at"), _json(metrics), _json(item), timestamp, publication_id),
                )
                updated += 1
            else:
                conn.execute(
                    """INSERT INTO publication_observations
                    (id, opc_publication_id, opc_content_item_id, platform, title, external_url, published_at, captured_at,
                    metrics_json, status, source_payload_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'observed', ?, ?, ?)""",
                    (str(uuid.uuid4()), publication_id, item.get("content_item_id"), item.get("platform"), item.get("title") or "未命名内容", item.get("external_url"), item.get("published_at"), item.get("captured_at"), _json(metrics), _json(item), timestamp, timestamp),
                )
                inserted += 1
        conn.commit()
        return {"received": len(rows), "inserted": inserted, "updated": updated}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
