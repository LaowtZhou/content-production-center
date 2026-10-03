"""Codex 运营闭环：把表现反馈转成采集调整和主编选题。

ERP 指标仍是事实源，OpenClaw 仍是原始资料采集器。这里保存的是 Codex
作为运营角色作出的可审计决策，不把派生判断伪装成平台事实。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import get_db, get_int_setting, get_setting
from app.services import feedback_service


RULE_VERSION = "operator-loop-v1"
ALLOWED_COLLECTOR_JOBS = {
    "AI日报每日推送",
    "B站视频数据周更",
    "公众号数据周更",
}
DISALLOWED_INSTRUCTION_TOKENS = (
    "powershell",
    "invoke-restmethod",
    "curl ",
    "http://",
    "https://",
    "token",
    "cookie",
    "password",
    "rm -",
    "删除文件",
    "发送消息",
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _now() -> str:
    return datetime.now().isoformat()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _jobs_path() -> Path:
    configured = get_setting("openclaw_cron_jobs_path", "").strip()
    if configured:
        return Path(configured).expanduser()
    env_path = os.environ.get("OPENCLAW_CRON_JOBS_PATH", "").strip()
    if env_path:
        return Path(env_path).expanduser()
    return Path.home() / ".openclaw" / "cron" / "jobs.json"


def _slug(job_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", job_name.lower()).strip("-") or "collector"


def _directive_markers(job_name: str) -> tuple[str, str]:
    marker = f"content-production-center:collector-directive:{_slug(job_name)}"
    return f"<!-- {marker} -->", f"<!-- {marker}:end -->"


def _managed_instruction(message: str, job_name: str) -> str:
    start, end = _directive_markers(job_name)
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    block = f"{start}\n{message.strip()}\n{end}"
    if pattern.search(message):
        return pattern.sub(block, message)
    return f"{message.rstrip()}\n\n{block}"


def _validate_adjustment(item: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(item, dict):
        return None, "采集调整必须是对象"
    job_name = str(item.get("job_name") or "").strip()
    instruction = str(item.get("instruction") or "").strip()
    if job_name not in ALLOWED_COLLECTOR_JOBS:
        return None, f"不允许调整采集任务：{job_name or '未填写'}"
    if not instruction:
        return None, "采集调整指令不能为空"
    if len(instruction) > 1200:
        return None, "采集调整指令不能超过 1200 字"
    if any(ord(char) < 32 and char not in "\n\t" for char in instruction):
        return None, "采集调整指令包含不可用控制字符"
    lowered = instruction.lower()
    hit = next((token for token in DISALLOWED_INSTRUCTION_TOKENS if token in lowered), None)
    if hit:
        return None, f"采集调整指令包含受限内容：{hit}"
    return {
        "job_name": job_name,
        "instruction": instruction,
        "reason": str(item.get("reason") or "").strip(),
        "evidence_ids": item.get("evidence_ids") if isinstance(item.get("evidence_ids"), list) else [],
    }, None


def _collector_snapshot() -> dict[str, Any]:
    path = _jobs_path()
    if not path.is_file():
        return {"path": str(path), "status": "missing", "jobs": [], "error": "OpenClaw jobs.json 不存在"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {"path": str(path), "status": "invalid", "jobs": [], "error": str(exc)}
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        return {"path": str(path), "status": "invalid", "jobs": [], "error": "jobs.json 缺少 jobs 数组"}
    result = []
    for job in jobs:
        if not isinstance(job, dict) or job.get("name") not in ALLOWED_COLLECTOR_JOBS:
            continue
        message = str((job.get("payload") or {}).get("message") or "")
        start, end = _directive_markers(str(job.get("name")))
        managed = message.split(start, 1)[1].split(end, 1)[0].strip() if start in message and end in message else ""
        result.append({
            "name": job.get("name"),
            "enabled": bool(job.get("enabled", False)),
            "schedule": job.get("schedule") or {},
            "managed_directive": managed,
            "message_hash": _hash(message),
        })
    return {"path": str(path), "status": "ready", "jobs": result}


def get_collector_jobs() -> dict[str, Any]:
    """返回采集任务摘要，供设置页或执行器做只读检查。"""
    return _collector_snapshot()


def get_operator_context(limit: int = 20) -> dict[str, Any]:
    """返回 Codex 作运营判断所需的最小上下文，不复制 ERP 原始数据库。"""
    conn = get_db()
    try:
        observations = [dict(row) for row in conn.execute(
            "SELECT id, opc_publication_id, platform, title, published_at, captured_at, metrics_json, status "
            "FROM publication_observations ORDER BY COALESCE(published_at, created_at) DESC LIMIT ?",
            (min(max(limit, 1), 100),),
        ).fetchall()]
        recent_topics = [dict(row) for row in conn.execute(
            "SELECT topic.id, topic.title, topic.summary, topic.source_type, topic.status, topic.created_at, "
            "score.total_score, score.recommended_format, score.key_angle "
            "FROM topics topic LEFT JOIN topic_scores score ON score.id = ("
            "SELECT id FROM topic_scores WHERE topic_id = topic.id ORDER BY scored_at DESC, id DESC LIMIT 1) "
            "ORDER BY topic.created_at DESC LIMIT ?",
            (min(max(limit, 1), 100),),
        ).fetchall()]
        tasks = [dict(row) for row in conn.execute(
            "SELECT task.id, task.status, task.mode, task.requested_platform, task.created_at, topic.title AS topic_title "
            "FROM production_tasks task JOIN topics topic ON topic.id = task.topic_id "
            "ORDER BY task.created_at DESC LIMIT ?",
            (min(max(limit, 1), 100),),
        ).fetchall()]
        cycles = [dict(row) for row in conn.execute(
            "SELECT id, status, summary, created_at, completed_at, result_json FROM operator_cycles "
            "ORDER BY created_at DESC LIMIT 10"
        ).fetchall()]
    finally:
        conn.close()
    return {
        "rule_version": RULE_VERSION,
        "production_mode": get_setting("production_mode", "manual"),
        "daily_task_limit": get_int_setting("daily_task_limit", 1),
        "feedback_signals": feedback_service.list_signals(status="open", limit=min(max(limit, 1), 100)),
        "observations": observations,
        "recent_topics": recent_topics,
        "recent_tasks": tasks,
        "recent_operator_cycles": cycles,
        "collector_jobs": _collector_snapshot(),
        "guardrails": {
            "max_collector_adjustments_per_cycle": 3,
            "max_editorial_proposals_per_cycle": 1,
            "no_external_publish": True,
            "no_erp_write": True,
            "formal_skill_update_requires_human": True,
        },
    }


def _insert_directive_audit(conn, cycle_id: str, item: dict[str, Any], status: str, **extra: Any) -> str:
    directive_id = str(uuid.uuid4())
    timestamp = _now()
    conn.execute(
        """INSERT INTO collector_directives
        (id, cycle_id, job_name, instruction, instruction_hash, status, reason, evidence_json,
         before_hash, after_hash, backup_path, error_detail, created_at, applied_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (directive_id, cycle_id, item["job_name"], item["instruction"], _hash(item["instruction"]), status,
         item.get("reason", ""), _json(item.get("evidence_ids", [])), extra.get("before_hash"),
         extra.get("after_hash"), extra.get("backup_path"), extra.get("error_detail"), timestamp,
         timestamp if status == "applied" else None),
    )
    return directive_id


