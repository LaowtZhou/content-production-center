"""事件线（同一 event_key 的多条素材）的整线一致性维护。

平台约定（见 docs/事件归并-Agent工作手册.md）：**默认不跨分类** ——
一条事件线整线只有一个分类、一个地域、一个主体，这样它在归类树里只占一个格子。

为什么单拎出来：这套规则原本**只写在 `scripts/init_db.py` 的版本迁移闸里**
（`tree_v1_migrated` / `classify_version` / `geo_judge_version`），跑过一次就不再触发。
于是「人 / Agent 新并的线」不会被统一 —— 同一件事的多条素材会按各自的分类/地域/主体
被归类树**拆到不同格子里**，看起来像好几件事。所以 `POST /api/events/merge` 必须调这里。
"""
from __future__ import annotations


def unify_line(conn, event_key: str, classify_fn, now: str) -> dict | None:
    """把一条事件线拉回同一格。

    分类：整线标题拼起来按动词再判一次（与 init_db 迁移同一口径）。
    地域/主体：取**最早一条**（与树页的线名、区间起点同一口径）。
    线内不足 2 条时不处理（返回 None）——单条素材没有"整线一致"可言。

    调用方负责 commit。返回统一后的结果（供接口回显），未处理返回 None。
    """
    members = conn.execute(
        "SELECT id, title, news_date, region, entity FROM topics "
        "WHERE event_key = ? ORDER BY news_date, id",
        (event_key,),
    ).fetchall()
    if len(members) < 2:
        return None

    line_cat = classify_fn(" ".join((m["title"] or "") for m in members))
    rep = members[0]  # 最早一条
    for m in members:
        conn.execute(
            "UPDATE topics SET category = ?, region = ?, entity = ?, updated_at = ? "
            "WHERE id = ?",
            (line_cat, rep["region"], rep["entity"], now, m["id"]),
        )
    return {
        "event_key": event_key,
        "category": line_cat,
        "region": rep["region"],
        "entity": rep["entity"],
        "members": [m["id"] for m in members],
    }
