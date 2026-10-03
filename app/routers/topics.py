"""选题 API - CRUD、状态流转、评分触发"""

import json
from datetime import datetime
from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel
from typing import Any, Optional
import math

from app.config import get_db, get_float_setting
from app.taxonomy import CATEGORIES, is_valid_category
from app.statuses import (
    DEFAULT_USAGE,
    USAGE_LABELS,
    USAGE_STATUSES,
    is_valid_period,
    period_start,
)

router = APIRouter()


def _has_score(conn, topic_id: int) -> bool:
    """是否已评分 = topic_scores 里有没有记录。这是事实，不是状态。"""
    row = conn.execute(
        "SELECT 1 FROM topic_scores WHERE topic_id = ? LIMIT 1", (topic_id,)
    ).fetchone()
    return row is not None


class TopicCreate(BaseModel):
    title: str
    summary: str = ""
    raw_content: str = ""
    source_type: str = "manual"
    source_ref: str = ""
    tags: list = []
    # 来源元数据：不传则留空。这些字段此前未声明，导致所有非 OpenClaw 入口
    # （脚本导入、手动录入页）传入的日期/来源被 Pydantic 静默丢弃，只能事后手工补。
    news_date: Optional[str] = None
    published_date: Optional[str] = None
    source_name: Optional[str] = None
    source_level: Optional[str] = None
    source_author: Optional[str] = None
    original_url: Optional[str] = None
    # 类别：不传则按标题/摘要自动归类（7 类固定清单，见 app/taxonomy.py）。
    category: Optional[str] = None


class TopicSelect(BaseModel):
    note: str = ""


class TopicAbandon(BaseModel):
    reason: str


class ScoreSubmission(BaseModel):
    """Agent直接提交评分的结构"""
    traffic_score: float          # 0-10
    opinion_score: float         # 0-10
    novelty_score: float         # 0-10
    traffic_reasoning: str = ""
    opinion_reasoning: str = ""
    novelty_reasoning: str = ""
    recommended_format: str = "undecided"   # video / article / xiaohongshu
    format_reasoning: str = ""
    key_angle: str = ""
    model: str = "agent"         # 评分者标识


class BatchScoreItem(BaseModel):
    topic_id: int
    traffic_score: float
    opinion_score: float
    novelty_score: float
    traffic_reasoning: str = ""
    opinion_reasoning: str = ""
    novelty_reasoning: str = ""
    recommended_format: str = "undecided"
    format_reasoning: str = ""
    key_angle: str = ""
    model: str = "agent"


class BatchScoreSubmission(BaseModel):
    scores: list[BatchScoreItem]