def _apply_collector_adjustments(conn, cycle_id: str, raw_items: Any) -> list[dict[str, Any]]:
    items = raw_items if isinstance(raw_items, list) else []
    if len(items) > 3:
        items = items[:3]
    prepared: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for raw in items:
        item, error = _validate_adjustment(raw)
        if error:
            results.append({"status": "blocked", "job_name": raw.get("job_name") if isinstance(raw, dict) else "", "error": error})
        else:
            prepared.append(item)
    if not prepared:
        for result in results:
            item = {"job_name": result.get("job_name", ""), "instruction": "", "reason": "", "evidence_ids": []}
            _insert_directive_audit(conn, cycle_id, item, "blocked", error_detail=result["error"])
        return results

    path = _jobs_path()
    if not path.is_file():
        for item in prepared:
            _insert_directive_audit(conn, cycle_id, item, "blocked", error_detail=f"文件不存在：{path}")
            results.append({"status": "blocked", "job_name": item["job_name"], "error": f"文件不存在：{path}"})
        return results
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            raise ValueError("jobs.json 缺少 jobs 数组")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        for item in prepared:
            _insert_directive_audit(conn, cycle_id, item, "blocked", error_detail=str(exc))
            results.append({"status": "blocked", "job_name": item["job_name"], "error": str(exc)})
        return results

    by_name = {job.get("name"): job for job in jobs if isinstance(job, dict)}
    changes: list[tuple[dict[str, Any], str, str]] = []
    for item in prepared:
        job = by_name.get(item["job_name"])
        message = str((job or {}).get("payload", {}).get("message") or "") if job else ""
        if not job or not isinstance(job.get("payload"), dict) or not isinstance(job["payload"].get("message"), str):
            _insert_directive_audit(conn, cycle_id, item, "blocked", error_detail="采集任务不存在或缺少 payload.message")
            results.append({"status": "blocked", "job_name": item["job_name"], "error": "采集任务不存在或缺少 payload.message"})
            continue
        start, end = _directive_markers(item["job_name"])
        block = f"{start}\n{item['instruction']}\n{end}"
        if start in message and end in message:
            pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
            new_message = pattern.sub(block, message)
        else:
            new_message = f"{message.rstrip()}\n\n{block}"
        before_hash = _hash(message)
        after_hash = _hash(new_message)
        if before_hash == after_hash:
            _insert_directive_audit(conn, cycle_id, item, "skipped", before_hash=before_hash, after_hash=after_hash)
            results.append({"status": "skipped", "job_name": item["job_name"], "message_hash": after_hash})
            continue
        changes.append((job, before_hash, new_message))

    if not changes:
        return results

    backup_path = path.with_name(f"{path.name}.bak-content-center-{uuid.uuid4().hex[:10]}")
    try:
        shutil.copy2(path, backup_path)
        for job, _, new_message in changes:
            job["payload"]["message"] = new_message
        payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False, suffix=".tmp") as handle:
            handle.write(payload)
            temp_path = Path(handle.name)
        os.replace(temp_path, path)
    except (OSError, TypeError, ValueError) as exc:
        for job, _, _ in changes:
            item = next(item for item in prepared if item["job_name"] == job.get("name"))
            _insert_directive_audit(conn, cycle_id, item, "blocked", error_detail=f"写入 OpenClaw jobs.json 失败：{exc}", backup_path=str(backup_path))
            results.append({"status": "blocked", "job_name": item["job_name"], "error": str(exc)})
        return results

    for job, before_hash, new_message in changes:
        item = next(item for item in prepared if item["job_name"] == job.get("name"))
        after_hash = _hash(new_message)
        _insert_directive_audit(conn, cycle_id, item, "applied", before_hash=before_hash, after_hash=after_hash, backup_path=str(backup_path))
        results.append({"status": "applied", "job_name": item["job_name"], "before_hash": before_hash, "after_hash": after_hash, "backup_path": str(backup_path)})
    return results


