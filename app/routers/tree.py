"""归类三级树 —— 分类 > 中国/美国/其他 > 政府机构或公司主体。

周老师 2026-10-03 定：
- 这是主浏览入口，替代原来的"事件归并"页。
- 默认不跨分类：分类由动词/动名词决定（app/verbs.py），事件线整线统一。
- 格子（分类+地域+主体）里仍按 event_key 聚成事件线（同一件事的多条素材）。

数据只读；人工改分类/地域/主体走 PUT 接口（见 app/routers/topics.py）。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from app.config import get_db
from app.statuses import DEFAULT_PERIOD, is_valid_period, period_start
from app.taxonomy import CATEGORIES

router = APIRouter()


def _where(period: str, cat: Optional[str] = None, region: Optional[str] = None,
           entity: Optional[str] = None) -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []
    start = period_start(period)
    if start:
        clauses.append("news_date >= ?")
        params.append(start)
    if cat:
        clauses.append("category = ?")
        params.append(cat)
    if region:
        clauses.append("region = ?")
        params.append(region)
    if entity:
        clauses.append("entity = ?")
        params.append(entity)
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def build_tree(period: str = DEFAULT_PERIOD, cat: Optional[str] = None,
               region: Optional[str] = None, entity: Optional[str] = None) -> dict:
    """按三层逐级展开：没选分类只给第一层，选了才往下算，避免一次性全表聚合。"""
    if not is_valid_period(period):
        period = DEFAULT_PERIOD
    conn = get_db()
    try:
        # 第一层：分类
        w, p = _where(period)
        rows = conn.execute(
            "SELECT category, COUNT(*) n FROM topics" + w + " GROUP BY category", p
        ).fetchall()
        per_cat = {r["category"]: r["n"] for r in rows}
        cats = [{
            "key": c["key"], "name": c["name"], "desc": c["desc"],
            "count": per_cat.get(c["name"], 0),
        } for c in CATEGORIES]
        total = sum(per_cat.values())

        # 第二层：地域（选中分类后）
        regions: list[dict] = []
        if cat:
            w, p = _where(period, cat=cat)
            rows = conn.execute(
                "SELECT region, COUNT(*) n FROM topics" + w
                + " GROUP BY region ORDER BY n DESC", p
            ).fetchall()
            regions = [{"name": r["region"], "count": r["n"]} for r in rows]

        # 第三层：主体（选中地域后）
        entities: list[dict] = []
        if cat and region:
            w, p = _where(period, cat=cat, region=region)
            rows = conn.execute(
                "SELECT entity, COUNT(*) n FROM topics" + w
                + " GROUP BY entity ORDER BY n DESC", p
            ).fetchall()
            entities = [{"name": r["entity"], "count": r["n"]} for r in rows]

        # 格子里的素材：按 event_key 聚成事件线，未归并的单独列
        lines: list[dict] = []
        loose: list[dict] = []
        nodes: list[dict] = []
        grid_total = 0
        if cat and region and entity:
            w, p = _where(period, cat=cat, region=region, entity=entity)
            rows = conn.execute(
                "SELECT id, title, news_date, source_name, status, "
                "COALESCE(event_key,'') ek, category, region, entity "
                "FROM topics" + w + " ORDER BY news_date DESC, id DESC", p
            ).fetchall()
            buckets: dict[str, list] = {}
            for r in rows:
                item = dict(r)
                if item["ek"]:
                    buckets.setdefault(item["ek"], []).append(item)
                else:
                    loose.append(item)

            for key, items in buckets.items():
                # 先按时间正序：最早那条代表事情起点，用它当线名和区间起点。
                items.sort(key=lambda x: (x["news_date"] or "", x["id"]))
                lines.append({
                    "event_key": key,
                    "name": items[0]["title"],
                    "count": len(items),
                    "first_date": items[0]["news_date"],
                    "last_date": items[-1]["news_date"],
                    # 展示时倒序：最新一条在最上面
                    "members": list(reversed(items)),
                })
            # 线上不再按"条数多少"排（那是错的，最近的新闻会沉到下面）。
            # 统一按最近发生时间倒序，最新的在最上面。
            lines.sort(key=lambda ln: (ln["last_date"] or "", ln["count"]), reverse=True)

            loose.sort(key=lambda x: (x["news_date"] or "", x["id"]), reverse=True)
            grid_total = sum(len(v) for v in buckets.values()) + len(loose)

            # 展示节点：事件线与散素材挂在同一条时间轴上，谁更新谁靠上。
            # 散素材不是"尾巴"，它是这条时间轴上还没归并的那一段。
            nodes = [{"type": "line", **ln} for ln in lines]
            if loose:
                nodes.append({
                    "type": "loose",
                    "name": "散素材（还没归并成事件）",
                    "count": len(loose),
                    "first_date": loose[-1]["news_date"],
                    "last_date": loose[0]["news_date"],
                    "members": loose,
                })
            nodes.sort(
                key=lambda n: (n["last_date"] or "", n["first_date"] or "",
                               n["type"] == "line"),
                reverse=True,
            )

        return {
            "period": period,
            "cat": cat, "region": region, "entity": entity,
            "cats": cats, "regions": regions, "entities": entities,
            "total": total, "lines": lines, "loose": loose, "nodes": nodes,
            "grid_total": grid_total,
        }
    finally:
        conn.close()


@router.get("/tree")
def api_tree(
    period: str = Query(DEFAULT_PERIOD),
    cat: Optional[str] = None,
    region: Optional[str] = None,
    entity: Optional[str] = None,
):
    """三级树数据（供页面与 Agent 使用）。"""
    return build_tree(period, cat, region, entity)
