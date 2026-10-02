"""统一内容运营主线的派生摘要。

这里不创建第二套业务状态，只把现有 Topic、ProductionTask、
PublicationObservation、ReviewCase 和运行组件汇总成首页可执行的下一步。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx

from app.config import get_db, get_setting
from app.services.file_watcher import watcher_status
from app.services.scheduler import scheduler_status


_ERP_HEALTH_CACHE: tuple[float, str, str] | None = None
_ERP_HEALTH_CACHE_SECONDS = 15.0


def _group_counts(conn, table: str) -> dict[str, int]:
    rows = conn.execute(f"SELECT status, COUNT(*) AS count FROM {table} GROUP BY status").fetchall()
    return {row["status"]: row["count"] for row in rows}


def _component(key: str, label: str, status: str, detail: str) -> dict[str, str]:
    return {"key": key, "label": label, "status": status, "detail": detail}


def _erp_health() -> tuple[str, str]:
    """区分 ERP 地址已配置和 ERP 接口当前可达，且避免页面刷新反复阻塞网络请求。"""
    global _ERP_HEALTH_CACHE
    erp_base = get_setting("opc_erp_api_base", "").strip().rstrip("/")
    if not erp_base:
        return "degraded", "未配置接口，发布表现不会自动回流"

    now = time.monotonic()
    if _ERP_HEALTH_CACHE and now - _ERP_HEALTH_CACHE[0] < _ERP_HEALTH_CACHE_SECONDS:
        return _ERP_HEALTH_CACHE[1], _ERP_HEALTH_CACHE[2]

    try:
        with httpx.Client(timeout=0.8, trust_env=False) as client:
            response = client.get(f"{erp_base}/api/health")
        if response.status_code >= 500:
            result = ("degraded", f"接口返回 HTTP {response.status_code}，反馈回流暂不可用")
        elif response.status_code >= 400:
            result = ("degraded", f"健康检查返回 HTTP {response.status_code}，请检查 ERP 服务")
        else:
            payload = response.json() if response.content else {}
            result = ("ready", "接口可达，发布表现可回流") if payload.get("status") != "degraded" else ("degraded", "接口可达但 ERP 自身处于降级状态")
    except (httpx.HTTPError, ValueError) as exc:
        result = ("degraded", f"接口不可达，反馈回流暂停：{exc}")

    _ERP_HEALTH_CACHE = (now, result[0], result[1])
    return result


def _health_snapshot(source_issues_open: int = 0) -> list[dict[str, str]]:
    watcher = watcher_status()
    scheduler = scheduler_status()
    llm_ready = bool(get_setting("llm_api_key", "").strip())
    vault = Path(get_setting("content_vault_dir", "").strip())
    skills = Path(get_setting("content_skills_dir", "").strip())
    contract_dir = Path(get_setting("task_contract_dir", "").strip())

    components = [
        _component("openclaw", "资料监听", watcher.get("status", "degraded"), watcher.get("message") or watcher.get("watch_dir") or "正常监听"),
        _component("scheduler", "自动调度", scheduler.get("status", "degraded"), f"已加载 {scheduler.get('jobs', 0)} 项任务" if scheduler.get("status") == "running" else scheduler.get("message", "调度器未运行")),
        _component("llm", "AI 评分", "ready" if llm_ready else "degraded", "已配置模型凭证" if llm_ready else "未配置凭证，待评分选题需 Agent/API 处理"),
        _component("erp", "ERP 回流", *_erp_health()),
        _component("obsidian", "内容产物", "ready" if vault.is_dir() and skills.is_dir() else "degraded", "项目与 Skill 路径可用" if vault.is_dir() and skills.is_dir() else "请配置内容 vault 和 Skill 目录"),
        _component("contracts", "任务契约", "ready" if contract_dir.is_dir() else "degraded", str(contract_dir) if contract_dir.is_dir() else "契约目录未配置或不存在"),
    ]
    if source_issues_open:
        components.append(_component("source_issues", "资料异常", "degraded", f"有 {source_issues_open} 个来源未处理，可查看 /api/source-issues"))
    return components


def _next_action(topic_counts: dict[str, int], task_counts: dict[str, int], observation_counts: dict[str, int], review_counts: dict[str, int], feedback_open: int, source_issues_open: int = 0) -> dict[str, str]:
    """下一步只讲素材这件事：先补齐评分，再按分挑题。

    创作任务/发布回流属于本平台之外的流程（周老师 2026-10-02 定：平台只做选题供给），
    所以不再拿它们当首页待办，免得把人引到已经不用的地方。
    """
    if source_issues_open:
        return {"key": "source_issue", "title": "先处理资料采集异常", "detail": f"有 {source_issues_open} 个来源没有进入素材池，先检查异常再判断素材池是否完整。", "href": "/api/source-issues?status=open", "label": "查看采集异常"}
    if topic_counts.get("unscored", 0):
        if get_setting("llm_api_key", "").strip():
            return {"key": "scoring", "title": "有素材等待评分", "detail": f"还有 {topic_counts['unscored']} 条素材没有评分，系统会按节奏补齐。", "href": "/?status=unscored", "label": "查看待评分"}
        return {"key": "unscored", "title": "有素材等待评分", "detail": f"有 {topic_counts['unscored']} 条素材还没有评分，当前未配置模型凭证，需要 Agent 评分。", "href": "/?status=unscored", "label": "查看待评分"}
    if topic_counts.get("scored", 0):
        return {"key": "pick", "title": "去挑高分素材", "detail": f"素材池已有 {topic_counts['scored']} 条评分，按分数从高到低挑就行。", "href": "/?sort=score", "label": "按评分挑题"}
    return {"key": "wait", "title": "素材池已就绪", "detail": "系统会继续监听资料、运行调度，把新素材自动收进来并评分。", "href": "/", "label": "查看素材池"}


def get_operations_overview() -> dict[str, Any]:
    conn = get_db()
    try:
        usage_counts = _group_counts(conn, "topics")
        total_topics = conn.execute("SELECT COUNT(*) AS count FROM topics").fetchone()["count"]
        scored_topics = conn.execute(
            "SELECT COUNT(*) AS count FROM topics t "
            "WHERE EXISTS (SELECT 1 FROM topic_scores s WHERE s.topic_id = t.id)"
        ).fetchone()["count"]
        merged_threads = conn.execute(
            "SELECT COUNT(DISTINCT event_key) AS count FROM topics WHERE COALESCE(event_key, '') != ''"
        ).fetchone()["count"]
        # topic_counts 的键是"素材视角"：待评分/已评分是事实，未用/已用/弃用是使用状态。
        topic_counts = {
            "unscored": total_topics - scored_topics,
            "scored": scored_topics,
            "unused": usage_counts.get("unused", 0),
            "used": usage_counts.get("used", 0),
            "dropped": usage_counts.get("dropped", 0),
        }
        topic_counts["total"] = total_topics
        task_counts = _group_counts(conn, "production_tasks")
        observation_counts = _group_counts(conn, "publication_observations")
        review_counts = _group_counts(conn, "review_cases")
        feedback_open = conn.execute("SELECT COUNT(*) AS count FROM feedback_signals WHERE status = 'open'").fetchone()["count"]
        source_issues_open = conn.execute("SELECT COUNT(*) AS count FROM source_issues WHERE status = 'open'").fetchone()["count"]
        last_observation = conn.execute("SELECT MAX(updated_at) AS value FROM publication_observations").fetchone()["value"]
        last_cycle_row = conn.execute(
            "SELECT id, status, summary, result_json, created_at, completed_at FROM operator_cycles ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        operator_cycle = {"status": "none", "summary": "尚未运行 Codex 运营周期", "created_at": None, "completed_at": None, "id": None, "counts": {"collector": 0, "editorial": 0, "production": 0}}
        if last_cycle_row:
            try:
                result = json.loads(last_cycle_row["result_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                result = {}
            operator_cycle = {
                "id": last_cycle_row["id"],
                "status": last_cycle_row["status"],
                "summary": last_cycle_row["summary"] or "已完成运营判断",
                "created_at": last_cycle_row["created_at"],
                "completed_at": last_cycle_row["completed_at"],
                "counts": {
                    "collector": len(result.get("collector_adjustments") or []),
                    "editorial": len([item for item in (result.get("editorial_proposals") or []) if item.get("status") == "created"]),
                    "production": len([item for item in (result.get("production") or []) if item.get("status") == "queued"]),
                },
            }

        pipeline = [
            {"key": "unscored", "label": "待评分", "count": topic_counts["unscored"], "href": "/?status=unscored", "tone": "gray"},
            {"key": "scored", "label": "已评分", "count": topic_counts["scored"], "href": "/?status=scored", "tone": "green"},
            {"key": "unused", "label": "未用", "count": topic_counts["unused"], "href": "/?status=unused", "tone": "indigo"},
            {"key": "used", "label": "已用", "count": topic_counts["used"], "href": "/?status=used", "tone": "blue"},
            {"key": "dropped", "label": "弃用", "count": topic_counts["dropped"], "href": "/?status=dropped", "tone": "slate"},
            {"key": "merged", "label": "归类线", "count": merged_threads, "href": "/tree", "tone": "amber"},
        ]
        return {
            "counts": {"topics": total_topics, "tasks": sum(task_counts.values()), "observations": sum(observation_counts.values()), "reviews": sum(review_counts.values()), "feedback_open": feedback_open, "source_issues_open": source_issues_open},
            "topic_counts": topic_counts,
            "task_counts": task_counts,
            "observation_counts": observation_counts,
            "review_counts": review_counts,
            "pipeline": pipeline,
            "next_action": _next_action(topic_counts, task_counts, observation_counts, review_counts, feedback_open, source_issues_open),
            "health": _health_snapshot(source_issues_open),
            "last_observation_update": last_observation,
            "operator_cycle": operator_cycle,
        }
    finally:
        conn.close()