def _create_editorial_topic(conn, cycle_id: str, proposal: dict[str, Any]) -> dict[str, Any]:
    title = str(proposal.get("title") or "").strip()
    summary = str(proposal.get("summary") or "").strip()
    raw_content = str(proposal.get("raw_content") or "").strip()
    if not title or not summary or not raw_content:
        return {"status": "blocked", "error": "主动选题必须包含 title、summary、raw_content"}
    if len(title) > 200 or len(raw_content) > 12000:
        return {"status": "blocked", "error": "主动选题标题或证据包超出长度限制"}
    score = proposal.get("score") if isinstance(proposal.get("score"), dict) else None
    if score:
        required = ("traffic_score", "opinion_score", "novelty_score")
        if not all(isinstance(score.get(field), (int, float)) and 0 <= float(score[field]) <= 10 for field in required):
            return {"status": "blocked", "error": "评分必须是 0-10 数字"}
    duplicate = conn.execute(
        "SELECT id, status FROM topics WHERE title = ? AND status != 'dropped' ORDER BY id DESC LIMIT 1",
        (title,),
    ).fetchone()
    if duplicate:
        return {"status": "skipped", "topic_id": duplicate["id"], "reason": "已有同名有效选题"}
    timestamp = _now()
    source_ref = str(proposal.get("source_ref") or f"operator-cycle:{cycle_id}").strip()
    tags = proposal.get("tags") if isinstance(proposal.get("tags"), list) else []
    tags = [str(tag).strip() for tag in tags if str(tag).strip()][:12]
    if "主编主动选题" not in tags:
        tags.append("主编主动选题")
    from app.taxonomy import classify
    from app.geo import judge as judge_geo
    category = classify(title, summary)
    region, entity, _kind = judge_geo(title, raw_content or summary)
    cur = conn.execute(
        """INSERT INTO topics (title, summary, raw_content, source_type, source_ref, tags, status,
        category, region, entity, created_at, updated_at)
        VALUES (?, ?, ?, 'codex_operator', ?, ?, 'unused', ?, ?, ?, ?, ?)""",
        (title, summary, raw_content, source_ref, _json(tags), category, region, entity, timestamp, timestamp),
    )
    topic_id = cur.lastrowid
    conn.execute(
        "INSERT INTO topic_events (topic_id, event_type, metadata) VALUES (?, 'operator_proposed', ?)",
        (topic_id, _json({"cycle_id": cycle_id, "reason": proposal.get("reason", ""), "evidence_ids": proposal.get("evidence_ids", [])})),
    )
    conn.execute(
        "INSERT INTO agent_activities (agent_name, activity_type, target_type, target_id, summary, metadata) VALUES ('Codex', 'operator_proposed', 'topic', ?, ?, ?)",
        (topic_id, f"主编主动发起选题：{title[:80]}", _json({"cycle_id": cycle_id})),
    )

    score_result = None
    if score:
        required = ("traffic_score", "opinion_score", "novelty_score")
        from app.services.scoring_engine import _save_score
        score_result = {
            "traffic_score": float(score["traffic_score"]),
            "opinion_score": float(score["opinion_score"]),
            "novelty_score": float(score["novelty_score"]),
            "traffic_reasoning": str(score.get("traffic_reasoning") or ""),
            "opinion_reasoning": str(score.get("opinion_reasoning") or ""),
            "novelty_reasoning": str(score.get("novelty_reasoning") or ""),
            "recommended_format": str(score.get("recommended_format") or "article"),
            "format_reasoning": str(score.get("format_reasoning") or ""),
            "key_angle": str(score.get("key_angle") or ""),
        }
        _save_score(conn, topic_id, score_result, "Codex-Operator")
    return {"status": "created", "topic_id": topic_id, "score": score_result, "start_production": bool(proposal.get("start_production"))}


