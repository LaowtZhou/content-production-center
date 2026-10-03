"""事件归并 API —— 平台只做粗筛，是否"同一件事"由 Agent 判定。

设计（2026-10-02 周老师定 + 借鉴 AIHOT 思路）：
- 平台**不**用 bigram 硬抠（上一版这么做，召回率只有 2.2%，且把不同事件混到一起）。
- 平台只做**粗筛**：从标题里抽出实体词，把"共享实体词"的选题摆出来当候选。
- 是否同一事件、是后续进展还是另一件事 —— 由 Agent（Codex 等）按
  docs/事件归并-Agent工作手册.md 判断，再把结果写回 /api/events/merge。
- 归并结果落在 topics.event_key（不新建表）：同一事件的多条选题共享一个 key。

配套：GET /events 页面（手册 + 候选），供人和 Agent 使用。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.config import get_db
from app.services.event_lines import unify_line
from app.verbs import classify as _classify

router = APIRouter()

# 常见噪声词（出现在实体位置但不是实体，避免制造假候选）
_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "into", "your", "you",
    "new", "how", "why", "what", "who", "its", "his", "her", "are", "was",
    "ai", "aigc", "llm", "app", "apps", "day", "week", "year", "now", "why",
    "top", "best", "vs", "says", "say", "can", "will", "not", "but", "all",
    "中国", "美国", "全球", "发布", "宣布", "正式", "独家", "重磅", "刚刚",
    "今天", "昨日", "凌晨", "突发", "最新", "我们", "他们", "如何", "为什么",
}

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9\.\-]{2,}")
_CJK = re.compile(r"[\u4e00-\u9fa5]{2,6}")


def _tokens(title: str) -> set[str]:
    """从标题抽实体候选：英文词（去停用词）+ 关键中文片段。"""
    out: set[str] = set()
    for w in _WORD.findall(title or ""):
        lw = w.lower().strip(".")
        if len(lw) >= 3 and lw not in _STOP:
            out.add(lw)
    return out


@router.get("/events/candidates")
def event_candidates(
    news_date: Optional[str] = None,
    days: int = Query(7, le=60),
    min_shared: int = Query(1, ge=1),
    limit: int = Query(60, le=300),
):
    """粗筛：找出可能属于同一事件的候选组。

    只按"标题共享实体词"分堆，不做判断。是否同一事件由 Agent 决定。
    返回每组：共享实体 + 涉及选题（id/标题/来源/日期/是否已归并）。
    """
    conn = get_db()
    sql = "SELECT id, title, source_name, news_date, event_key, category FROM topics WHERE 1=1"
    params: list = []
    if news_date:
        sql += " AND news_date = ?"
        params.append(news_date)
    elif days:
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        sql += " AND (news_date IS NULL OR news_date >= ?)"
        params.append(since)
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    by_token: dict[str, list] = {}
    for r in rows:
        for tok in _tokens(r["title"] or ""):
            by_token.setdefault(tok, []).append(dict(r))

    groups = []
    for tok, items in by_token.items():
        # 去重：同一实体词在多个选题出现才有价值
        ids = {i["id"] for i in items}
        if len(ids) <= min_shared:
            continue
        # 整组都已归并（成员全部已有 event_key）→ 已处理完，不再摆出来。
        # 只要组里还有未归并成员，就整组返回（含已归并成员，带 event_key），
        # 让 Agent 判断"新出现的这条要不要并入那条老线"。
        # 这样既避免重复判断，也不会让 Agent 把已归并条目误并到别的线（merge 是直接覆盖 event_key）。
        if not any(not (i["event_key"] or "") for i in items):
            continue
        groups.append({"shared_token": tok, "count": len(ids), "topics": items})

    groups.sort(key=lambda g: g["count"], reverse=True)
    return {
        "scope": {"news_date": news_date, "days": None if news_date else days},
        "scanned": len(rows),
        "groups": groups[:limit],
        "hint": "以上只是共享实体词的候选。是否同一事件、是进展还是另一件事，"
                "请按 docs/事件归并-Agent工作手册.md 判断后用 POST /api/events/merge 写回。",
    }


class MergeRequest(BaseModel):
    topic_ids: list[int]
    event_key: Optional[str] = None
    note: str = ""
    agent: str = "agent"


@router.post("/events/merge")
def event_merge(body: MergeRequest):
    """Agent 判定"这些选题是同一件事"后写回。给它们同一个 event_key。"""
    if not body.topic_ids:
        raise HTTPException(status_code=400, detail="topic_ids 不能为空")
    conn = get_db()
    ids = list(dict.fromkeys(body.topic_ids))  # 去重保序
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT id, event_key FROM topics WHERE id IN ({placeholders})", ids).fetchall()
    if len(rows) != len(ids):
        conn.close()
        found = {r["id"] for r in rows}
        raise HTTPException(status_code=404, detail=f"以下选题不存在：{sorted(set(ids) - found)}")

    # 未指定 key 时，用最小 id 生成一个稳定 key（幂等：重复调用结果一致）
    event_key = (body.event_key or "").strip() or f"evt-{min(ids)}"
    now = datetime.now().isoformat()
    for tid in ids:
        conn.execute("UPDATE topics SET event_key = ?, updated_at = ? WHERE id = ?", (event_key, now, tid))
        conn.execute(
            "INSERT INTO topic_events (topic_id, event_type, metadata) VALUES (?, 'event_merged', ?)",
            (tid, json.dumps({"event_key": event_key, "members": ids, "by": body.agent,
                              "note": body.note}, ensure_ascii=False)),
        )

    # 整线一致：并线后必须让整条线待在同一格（分类/地域/主体），否则归类树会
    # 把同一件事按各自的格子拆开显示。规则见 app/services/event_lines.py。
    unified = unify_line(conn, event_key, _classify, now)
    if unified:
        conn.execute(
            "INSERT INTO topic_events (topic_id, event_type, metadata) "
            "VALUES (?, 'event_line_unified', ?)",
            (min(ids), json.dumps({**unified, "by": body.agent}, ensure_ascii=False)),
        )

    conn.commit()
    conn.close()
    return {"event_key": event_key, "merged": ids,
            "unified": unified,
            "message": f"已把 {len(ids)} 条选题归为同一事件"}


class UnmergeRequest(BaseModel):
    topic_ids: list[int]
    reason: str = ""
    agent: str = "agent"


@router.post("/events/unmerge")
def event_unmerge(body: UnmergeRequest):
    """撤销归并（判定错了就退回）。"""
    if not body.topic_ids:
        raise HTTPException(status_code=400, detail="topic_ids 不能为空")
    conn = get_db()
    now = datetime.now().isoformat()
    for tid in body.topic_ids:
        conn.execute("UPDATE topics SET event_key = '', updated_at = ? WHERE id = ?", (now, tid))
        conn.execute(
            "INSERT INTO topic_events (topic_id, event_type, metadata) VALUES (?, 'event_unmerged', ?)",
            (tid, json.dumps({"by": body.agent, "reason": body.reason}, ensure_ascii=False)),
        )
    conn.commit()
    conn.close()
    return {"unmerged": body.topic_ids}


@router.get("/events/threads")
def event_threads(limit: int = Query(100, le=500)):
    """查看已归并的事件线（同一 event_key 的多条选题）。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT event_key, COUNT(*) AS n, MIN(news_date) AS first_date, MAX(news_date) AS last_date "
        "FROM topics WHERE event_key IS NOT NULL AND event_key <> '' GROUP BY event_key "
        "ORDER BY n DESC, last_date DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out = []
    for r in rows:
        members = conn.execute(
            "SELECT id, title, source_name, news_date FROM topics WHERE event_key = ? "
            "ORDER BY news_date, id",
            (r["event_key"],),
        ).fetchall()
        out.append({
            "event_key": r["event_key"], "count": r["n"],
            "first_date": r["first_date"], "last_date": r["last_date"],
            "members": [dict(m) for m in members],
        })
    conn.close()
    return {"threads": out}
