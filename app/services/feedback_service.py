"""将 OPC-ERP 发布观察转化为内容生产决策信号。

ERP 仍是指标事实源。本模块只读取 observation.metrics_json，生成带规则版本的
派生信号，并通过 observation_id 引用原始观察，避免建立第二份指标主数据。
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timedelta
from statistics import median
from typing import Any

from app.config import get_db
from app.services.production_service import now_iso, sync_opc_performance

RULE_VERSION = "feedback-v1"
def _load_metrics(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _primary_metric(platform: str | None, metrics: dict[str, Any]) -> tuple[str, float] | None:
    candidates = (
        ("wechat", "latest_read_count"),
        ("bilibili", "latest_play_count"),
        ("douyin", "latest_play_count"),
    )
    preferred = [key for item_platform, key in candidates if item_platform == (platform or "").lower()]
    preferred.extend(["latest_read_count", "latest_play_count", "latest_views", "latest_impressions"])
    for key in preferred:
        value = _number(metrics.get(key))
        if value is not None and value >= 0:
            return key, value
    return None


def _engagement_rate(metrics: dict[str, Any], primary_value: float) -> tuple[float, float] | None:
    if primary_value <= 0:
        return None
    keys = ("latest_like_count", "latest_comment_count", "latest_share_count", "latest_favorite_count", "latest_coin_count")
    total = sum((_number(metrics.get(key)) or 0) for key in keys)
    return total / primary_value, total


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def _display_number(value: float) -> str:
    return f"{value:,.0f}" if value >= 1 else f"{value:.2%}"


def _baseline(conn, platform: str | None) -> dict[str, Any]:
    cutoff = (datetime.now() - timedelta(days=90)).isoformat()
    rows = conn.execute(
        "SELECT metrics_json FROM publication_observations "
        "WHERE platform = ? AND COALESCE(published_at, created_at) >= ?",
        (platform, cutoff),
    ).fetchall()
    reach: list[float] = []
    engagement: list[float] = []
    for row in rows:
        metrics = _load_metrics(row["metrics_json"])
        primary = _primary_metric(platform, metrics)
        if not primary:
            continue
        reach.append(primary[1])
        rate = _engagement_rate(metrics, primary[1])
        if rate:
            engagement.append(rate[0])
    return {
        "sample_size": len(reach),
        "reach_median": median(reach) if reach else None,
        "reach_p25": _percentile(reach, 0.25),
        "reach_p75": _percentile(reach, 0.75),
        "engagement_p75": _percentile(engagement, 0.75),
    }


def _delta_signal(metrics: dict[str, Any]) -> tuple[str, float] | None:
    for key, value in metrics.items():
        if not key.endswith("_delta") or not any(token in key for token in ("read", "play", "view", "impression")):
            continue
        number = _number(value)
        if number is not None and number < 0:
            return key, number
    return None


def _signal_specs(observation: Any, baseline: dict[str, Any]) -> list[dict[str, Any]]:
    metrics = _load_metrics(observation["metrics_json"])
    primary = _primary_metric(observation["platform"], metrics)
    if not primary:
        return []
    metric_key, metric_value = primary
    sample_size = int(baseline["sample_size"] or 0)
    specs: list[dict[str, Any]] = []
    evidence_base = {
        "metric": metric_key,
        "value": metric_value,
        "sample_size": sample_size,
        "reach_median": baseline["reach_median"],
        "reach_p25": baseline["reach_p25"],
        "reach_p75": baseline["reach_p75"],
    }
    if sample_size >= 4 and baseline["reach_p75"] is not None and metric_value >= baseline["reach_p75"] and metric_value > 0:
        specs.append({
            "signal_type": "high_reach",
            "priority": "high",
            "headline": "高传播内容",
            "detail": f"{metric_key} 为 {_display_number(metric_value)}，位于 {observation['platform'] or '当前平台'} 近期高位。",
            "suggested_action": "追踪同一事件或观点，评估改编为视频/短帖。",
            "evidence": evidence_base,
        })
    if sample_size >= 4 and baseline["reach_p25"] is not None and metric_value <= baseline["reach_p25"]:
        specs.append({
            "signal_type": "low_reach",
            "priority": "medium",
            "headline": "低传播内容",
            "detail": f"{metric_key} 为 {_display_number(metric_value)}，位于 {observation['platform'] or '当前平台'} 近期低位。",
            "suggested_action": "复盘标题承诺、开头、选题时机和分发路径，不直接归因于正文质量。",
            "evidence": evidence_base,
        })
    rate = _engagement_rate(metrics, metric_value)
    if rate and sample_size >= 4 and baseline["engagement_p75"] is not None and rate[0] >= baseline["engagement_p75"] and rate[1] >= 3:
        specs.append({
            "signal_type": "high_engagement",
            "priority": "high",
            "headline": "高互动内容",
            "detail": f"互动 {rate[1]:,.0f}，互动率 {rate[0]:.2%}，位于近期高位。",
            "suggested_action": "提炼评论、分享或收藏触发点，作为后续标题和内容结构素材。",
            "evidence": {**evidence_base, "engagement_total": rate[1], "engagement_rate": rate[0], "engagement_p75": baseline["engagement_p75"]},
        })
    delta = _delta_signal(metrics)
    if delta:
        specs.append({
            "signal_type": "declining",
            "priority": "medium",
            "headline": "近期表现下降",
            "detail": f"ERP 提供的 {delta[0]} 为 {delta[1]:,.0f}。",
            "suggested_action": "判断是否需要追发、更新标题、补充分发或停止继续投入。",
            "evidence": {**evidence_base, "delta_metric": delta[0], "delta_value": delta[1]},
        })
    if any(spec["signal_type"] in {"high_reach", "high_engagement"} for spec in specs):
        specs.append({
            "signal_type": "repurpose_candidate",
            "priority": "high",
            "headline": "值得再分发",
            "detail": "该内容至少有一项触达或互动表现达到近期高位。",
            "suggested_action": "生成 B 站视频、短帖或后续文章的再分发任务。",
            "evidence": {**evidence_base, "trigger_signals": [spec["signal_type"] for spec in specs]},
        })
    return specs


def recompute_signals() -> dict[str, int]:
    conn = get_db()
    inserted = 0
    updated = 0
    try:
        rows = conn.execute(
            "SELECT * FROM publication_observations WHERE platform IS NOT NULL "
            "ORDER BY COALESCE(published_at, created_at) DESC"
        ).fetchall()
        for observation in rows:
            baseline = _baseline(conn, observation["platform"])
            for spec in _signal_specs(observation, baseline):
                timestamp = now_iso()
                evidence = json.dumps(spec["evidence"], ensure_ascii=False, sort_keys=True)
                existing = conn.execute(
                    "SELECT * FROM feedback_signals WHERE observation_id = ? AND signal_type = ? AND rule_version = ?",
                    (observation["id"], spec["signal_type"], RULE_VERSION),
                ).fetchone()
                if existing:
                    # 用户已处理的信号保留状态，只更新解释和证据。
                    conn.execute(
                        "UPDATE feedback_signals SET priority = ?, headline = ?, detail = ?, suggested_action = ?, evidence_json = ?, updated_at = ? WHERE id = ?",
                        (spec["priority"], spec["headline"], spec["detail"], spec["suggested_action"], evidence, timestamp, existing["id"]),
                    )
                    updated += 1
                else:
                    conn.execute(
                        """INSERT INTO feedback_signals
                        (id, observation_id, signal_type, priority, status, headline, detail, suggested_action,
                         evidence_json, rule_version, created_at, updated_at)
                        VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?)""",
                        (str(uuid.uuid4()), observation["id"], spec["signal_type"], spec["priority"], spec["headline"], spec["detail"], spec["suggested_action"], evidence, RULE_VERSION, timestamp, timestamp),
                    )
                    inserted += 1
        conn.commit()
        return {"observations": len(rows), "inserted": inserted, "updated": updated}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def sync_and_generate_feedback() -> dict[str, Any]:
    sync_result = sync_opc_performance()
    signal_result = recompute_signals()
    return {"sync": sync_result, "signals": signal_result}


def list_signals(status: str | None = None, platform: str | None = None, signal_type: str | None = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    conn = get_db()
    try:
        sql = """SELECT signal.*, observation.opc_publication_id, observation.platform,
                 observation.title AS publication_title, observation.external_url,
                 observation.published_at, observation.captured_at, observation.metrics_json,
                 observation.status AS observation_status, topic.title AS topic_title
                 FROM feedback_signals signal
                 JOIN publication_observations observation ON observation.id = signal.observation_id
                 LEFT JOIN topics topic ON topic.id = observation.topic_id
                 WHERE 1 = 1"""
        params: list[Any] = []
        if status:
            sql += " AND signal.status = ?"
            params.append(status)
        if platform:
            sql += " AND observation.platform = ?"
            params.append(platform)
        if signal_type:
            sql += " AND signal.signal_type = ?"
            params.append(signal_type)
        sql += " ORDER BY CASE signal.status WHEN 'open' THEN 0 ELSE 1 END, CASE signal.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, signal.updated_at DESC LIMIT ? OFFSET ?"
        params.extend([min(max(limit, 1), 500), max(offset, 0)])
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def signal_summary() -> dict[str, Any]:
    conn = get_db()
    try:
        counts = {row["status"]: row["count"] for row in conn.execute("SELECT status, COUNT(*) AS count FROM feedback_signals GROUP BY status").fetchall()}
        types = {row["signal_type"]: row["count"] for row in conn.execute("SELECT signal_type, COUNT(*) AS count FROM feedback_signals WHERE status = 'open' GROUP BY signal_type ORDER BY count DESC").fetchall()}
        last_sync = conn.execute("SELECT MAX(updated_at) AS value FROM publication_observations").fetchone()["value"]
        return {"counts": counts, "open_by_type": types, "last_observation_update": last_sync}
    finally:
        conn.close()


def handle_signal(signal_id: str, status: str, handled_by: str = "human", note: str = "") -> dict[str, Any]:
    if status not in {"accepted", "dismissed", "expired"}:
        raise ValueError("反馈信号状态无效")
    conn = get_db()
    try:
        signal = conn.execute("SELECT * FROM feedback_signals WHERE id = ?", (signal_id,)).fetchone()
        if not signal:
            raise ValueError("反馈信号不存在")
        timestamp = now_iso()
        conn.execute(
            "UPDATE feedback_signals SET status = ?, handled_by = ?, handling_note = ?, handled_at = ?, updated_at = ? WHERE id = ?",
            (status, handled_by, note, timestamp, timestamp, signal_id),
        )
        conn.commit()
        return {"signal_id": signal_id, "status": status}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_review_from_signal(signal_id: str, created_by: str = "feedback_layer") -> dict[str, Any]:
    conn = get_db()
    try:
        signal = conn.execute(
            "SELECT signal.*, observation.title, observation.task_id, observation.topic_id, observation.metrics_json "
            "FROM feedback_signals signal JOIN publication_observations observation ON observation.id = signal.observation_id "
            "WHERE signal.id = ?",
            (signal_id,),
        ).fetchone()
        if not signal:
            raise ValueError("反馈信号不存在")
        existing = conn.execute("SELECT id FROM review_cases WHERE observation_id = ? AND status != 'closed'", (signal["observation_id"],)).fetchone()
        if existing:
            review_id = existing["id"]
        else:
            review_id = str(uuid.uuid4())
            timestamp = now_iso()
            conn.execute(
                """INSERT INTO review_cases
                (id, observation_id, task_id, topic_id, status, trigger_type, trigger_reason,
                 title_snapshot, performance_snapshot_json, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'candidate', 'feedback_signal', ?, ?, ?, ?, ?, ?)""",
                (review_id, signal["observation_id"], signal["task_id"], signal["topic_id"], signal["headline"], signal["title"], signal["metrics_json"], created_by, timestamp, timestamp),
            )
        timestamp = now_iso()
        conn.execute("UPDATE feedback_signals SET status = 'accepted', handled_by = ?, handling_note = ?, handled_at = ?, updated_at = ? WHERE id = ?", (created_by, "已创建复盘案例", timestamp, timestamp, signal_id))
        conn.commit()
        return {"signal_id": signal_id, "review_case_id": review_id, "status": "accepted"}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