def _normalise_agent_scores(payload: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把 Agent 常见的评分返回形态收敛成统一记录，并保留逐条错误。"""
    if isinstance(payload, list):
        raw_items = payload
    elif isinstance(payload, dict):
        raw_items = payload.get("scores")
        if raw_items is None:
            raw_items = payload.get("scored_topics") or payload.get("results") or payload.get("pending_topics")
    else:
        raw_items = None

    if isinstance(raw_items, dict):
        raw_items = list(raw_items.values())
    if not isinstance(raw_items, list):
        return [], [{"index": None, "error": "请求必须包含 scores 数组"}]

    normalised = []
    errors = []
    aliases = {
        "traffic_score": ("traffic_score", "traffic"),
        "opinion_score": ("opinion_score", "opinion"),
        "novelty_score": ("novelty_score", "novelty"),
    }
    for index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, dict):
            errors.append({"index": index, "error": "评分项必须是对象"})
            continue
        nested_score = raw_item.get("score") if isinstance(raw_item.get("score"), dict) else {}
        item = {**raw_item, **nested_score}
        topic_id = item.get("topic_id", item.get("id"))
        item_errors = []
        try:
            topic_id = int(topic_id)
        except (TypeError, ValueError):
            item_errors.append("topic_id 必须是整数")

        values = {}
        for field, field_aliases in aliases.items():
            value = next((item.get(key) for key in field_aliases if item.get(key) is not None), None)
            try:
                value = float(value)
                if not math.isfinite(value) or not 0 <= value <= 10:
                    raise ValueError
            except (TypeError, ValueError):
                item_errors.append(f"{field} 必须是 0-10 的数字")
            else:
                values[field] = value

        if item_errors:
            errors.append({"index": index, "topic_id": topic_id, "error": "；".join(item_errors)})
            continue

        normalised.append({
            "topic_id": topic_id,
            **values,
            "traffic_reasoning": str(item.get("traffic_reasoning") or ""),
            "opinion_reasoning": str(item.get("opinion_reasoning") or ""),
            "novelty_reasoning": str(item.get("novelty_reasoning") or ""),
            "recommended_format": str(item.get("recommended_format") or "undecided"),
            "format_reasoning": str(item.get("format_reasoning") or ""),
            "key_angle": str(item.get("key_angle") or ""),
            "model": str(item.get("model") or "OpenClaw"),
        })
    return normalised, errors


class BatchSelect(BaseModel):
    topic_ids: list[int]
    note: str = ""


class BatchAbandon(BaseModel):
    topic_ids: list[int]
    reason: str


class StatusUpdate(BaseModel):
    status: str


class UsageUpdate(BaseModel):
    """素材使用状态：unused 未用 / used 已用 / dropped 弃用。"""
    status: str
    note: str = ""


class BatchUsage(BaseModel):
    topic_ids: list[int]
    status: str


@router.get("/topics")
def list_topics(
    status: Optional[str] = None,
    source_type: Optional[str] = None,
    news_date: Optional[str] = None,
    source_name: Optional[str] = None,
    category: Optional[str] = None,
    min_score: Optional[float] = None,
    max_score: Optional[float] = None,
    order: Optional[str] = None,
    limit: int = Query(100, le=500),
    offset: int = 0,
):
    """获取选题列表，支持按日期、来源、使用状态、是否已评分、类别、分数筛选。

    status 取值：
    - 使用状态 unused / used / dropped（未用 / 已用 / 弃用）
    - 事实筛选 unscored / scored（按 topic_scores 有无评分记录）

    order='score' 时按评分从高到低排（无分的排最后），否则维持日期倒序。
    """
    conn = get_db()
    query = """
        SELECT t.*, s.total_score, s.traffic_score, s.opinion_score, s.novelty_score
        FROM topics t
        LEFT JOIN topic_scores s ON s.id = (
            SELECT id FROM topic_scores WHERE topic_id = t.id ORDER BY scored_at DESC LIMIT 1
        )
        WHERE 1=1
    """
    params = []
    if status == "unscored":
        query += " AND s.total_score IS NULL"
    elif status == "scored":
        query += " AND s.total_score IS NOT NULL"
    elif status in USAGE_STATUSES:
        query += " AND t.status = ?"
        params.append(status)
    elif status:
        raise HTTPException(
            status_code=400,
            detail=f"非法状态「{status}」。只允许：{'、'.join(USAGE_STATUSES)}、unscored、scored",
        )
    if source_type:
        query += " AND t.source_type = ?"
        params.append(source_type)
    if news_date:
        query += " AND t.news_date = ?"
        params.append(news_date)
    if source_name:
        query += " AND t.source_name = ?"
        params.append(source_name)
    if category:
        query += " AND t.category = ?"
        params.append(category)
    if min_score is not None:
        query += " AND s.total_score >= ?"
        params.append(min_score)
    if max_score is not None:
        query += " AND s.total_score <= ?"
        params.append(max_score)
    if order == "score":
        # 有分的在前、按分降序；无分的在后、按日期倒序
        query += " ORDER BY (s.total_score IS NULL) ASC, s.total_score DESC, t.news_date DESC, t.id DESC"
    else:
        query += " ORDER BY t.news_date DESC, t.id DESC"
    query += " LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.get("/categories")
def category_stats(news_date: Optional[str] = None, period: Optional[str] = None):
    """类别统计：每个类别在指定周期内的条数 + 已评分条数 + 最高分 + 全库总数。

    周老师的判断是两层：先看"哪类内容多"决定写哪个方向，再挑高分。
    只给条数会误导（某类虽多但都低分），所以一并给"该类最高分"。
    period 见 app/statuses.py：7d / 30d / quarter / year / all。
    """
    conn = get_db()
    sql = """
        SELECT t.category AS category,
               COUNT(*) AS total,
               SUM(CASE WHEN s.total_score IS NOT NULL THEN 1 ELSE 0 END) AS scored,
               MAX(s.total_score) AS max_score
        FROM topics t
        LEFT JOIN topic_scores s ON s.id = (
            SELECT id FROM topic_scores WHERE topic_id = t.id ORDER BY scored_at DESC LIMIT 1
        )
    """
    params: list = []
    conditions = []
    if news_date:
        conditions.append("t.news_date = ?")
        params.append(news_date)
    if period:
        start = period_start(period)
        if start:
            conditions.append("t.news_date >= ?")
            params.append(start)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " GROUP BY t.category"
    rows = conn.execute(sql, params).fetchall()

    lib_counts = {
        (row["category"] or ""): int(row["n"])
        for row in conn.execute("SELECT category, COUNT(*) AS n FROM topics GROUP BY category")
    }
    conn.close()

    by_cat = {r["category"] or "": r for r in rows}
    out = []
    for c in CATEGORIES:
        r = by_cat.get(c["name"])
        out.append({
            "key": c["key"],
            "name": c["name"],
            "desc": c["desc"],
            "total": int(r["total"]) if r else 0,
            "scored": int(r["scored"] or 0) if r else 0,
            "max_score": round(float(r["max_score"]), 1) if (r and r["max_score"] is not None) else None,
            "lib_total": lib_counts.get(c["name"], 0),
        })
    return {"news_date": news_date, "period": period if is_valid_period(period) else None, "categories": out}


class CategoryUpdate(BaseModel):
    category: str


@router.put("/topics/{topic_id}/category")
def update_topic_category(topic_id: int, body: CategoryUpdate):
    """人工/Agent 覆盖类别。只接受 7 类固定值，写别的直接 400 —— 防止类别膨胀。"""
    if not is_valid_category(body.category):
        raise HTTPException(
            status_code=400,
            detail=f"非法类别「{body.category}」。只允许：{'、'.join(c['name'] for c in CATEGORIES)}",
        )
    conn = get_db()
    row = conn.execute("SELECT id, category FROM topics WHERE id = ?", (topic_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="选题不存在")
    old = row["category"]
    conn.execute("UPDATE topics SET category = ?, updated_at = ? WHERE id = ?",
                 (body.category, datetime.now().isoformat(), topic_id))
    conn.execute(
        "INSERT INTO topic_events (topic_id, event_type, metadata) VALUES (?, 'category_changed', ?)",
        (topic_id, json.dumps({"from": old, "to": body.category}, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()
    return {"id": topic_id, "category": body.category, "previous": old}


@router.get("/topics/search")
def search_topics(
    q: str = "",
    status: Optional[str] = None,
    source_name: Optional[str] = None,
    min_score: Optional[float] = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
):
    """搜索选题。status 同 /api/topics：unused/used/dropped 或 unscored/scored。"""
    conn = get_db()
    query = """
        SELECT t.*, s.total_score, s.traffic_score, s.opinion_score, s.novelty_score,
               s.recommended_format, s.key_angle
        FROM topics t
        LEFT JOIN topic_scores s ON s.id = (
            SELECT id FROM topic_scores WHERE topic_id = t.id ORDER BY scored_at DESC LIMIT 1
        )
        WHERE 1=1
    """
    params = []

    if q:
        query += " AND (t.title LIKE ? OR t.summary LIKE ? OR t.raw_content LIKE ?)"
        keyword = f"%{q}%"
        params.extend([keyword, keyword, keyword])

    if status == "unscored":
        query += " AND s.total_score IS NULL"
    elif status == "scored":
        query += " AND s.total_score IS NOT NULL"
    elif status in USAGE_STATUSES:
        query += " AND t.status = ?"
        params.append(status)

    if source_name:
        query += " AND t.source_name = ?"
        params.append(source_name)

    if min_score is not None:
        query += " AND s.total_score >= ?"
        params.append(min_score)

    query += " ORDER BY t.news_date DESC, t.id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = conn.execute(query, params).fetchall()

    # 总数：与上面同一套筛选条件，避免汇总数与明细条数对不上。
    count_query = (
        "SELECT COUNT(*) as cnt FROM topics t "
        "LEFT JOIN topic_scores s ON s.id = "
        "(SELECT id FROM topic_scores WHERE topic_id = t.id ORDER BY scored_at DESC LIMIT 1) WHERE 1=1"
    )
    count_params = []
    if q:
        count_query += " AND (t.title LIKE ? OR t.summary LIKE ? OR t.raw_content LIKE ?)"
        count_params.extend([keyword, keyword, keyword])
    if status == "unscored":
        count_query += " AND s.total_score IS NULL"
    elif status == "scored":
        count_query += " AND s.total_score IS NOT NULL"
    elif status in USAGE_STATUSES:
        count_query += " AND t.status = ?"
        count_params.append(status)
    total = conn.execute(count_query, count_params).fetchone()["cnt"]

    conn.close()
    return {
        "results": [dict(r) for r in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.get("/topics/{topic_id}")
def get_topic(topic_id: int):
    """获取选题详情（含评分历史、事件流、Agent活动）"""
    conn = get_db()
    topic = conn.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()
    if not topic:
        conn.close()
        raise HTTPException(status_code=404, detail="选题不存在")
    
    # 所有评分记录（按时间倒序）
    scores = conn.execute(
        "SELECT * FROM topic_scores WHERE topic_id = ? ORDER BY scored_at DESC",
        (topic_id,)
    ).fetchall()
    
    # 事件流
    events = conn.execute(
        "SELECT * FROM topic_events WHERE topic_id = ? ORDER BY event_at DESC",
        (topic_id,)
    ).fetchall()
    
    # Agent活动
    activities = conn.execute(
        "SELECT * FROM agent_activities WHERE target_type = 'topic' AND target_id = ? "
        "ORDER BY created_at DESC",
        (topic_id,)
    ).fetchall()
    
    # 创作任务文件
    task_files = conn.execute(
        "SELECT * FROM task_files WHERE topic_id = ? ORDER BY generated_at DESC",
        (topic_id,)
    ).fetchall()
    
    conn.close()
    return {
        "topic": dict(topic),
        "scores": [dict(s) for s in scores],
        "latest_score": dict(scores[0]) if scores else None,
        "events": [dict(e) for e in events],
        "activities": [dict(a) for a in activities],
        "task_files": [dict(tf) for tf in task_files],
    }


@router.post("/topics")
def create_topic(topic: TopicCreate):
    """手动创建选题"""
    conn = get_db()
    now = datetime.now().isoformat()
    # 类别：调用方明确指定且合法则采用，否则自动归类。
    # 统一走 app.verbs.classify（唯一判定实现），不要退回 app.taxonomy.classify。
    from app.verbs import classify
    from app.geo import judge as judge_geo
    category = (topic.category if is_valid_category(topic.category)
                else classify(topic.title, topic.summary, topic.source_ref))
    # 地域/主体与分类同一次落定，否则手动建的选题进不了归类树。
    region, entity, _kind = judge_geo(topic.title or "", topic.raw_content or topic.summary or "")
    cur = conn.execute(
        "INSERT INTO topics (title, summary, raw_content, source_type, source_ref, "
        "tags, news_date, published_date, source_name, source_level, source_author, "
        "original_url, category, status, region, entity, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (topic.title, topic.summary, topic.raw_content,
         topic.source_type, topic.source_ref,
         json.dumps(topic.tags, ensure_ascii=False),
         topic.news_date, topic.published_date, topic.source_name,
         topic.source_level, topic.source_author, topic.original_url,
         category, DEFAULT_USAGE, region, entity,
         now, now)
    )
    topic_id = cur.lastrowid
    # 记录事件
    conn.execute(
        "INSERT INTO topic_events (topic_id, event_type, metadata) "
        "VALUES (?, 'discovered', ?)",
        (topic_id, json.dumps({"source": topic.source_type, "category": category}, ensure_ascii=False))
    )
    conn.commit()
    conn.close()

    # 异步触发评分（不阻塞响应）
    try:
        from app.services.scoring_engine import score_topic_async
        score_topic_async(topic_id)
    except Exception:
        pass  # 评分失败不影响创建

    return {"id": topic_id, "message": "选题已创建"}


# ========== 素材使用状态（未用 / 已用 / 弃用）=========
# 平台只做选题供给。素材真正需要记录的事情只有一件：我用没用它。
# 所以使用状态是选题唯一的显式状态流转，且每次改动都留审计事件。

def _write_usage(conn, topic_id: int, status: str, note: str = "", actor: str = "User") -> dict:
    row = conn.execute("SELECT id, status FROM topics WHERE id = ?", (topic_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="选题不存在")
    old = row["status"]
    if old == status:
        return {"topic_id": topic_id, "status": status, "previous": old, "changed": False}
    now = datetime.now().isoformat()
    conn.execute("UPDATE topics SET status = ?, updated_at = ? WHERE id = ?", (status, now, topic_id))
    conn.execute(
        "INSERT INTO topic_events (topic_id, event_type, metadata) VALUES (?, 'usage_changed', ?)",
        (topic_id, json.dumps({"from": old, "to": status, "note": note}, ensure_ascii=False)),
    )
    conn.execute(
        "INSERT INTO agent_activities (agent_name, activity_type, target_type, target_id, summary) "
        "VALUES (?, 'usage_changed', 'topic', ?, ?)",
        (actor, topic_id, f"标记为{USAGE_LABELS[status]}" + (f"：{note}" if note else "")),
    )
    return {"topic_id": topic_id, "status": status, "previous": old, "changed": True}


@router.post("/topics/{topic_id}/usage")
def update_topic_usage(topic_id: int, body: UsageUpdate):
    """标记素材使用状态（未用 / 已用 / 弃用）。"""
    if body.status not in USAGE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"非法状态「{body.status}」。只允许：{'、'.join(USAGE_STATUSES)}",
        )
    conn = get_db()
    try:
        result = _write_usage(conn, topic_id, body.status, body.note)
        conn.commit()
    finally:
        conn.close()
    return {**result, "message": f"已标记为{USAGE_LABELS[body.status]}"}


@router.post("/topics/batch-usage")
def batch_update_usage(body: BatchUsage):
    """批量标记使用状态；逐条返回结果，不伪造部分成功。"""
    if body.status not in USAGE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"非法状态「{body.status}」。只允许：{'、'.join(USAGE_STATUSES)}",
        )
    conn = get_db()
    results = []
    changed = 0
    try:
        for tid in body.topic_ids:
            try:
                item = _write_usage(conn, tid, body.status, actor="User")
            except HTTPException as exc:
                results.append({"topic_id": tid, "status": "failed", "reason": exc.detail})
                continue
            changed += 1 if item["changed"] else 0
            results.append({**item, "status": body.status})
        conn.commit()
    finally:
        conn.close()
    return {
        "message": f"共 {len(body.topic_ids)} 条，实际改变 {changed} 条，标记为{USAGE_LABELS[body.status]}",
        "changed": changed,
        "results": results,
    }


@router.post("/topics/{topic_id}/select")
def select_topic(topic_id: int, body: TopicSelect):
    """确认选题并原子创建可回传的创作任务契约。"""
    try:
        from app.services.production_service import create_task
        result = create_task(topic_id, selected_note=body.note, created_by="human")
        return {
            "message": result["message"],
            "task_id": result["task"]["id"],
            "task_file": result.get("contract_path") or result["task"].get("contract_path"),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建创作任务失败: {str(e)}")


@router.post("/topics/{topic_id}/abandon")
def abandon_topic(topic_id: int, body: TopicAbandon):
    """弃用素材（等价于使用状态 dropped，保留原因便于以后回看）。"""
    conn = get_db()
    try:
        row = conn.execute("SELECT id FROM topics WHERE id = ?", (topic_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="选题不存在")
        now = datetime.now().isoformat()
        result = _write_usage(conn, topic_id, "dropped", body.reason)
        conn.execute(
            "UPDATE topics SET abandoned_reason = ?, abandoned_at = ? WHERE id = ?",
            (body.reason, now, topic_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {**result, "message": "素材已弃用"}


@router.post("/topics/{topic_id}/rescore")
def rescore_topic(topic_id: int):
    """重新评分"""
    conn = get_db()
    topic = conn.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()
    if not topic:
        conn.close()
        raise HTTPException(status_code=404, detail="选题不存在")
    conn.close()

    try:
        from app.services.scoring_engine import score_topic_sync
        result = score_topic_sync(topic_id)
        if result:
            return {"message": "评分完成", "score": result}
        else:
            return {"message": "评分失败，请检查LLM API配置"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"评分失败: {str(e)}")


@router.get("/topics/{topic_id}/events")
def get_topic_events(topic_id: int):
    """获取选题事件历史"""
    conn = get_db()
    events = conn.execute(
        "SELECT * FROM topic_events WHERE topic_id = ? ORDER BY event_at DESC",
        (topic_id,)
    ).fetchall()
    conn.close()
    return [dict(e) for e in events]


@router.get("/scoring-pending")
def get_scoring_pending(
    limit: int = Query(20, le=100),
    offset: int = 0,
    source_name: Optional[str] = None,
    news_date: Optional[str] = None,
):
    """获取待评分选题及其上下文（供Agent一次性读取所有评分所需信息）

    分页参数：
    - limit: 每次返回数量，默认20，最大100
    - offset: 偏移量
    - source_name: 按来源筛选
    - news_date: 按新闻日期筛选
    """
    conn = get_db()

    # "待评分"= 还没有任何评分记录的选题（事实判定，不依赖 status）
    query = (
        "SELECT t.id, t.title, t.summary, t.raw_content, t.source_type, t.source_ref, "
        "t.source_name, t.source_level, t.published_date, t.news_date, t.original_url, "
        "t.tags, t.created_at FROM topics t "
        "LEFT JOIN topic_scores s ON s.topic_id = t.id WHERE s.id IS NULL"
    )
    params = []
    if source_name:
        query += " AND t.source_name = ?"
        params.append(source_name)
    if news_date:
        query += " AND t.news_date = ?"
        params.append(news_date)
    query += " ORDER BY t.news_date DESC, t.id ASC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    topics = conn.execute(query, params).fetchall()

    # 总数（用于分页判断）
    count_query = (
        "SELECT COUNT(*) as cnt FROM topics t "
        "LEFT JOIN topic_scores s ON s.topic_id = t.id WHERE s.id IS NULL"
    )
    count_params = []
    if source_name:
        count_query += " AND t.source_name = ?"
        count_params.append(source_name)
    if news_date:
        count_query += " AND t.news_date = ?"
        count_params.append(news_date)
    total_count = conn.execute(count_query, count_params).fetchone()["cnt"]

    # 如果没有待评分选题，直接返回
    if not topics:
        conn.close()
        return {
            "pending_count": 0,
            "total_pending": total_count,
            "returned": 0,
            "offset": offset,
            "has_more": False,
            "pending_topics": [],
            "expertise_profile": [],
            "coverage_keywords": [],
            "performance_references": [],
            "performance_summary": "暂无历史发布观察。",
            "scoring_weights": {},
            "scoring_criteria": {},
        }

    # 专业画像
    profiles = conn.execute(
        "SELECT area, description, keywords, weight FROM expertise_profile ORDER BY weight DESC"
    ).fetchall()

    # 近期覆盖关键词（旧人工记录）+ OPC-ERP 同步回来的发布表现参考。
    coverage_rows = conn.execute(
        "SELECT key_topics FROM coverage_records ORDER BY published_at DESC LIMIT 50"
    ).fetchall()
    all_kws = set()
    for r in coverage_rows:
        kws = json.loads(r["key_topics"]) if r["key_topics"] else []
        all_kws.update(kws)

    # 评分权重
    tw = get_float_setting("traffic_weight", 0.4)
    ow = get_float_setting("opinion_weight", 0.4)
    nw = get_float_setting("novelty_weight", 0.2)

    performance_rows = conn.execute(
        """SELECT title, platform, metrics_json, status, published_at
        FROM publication_observations ORDER BY COALESCE(published_at, created_at) DESC LIMIT 30"""
    ).fetchall()
    performance_references = []
    for row in performance_rows:
        try:
            metrics = json.loads(row["metrics_json"] or "{}")
        except json.JSONDecodeError:
            metrics = {}
        performance_references.append({
            "title": row["title"], "platform": row["platform"], "status": row["status"],
            "latest_read_count": metrics.get("latest_read_count"),
            "latest_play_count": metrics.get("latest_play_count"),
            "published_at": row["published_at"],
        })

    from app.services.scoring_engine import _get_performance_summary
    performance_summary = _get_performance_summary(conn)

    conn.close()

    return {
        "pending_count": len(topics),
        "total_pending": total_count,
        "returned": len(topics),
        "offset": offset,
        "has_more": (offset + len(topics)) < total_count,
        "pending_topics": [dict(t) for t in topics],
        "expertise_profile": [dict(p) for p in profiles],
        "coverage_keywords": sorted(all_kws),
        "performance_references": performance_references,
        "performance_summary": performance_summary,
        "scoring_weights": {"traffic": tw, "opinion": ow, "novelty": nw},
        "scoring_criteria": {
            "traffic_score": "流量潜力 0-10：8-10重大发布/行业地震；5-7高关注度；3-5中等；1-3小众",
            "opinion_score": "观点匹配 0-10：8-10完全在专业领域且有独特观点；5-7在专业领域有基础；3-5部分相关；1-3不在领域",
            "novelty_score": "新颖度 0-10：8-10全新信息无人覆盖；5-7较新有差异化空间；3-5已讨论但有新角度；1-3已充分覆盖",
        },
    }


@router.post("/topics/{topic_id}/score")
def submit_score(topic_id: int, score: ScoreSubmission):
    """Agent直接提交评分（不经过LLM API，由Agent自身判断）"""
    conn = get_db()
    topic = conn.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()
    if not topic:
        conn.close()
        raise HTTPException(status_code=404, detail="选题不存在")
    # 评分不再受状态限制：评分是事实（topic_scores 会留记录），不是使用状态。
    # 素材用过之后仍可重评，方便以后回看当时 AI 的判断。

    # 构造与 _save_score 兼容的 result dict
    result = {
        "traffic_score": score.traffic_score,
        "opinion_score": score.opinion_score,
        "novelty_score": score.novelty_score,
        "traffic_reasoning": score.traffic_reasoning,
        "opinion_reasoning": score.opinion_reasoning,
        "novelty_reasoning": score.novelty_reasoning,
        "recommended_format": score.recommended_format,
        "format_reasoning": score.format_reasoning,
        "key_angle": score.key_angle,
    }

    from app.services.scoring_engine import _save_score
    score_id = _save_score(conn, topic_id, result, score.model)
    conn.close()
    return {"score_id": score_id, "message": "评分已提交", "topic_id": topic_id}


@router.post("/topics/batch-score")
def batch_submit_scores(body: BatchScoreSubmission):
    """Agent批量提交评分（一次处理多个待评分选题）"""
    from app.services.scoring_engine import _save_score
    conn = get_db()
    results = []
    success_count = 0
    fail_count = 0

    for item in body.scores:
        topic = conn.execute(
            "SELECT * FROM topics WHERE id = ?", (item.topic_id,)
        ).fetchone()
        if not topic:
            results.append({"topic_id": item.topic_id, "status": "not_found"})
            fail_count += 1
            continue

        result = {
            "traffic_score": item.traffic_score,
            "opinion_score": item.opinion_score,
            "novelty_score": item.novelty_score,
            "traffic_reasoning": item.traffic_reasoning,
            "opinion_reasoning": item.opinion_reasoning,
            "novelty_reasoning": item.novelty_reasoning,
            "recommended_format": item.recommended_format,
            "format_reasoning": item.format_reasoning,
            "key_angle": item.key_angle,
        }
        score_id = _save_score(conn, item.topic_id, result, item.model)
        results.append({
            "topic_id": item.topic_id,
            "score_id": score_id,
            "status": "scored",
            "total_score": round(
                item.traffic_score * get_float_setting("traffic_weight", 0.4) +
                item.opinion_score * get_float_setting("opinion_weight", 0.4) +
                item.novelty_score * get_float_setting("novelty_weight", 0.2),
                2
            )
        })
        success_count += 1

    conn.close()
    return {
        "total": len(body.scores),
        "success": success_count,
        "failed": fail_count,
        "results": results,
    }


@router.post("/topics/agent-score")
def agent_submit_scores(payload: Any = Body(...)):
    """供 OpenClaw 使用的容错评分入口；错误逐条返回，不让整轮任务因一项格式问题中断。"""
    from app.services.scoring_engine import _save_score

    items, input_errors = _normalise_agent_scores(payload)
    conn = get_db()
    results = []
    success_count = 0
    fail_count = len(input_errors)
    try:
        for item in items:
            topic = conn.execute("SELECT * FROM topics WHERE id = ?", (item["topic_id"],)).fetchone()
            if not topic:
                results.append({"topic_id": item["topic_id"], "status": "not_found"})
                fail_count += 1
                continue
            score_id = _save_score(conn, item["topic_id"], item, item["model"])
            total_score = (
                item["traffic_score"] * get_float_setting("traffic_weight", 0.4)
                + item["opinion_score"] * get_float_setting("opinion_weight", 0.4)
                + item["novelty_score"] * get_float_setting("novelty_weight", 0.2)
            )
            results.append({
                "topic_id": item["topic_id"],
                "score_id": score_id,
                "status": "scored",
                "total_score": round(total_score, 2),
            })
            success_count += 1
        conn.close()
        return {
            "total": len(items) + len(input_errors),
            "success": success_count,
            "failed": fail_count,
            "input_errors": input_errors,
            "results": results,
            "next": "POST /api/production/tasks/auto-queue",
        }
    except Exception:
        conn.close()
        raise


@router.put("/topics/{topic_id}/status")
def update_topic_status(topic_id: int, status: str):
    """旧版状态直改接口已废止。

    选题状态现在只有一个含义——素材用没用（未用/已用/弃用），
    必须走 POST /api/topics/{id}/usage 才能写，避免外部系统覆盖使用状态。
    """
    raise HTTPException(
        status_code=410,
        detail="选题状态不能直改。要标记使用状态请用 POST /api/topics/{id}/usage（unused/used/dropped）。",
    )


# ========== 批量操作 ==========

@router.post("/topics/batch-select")
def batch_select_topics(body: BatchSelect):
    """批量确认选题；任一条失败会单独返回，不伪造已完成状态。"""
    results = []
    for tid in body.topic_ids:
        try:
            from app.services.production_service import create_task
            result = create_task(tid, selected_note=body.note, created_by="human")
            results.append({"topic_id": tid, "status": "queued", "task_id": result["task"]["id"], "task_file": result.get("contract_path") or result["task"].get("contract_path")})
        except ValueError as exc:
            results.append({"topic_id": tid, "status": "skipped", "reason": str(exc)})
        except Exception as exc:
            results.append({"topic_id": tid, "status": "failed", "reason": str(exc)})
    return {"message": f"已创建 {len([r for r in results if r['status'] == 'queued'])} 条创作任务", "results": results}


@router.post("/topics/batch-abandon")
def batch_abandon_topics(body: BatchAbandon):
    """批量弃用素材（使用状态 dropped）。"""
    conn = get_db()
    results = []
    now = datetime.now().isoformat()

    try:
        for tid in body.topic_ids:
            row = conn.execute("SELECT id FROM topics WHERE id = ?", (tid,)).fetchone()
            if not row:
                results.append({"topic_id": tid, "status": "not_found"})
                continue
            _write_usage(conn, tid, "dropped", body.reason)
            conn.execute(
                "UPDATE topics SET abandoned_reason = ?, abandoned_at = ? WHERE id = ?",
                (body.reason, now, tid),
            )
            results.append({"topic_id": tid, "status": "dropped"})
        conn.commit()
    finally:
        conn.close()
    return {"message": f"已弃用 {len(results)} 条", "results": results}


# ========== 后半段流转 ==========

@router.post("/topics/{topic_id}/producing")
def mark_producing(topic_id: int):
    raise HTTPException(status_code=410, detail="生产中属于创作任务状态，请领取并启动 production task。")

@router.post("/topics/{topic_id}/publish")
def mark_published(topic_id: int):
    raise HTTPException(status_code=410, detail="发布事实只能从 OPC-ERP 数据回流同步，不能手动标记。")


@router.post("/topics/{topic_id}/reopen")
def reopen_topic(topic_id: int):
    """把弃用的素材收回为未用（以后翻出来还能再写）。"""
    conn = get_db()
    try:
        topic = conn.execute("SELECT id, status FROM topics WHERE id = ?", (topic_id,)).fetchone()
        if not topic:
            raise HTTPException(status_code=404, detail="选题不存在")
        if topic["status"] != "dropped":
            raise HTTPException(
                status_code=400,
                detail=f"只有弃用的素材可以收回，当前状态: {USAGE_LABELS.get(topic['status'], topic['status'])}",
            )
        now = datetime.now().isoformat()
        result = _write_usage(conn, topic_id, "unused", "从弃用收回")
        conn.execute(
            "UPDATE topics SET abandoned_reason = NULL, abandoned_at = NULL WHERE id = ?",
            (topic_id,),
        )
        conn.commit()
    finally:
        conn.close()
    return {**result, "message": "素材已收回为未用"}
