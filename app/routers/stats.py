"""统计 API - 看板数据"""

from fastapi import APIRouter
from app.config import get_db

router = APIRouter()


@router.get("/stats")
def get_stats():
    """获取看板统计数据"""
    conn = get_db()

    # 状态统计
    status_counts = {}
    for row in conn.execute(
        "SELECT status, COUNT(*) as c FROM topics GROUP BY status"
    ).fetchall():
        status_counts[row["status"]] = row["c"]

    # 来源类型统计
    source_counts = {}
    for row in conn.execute(
        "SELECT source_type, COUNT(*) as c FROM topics GROUP BY source_type"
    ).fetchall():
        source_counts[row["source_type"]] = row["c"]

    # 日期范围
    date_range = conn.execute(
        "SELECT MIN(news_date) as min_d, MAX(news_date) as max_d, "
        "COUNT(DISTINCT news_date) as days "
        "FROM topics WHERE news_date IS NOT NULL"
    ).fetchone()

    # 总选题数
    total = conn.execute("SELECT COUNT(*) as c FROM topics").fetchone()["c"]

    # 最近7天每日新选题数（按news_date）
    daily = []
    for row in conn.execute(
        "SELECT news_date as d, COUNT(*) as c "
        "FROM topics WHERE news_date IS NOT NULL "
        "AND news_date >= DATE('now', '-14 days') "
        "GROUP BY news_date ORDER BY d DESC"
    ).fetchall():
        daily.append({"date": row["d"], "count": row["c"]})

    # 平均分（取每条选题的最新一次评分；不再用 status 过滤，评分是事实不是状态）
    avg_score = conn.execute(
        "SELECT AVG(s.total_score) as avg FROM topic_scores s "
        "WHERE s.id = (SELECT MAX(id) FROM topic_scores WHERE topic_id = s.topic_id)"
    ).fetchone()

    # 覆盖记录数
    coverage_count = conn.execute(
        "SELECT COUNT(*) as c FROM coverage_records"
    ).fetchone()["c"]

    # Agent活动统计
    agent_stats = []
    for row in conn.execute(
        "SELECT agent_name, COUNT(*) as cnt, MAX(created_at) as last_time "
        "FROM agent_activities GROUP BY agent_name ORDER BY last_time DESC"
    ).fetchall():
        agent_stats.append({
            "agent": row["agent_name"],
            "total_activities": row["cnt"],
            "last_activity": row["last_time"],
        })

    conn.close()
    return {
        "total_topics": total,
        "status_counts": status_counts,
        "source_counts": source_counts,
        "date_range": {
            "start": date_range["min_d"] if date_range else None,
            "end": date_range["max_d"] if date_range else None,
            "days_covered": date_range["days"] if date_range else 0,
        },
        "daily_new": daily,
        "avg_score": round(avg_score["avg"], 2) if avg_score and avg_score["avg"] else 0,
        "coverage_count": coverage_count,
        "agent_stats": agent_stats,
    }


@router.get("/dates")
def get_dates():
    """获取日期列表（带每日选题数），用于日期导航"""
    conn = get_db()
    rows = conn.execute(
        "SELECT news_date, COUNT(*) as cnt "
        "FROM topics WHERE news_date IS NOT NULL "
        "GROUP BY news_date ORDER BY news_date DESC"
    ).fetchall()
    conn.close()
    return [{"date": row["news_date"], "count": row["cnt"]} for row in rows]


@router.get("/sources/list")
def get_source_list():
    """获取来源名称列表（带选题数）"""
    conn = get_db()
    rows = conn.execute(
        "SELECT source_name, COUNT(*) as cnt "
        "FROM topics WHERE source_name != '' "
        "GROUP BY source_name ORDER BY cnt DESC"
    ).fetchall()
    conn.close()
    return [{"name": row["source_name"], "count": row["cnt"]} for row in rows]


@router.get("/agent-activities")
def get_agent_activities(limit: int = 30):
    """获取最近的Agent活动记录"""
    conn = get_db()
    rows = conn.execute(
        "SELECT a.*, t.title as topic_title "
        "FROM agent_activities a "
        "LEFT JOIN topics t ON t.id = a.target_id AND a.target_type = 'topic' "
        "ORDER BY a.created_at DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