def apply_operator_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """幂等应用一轮 Codex 运营计划。"""
    if not isinstance(plan, dict):
        raise ValueError("运营计划必须是 JSON 对象")
    cycle_id = str(plan.get("cycle_id") or uuid.uuid4()).strip()
    collector_adjustments = plan.get("collector_adjustments") if isinstance(plan.get("collector_adjustments"), list) else []
    editorial_proposals = plan.get("editorial_proposals") if isinstance(plan.get("editorial_proposals"), list) else []
    if len(collector_adjustments) > 3:
        raise ValueError("每个运营周期最多调整 3 个采集任务")
    if len(editorial_proposals) > 1:
        raise ValueError("每个运营周期最多主动发起 1 个选题")
    conn = get_db()
    try:
        existing = conn.execute("SELECT status, result_json FROM operator_cycles WHERE id = ?", (cycle_id,)).fetchone()
        if existing and existing["status"] != "running":
            return {"cycle_id": cycle_id, "status": existing["status"], "result": json.loads(existing["result_json"] or "{}")}
        if not existing:
            timestamp = _now()
            conn.execute(
                """INSERT INTO operator_cycles (id, status, trigger_type, summary, basis_json, plan_json, created_at, updated_at)
                VALUES (?, 'running', ?, ?, ?, ?, ?, ?)""",
                (cycle_id, str(plan.get("trigger_type") or "scheduled"), str(plan.get("summary") or ""),
                 _json(plan.get("basis") if isinstance(plan.get("basis"), list) else []), _json(plan), timestamp, timestamp),
            )
        collector_results = _apply_collector_adjustments(conn, cycle_id, collector_adjustments)
        proposals = editorial_proposals
        editorial_results = [_create_editorial_topic(conn, cycle_id, proposal) for proposal in proposals if isinstance(proposal, dict)]
        conn.commit()
    except Exception as exc:
        conn.rollback()
        timestamp = _now()
        conn.execute("UPDATE operator_cycles SET status = 'failed', error_detail = ?, updated_at = ?, completed_at = ? WHERE id = ?", (str(exc), timestamp, timestamp, cycle_id))
        conn.commit()
        raise
    finally:
        conn.close()

    # 评分后进入现有生产队列；不绕过 production_mode 和每日上限。
    production_results = []
    for result, proposal in zip(editorial_results, proposals):
        if result.get("status") != "created" or not result.get("start_production") or not result.get("topic_id"):
            continue
        score = result.get("score") or {}
        total = float(score.get("traffic_score", 0)) * 0.4 + float(score.get("opinion_score", 0)) * 0.4 + float(score.get("novelty_score", 0)) * 0.2
        threshold = 7.0
        if total < threshold:
            production_results.append({"topic_id": result["topic_id"], "status": "held", "reason": f"综合评分 {total:.2f} 低于主动生产门槛 {threshold:.1f}"})
            continue
        if get_setting("production_mode", "manual") != "autonomous":
            production_results.append({"topic_id": result["topic_id"], "status": "held", "reason": "当前为人工选择模式"})
            continue
        try:
            from app.services.production_service import create_task
            created = create_task(result["topic_id"], mode="autonomous", selected_note=f"Codex 主编主动选题，运营周期 {cycle_id}", created_by="Codex")
            production_results.append({"topic_id": result["topic_id"], "status": "queued", "task_id": created["task"]["id"]})
        except Exception as exc:
            production_results.append({"topic_id": result["topic_id"], "status": "blocked", "error": str(exc)})

    final_status = "completed"
    all_results = collector_results + editorial_results + production_results
    if any(item.get("status") == "blocked" for item in all_results):
        final_status = "partial" if any(item.get("status") in {"applied", "created", "queued"} for item in all_results) else "blocked"
    result_payload = {"collector_adjustments": collector_results, "editorial_proposals": editorial_results, "production": production_results}
    conn = get_db()
    try:
        timestamp = _now()
        conn.execute(
            "UPDATE operator_cycles SET status = ?, result_json = ?, updated_at = ?, completed_at = ? WHERE id = ?",
            (final_status, _json(result_payload), timestamp, timestamp, cycle_id),
        )
        conn.execute(
            "INSERT INTO agent_activities (agent_name, activity_type, target_type, target_id, summary, metadata) VALUES ('Codex', 'operator_cycle', 'operator_cycle', ?, ?, ?)",
            (cycle_id, f"运营周期{final_status}：{str(plan.get('summary') or '完成运营判断')[:100]}", _json(result_payload)),
        )
        conn.commit()
    finally:
        conn.close()
    return {"cycle_id": cycle_id, "status": final_status, "result": result_payload}
