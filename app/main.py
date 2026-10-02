"""FastAPI 应用入口"""

import os
import sys
import json
from pathlib import Path
from contextlib import asynccontextmanager

# 确保项目根目录在sys.path中
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from fastapi import FastAPI, Request, Query
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse
from app.runtime import APP_BASE_URL, APP_PORT

from app.config import TEMPLATE_DIR, STATIC_DIR

# 每次启动执行无损 schema 迁移；不会覆盖历史选题或原始来源信息。
from scripts.init_db import init_database
init_database()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：非核心后台能力失败时降级，不让工作台整体打不开。"""
    app.state.runtime_issues = []
    from app.services.file_watcher import start_watcher
    try:
        watcher = start_watcher()
        if watcher and watcher.get("status") != "running":
            app.state.runtime_issues.append({"component": "watcher", "message": watcher.get("message", "文件监听未运行")})
    except Exception as exc:
        app.state.runtime_issues.append({"component": "watcher", "message": str(exc)})
        print(f"[启动降级] 文件监听不可用: {exc}")

    from app.services.scheduler import start_scheduler
    try:
        start_scheduler()
    except Exception as exc:
        app.state.runtime_issues.append({"component": "scheduler", "message": str(exc)})
        print(f"[启动降级] 定时任务不可用: {exc}")

    yield

    from app.services.file_watcher import stop_watcher
    stop_watcher()
    from app.services.scheduler import stop_scheduler
    stop_scheduler()


app = FastAPI(title="内容生产中心", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.filters['from_json'] = lambda s: json.loads(s) if s else []

# 类别聚焦的周期选项（唯一事实来源：app/statuses.py），所有模板都能直接引用
from app.statuses import PERIODS as _PERIODS, DEFAULT_PERIOD as _DEFAULT_PERIOD  # noqa: E402
templates.env.globals['periods'] = _PERIODS

templates.env.globals['statusLabel'] = lambda s: {
    # 素材使用状态（选题唯一状态，见 app/statuses.py）
    'unused': '未用', 'used': '已用', 'dropped': '弃用',
    # 生产任务等其它对象的状态
    'queued': '待领取', 'claimed': '已领取', 'running': '执行中', 'blocked': '已阻塞',
    'draft_ready': '初稿完成', 'publish_ready': '发布稿完成', 'failed': '执行失败',
    'observed': '已观察', 'matched': '已关联', 'review_candidate': '待复盘',
    'open': '待处理', 'accepted': '已接受', 'dismissed': '已忽略'
}.get(s, s)

templates.env.globals['statusClass'] = lambda s: 'status-' + s

templates.env.globals['sourceLabel'] = lambda s: {
    'openclaw': 'OpenClaw', 'manual': '手动', 'x': 'X',
    'bilibili': 'B站', 'douyin': '抖音', 'other': '其他'
}.get(s, s)

templates.env.globals['formatScore'] = lambda v: '-' if v is None else f'{float(v):.1f}'

templates.env.globals['scoreClass'] = lambda v: (
    'score-high' if v is not None and float(v) >= 7 else
    'score-mid' if v is not None and float(v) >= 4 else
    'score-low'
)

# 导入路由
from app.routers import topics, sources, coverage, profile, settings, stats, production, reviews, operations, events, tree as tree_router

app.include_router(topics.router, prefix="/api", tags=["选题"])
app.include_router(sources.router, prefix="/api", tags=["来源"])
app.include_router(coverage.router, prefix="/api", tags=["覆盖记录"])
app.include_router(profile.router, prefix="/api", tags=["专业画像"])
app.include_router(settings.router, prefix="/api", tags=["设置"])
app.include_router(stats.router, prefix="/api", tags=["统计"])
app.include_router(production.router, prefix="/api", tags=["生产编排"])
app.include_router(reviews.router, prefix="/api", tags=["复盘学习"])
app.include_router(operations.router, prefix="/api", tags=["运营主线"])
app.include_router(events.router, prefix="/api", tags=["事件归并"])
app.include_router(tree_router.router, prefix="/api", tags=["归类树"])


def _database_health() -> dict:
    from app.config import DB_PATH, get_db
    try:
        conn = get_db()
        integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
        conn.execute("SELECT 1 FROM topics LIMIT 1").fetchone()
        conn.close()
        return {"status": "ready" if integrity == "ok" else "degraded", "path": str(DB_PATH), "integrity": integrity}
    except Exception as exc:
        return {"status": "failed", "message": str(exc)}


@app.get("/api/health")
async def health_check():
    """给启动器和用户看的真实就绪状态；不返回密钥或业务正文。"""
    from app.services.file_watcher import watcher_status
    from app.services.scheduler import scheduler_status

    database = _database_health()
    components = {
        "database": database,
        "watcher": watcher_status(),
        "scheduler": scheduler_status(),
    }
    fatal = database["status"] == "failed"
    degraded = any(value["status"] != "running" for key, value in components.items() if key != "database") or database["status"] == "degraded"
    return JSONResponse(
        status_code=503 if fatal else 200,
        content={
            "status": "failed" if fatal else "degraded" if degraded else "ready",
            "service": "content-production-center",
            "pid": os.getpid(),
            "base_url": APP_BASE_URL,
            "components": components,
            "issues": getattr(app.state, "runtime_issues", []),
        },
    )


@app.get("/status", response_class=HTMLResponse)
async def status_page(request: Request):
    return templates.TemplateResponse(request, "status.html", {"app_base_url": APP_BASE_URL, "app_port": APP_PORT})


@app.get("/", response_class=HTMLResponse)
async def dashboard_page(
    request: Request,
    news_date: str = Query(None),
    status: str = Query(None),
    q: str = Query(None),
    source_name: str = Query(None),
    category: str = Query(None),
    sort: str = Query(None),
    period: str = Query(None),
    scope: str = Query(None),
):
    """素材看板 - 按日期浏览 + 类别聚焦（可选周期）+ 关键词搜索。

    两个时间口径，互不混淆：
    - scope=date（默认）：看左侧日期导航选中的某一天，这是"今天来了什么"的视角。
    - scope=period：看点开类别卡片后的周期视图（近7日/近30日/本季度/本年/历史全部）。
      类别卡片的数字就是按周期算的，所以点进去必须也按周期筛，
      不然卡片写 30 条、点进去只有 3 条，两个口径对不上。

    status 取值见 app/statuses.py：unused/used/dropped（使用状态）
    以及 unscored/scored（按有无评分记录的事实筛选）。
    """
    from app.config import get_db
    from app.services.operations_service import get_operations_overview
    from app.taxonomy import CATEGORIES, CATEGORY_NAMES
    from app.statuses import (
        DEFAULT_PERIOD,
        USAGE_STATUSES,
        is_valid_period,
        period_label,
        period_start,
    )
    conn = get_db()

    # 说明：此前每次打开看板都会把 7 天未选中的选题 status 改成 expired（写库）。
    # 周老师 2026-10-02 明确：素材应一直保留、不要自动消失。故取消该自动过期写入。
    sort_key = "date" if sort == "date" else "score"

    period = period if is_valid_period(period) else DEFAULT_PERIOD
    period_text, period_range = period_label(period)
    period_from = period_start(period)
    scope = "period" if scope == "period" else "date"

    # 统计：待评分/已评分是事实（有无评分记录），未用/已用/弃用是使用状态。
    total = conn.execute("SELECT COUNT(*) as c FROM topics").fetchone()["c"]
    scored_total = conn.execute(
        "SELECT COUNT(*) as c FROM topics t "
        "WHERE EXISTS (SELECT 1 FROM topic_scores s WHERE s.topic_id = t.id)"
    ).fetchone()["c"]
    usage_counts = {}
    for row in conn.execute("SELECT status, COUNT(*) as c FROM topics GROUP BY status"):
        usage_counts[row["status"]] = row["c"]
    status_counts = {
        "total": total,
        "unscored": total - scored_total,
        "scored": scored_total,
        "unused": usage_counts.get("unused", 0),
        "used": usage_counts.get("used", 0),
        "dropped": usage_counts.get("dropped", 0),
    }

    date_range = conn.execute(
        "SELECT MIN(news_date) as min_d, MAX(news_date) as max_d, "
        "COUNT(DISTINCT news_date) as days "
        "FROM topics WHERE news_date IS NOT NULL"
    ).fetchone()

    # 日期列表（带每日素材数）
    dates = conn.execute(
        "SELECT news_date, COUNT(*) as cnt "
        "FROM topics WHERE news_date IS NOT NULL "
        "GROUP BY news_date ORDER BY news_date DESC"
    ).fetchall()

    # 当前选中日期的素材（默认最新日期）；周期视图下不再按单日约束。
    if not news_date and not q and dates and scope != "period":
        news_date = dates[0]["news_date"]

    # 查询素材
    query = """
        SELECT t.*, s.total_score, s.traffic_score, s.opinion_score, s.novelty_score,
               s.recommended_format, s.key_angle,
               s.traffic_reasoning, s.opinion_reasoning, s.novelty_reasoning
        FROM topics t
        LEFT JOIN topic_scores s ON s.id = (
            SELECT id FROM topic_scores WHERE topic_id = t.id ORDER BY scored_at DESC LIMIT 1
        )
        WHERE 1=1
    """
    params = []

    # 关键词匹配标题/摘要/正文
    if q:
        keyword = f"%{q}%"
        query += " AND (t.title LIKE ? OR t.summary LIKE ? OR t.raw_content LIKE ?)"
        params.extend([keyword, keyword, keyword])

    if scope == "period":
        if period_from:
            query += " AND t.news_date >= ?"
            params.append(period_from)
    elif news_date:
        query += " AND t.news_date = ?"
        params.append(news_date)

    if status == "unscored":
        query += " AND s.total_score IS NULL"
    elif status == "scored":
        query += " AND s.total_score IS NOT NULL"
    elif status in USAGE_STATUSES:
        query += " AND t.status = ?"
        params.append(status)
    else:
        status = None
    if source_name:
        query += " AND t.source_name = ?"
        params.append(source_name)
    if category and category in CATEGORY_NAMES:
        query += " AND t.category = ?"
        params.append(category)
    else:
        category = None
    if sort_key == "score":
        query += " ORDER BY (s.total_score IS NULL) ASC, s.total_score DESC, t.news_date DESC, t.id DESC"
    else:
        query += " ORDER BY t.news_date DESC, t.id DESC"

    topics_list = conn.execute(query, params).fetchall()

    # 类别聚焦：按所选周期统计（同一周期口径也用在点进来的列表上，保证数字对得上）。
    cat_scope = "FROM topics t LEFT JOIN topic_scores s ON s.id = (" \
                "SELECT id FROM topic_scores WHERE topic_id = t.id ORDER BY scored_at DESC LIMIT 1) WHERE 1=1"
    cat_params = []
    if period_from:
        cat_scope += " AND t.news_date >= ?"
        cat_params.append(period_from)
    if source_name:
        cat_scope += " AND t.source_name = ?"
        cat_params.append(source_name)
    if q:
        cat_scope += " AND (t.title LIKE ? OR t.summary LIKE ? OR t.raw_content LIKE ?)"
        cat_params.extend([f"%{q}%"] * 3)
    cat_rows = conn.execute(
        "SELECT t.category AS category, COUNT(*) AS total, "
        "SUM(CASE WHEN s.total_score IS NOT NULL THEN 1 ELSE 0 END) AS scored, "
        "MAX(s.total_score) AS max_score " + cat_scope + " GROUP BY t.category",
        cat_params,
    ).fetchall()
    cat_by_name = {r["category"] or "": r for r in cat_rows}
    # 全库类别分布（不受周期影响），作为卡片上的一行小字参照。
    lib_cat = {
        (r["category"] or ""): int(r["n"])
        for r in conn.execute("SELECT category, COUNT(*) AS n FROM topics GROUP BY category")
    }
    categories = []
    for c in CATEGORIES:
        r = cat_by_name.get(c["name"])
        categories.append({
            "key": c["key"], "name": c["name"], "desc": c["desc"],
            "total": int(r["total"]) if r else 0,
            "lib_total": lib_cat.get(c["name"], 0),
            "scored": int(r["scored"] or 0) if r else 0,
            "max_score": round(float(r["max_score"]), 1) if (r and r["max_score"] is not None) else None,
        })
    period_total = sum(c["total"] for c in categories)

    # Agent活动摘要
    agent_stats = conn.execute(
        "SELECT agent_name, COUNT(*) as cnt, MAX(created_at) as last_time "
        "FROM agent_activities GROUP BY agent_name ORDER BY last_time DESC"
    ).fetchall()

    # 来源列表（供筛选下拉）
    source_names = conn.execute(
        "SELECT DISTINCT source_name FROM topics WHERE source_name IS NOT NULL AND source_name != '' "
        "ORDER BY source_name"
    ).fetchall()

    conn.close()
    overview = get_operations_overview()
    return templates.TemplateResponse(request, "dashboard.html", {
        "topics": topics_list,
        "dates": dates,
        "current_date": news_date,
        "current_status": status,
        "current_q": q or "",
        "current_source": source_name or "",
        "current_category": category or "",
        "current_sort": sort_key,
        "current_period": period,
        "current_scope": scope,
        "period_text": period_text,
        "period_range": period_range,
        "period_total": period_total,
        "categories": categories,
        "source_names": [s[0] for s in source_names],
        "total": total,
        "status_counts": status_counts,
        "date_range": date_range,
        "agent_stats": agent_stats,
        "overview": overview,
    })


@app.get("/topics/{topic_id}", response_class=HTMLResponse)
async def topic_detail_page(request: Request, topic_id: int):
    """选题详情 - 完整生命周期视图"""
    from app.config import get_db
    conn = get_db()

    topic = conn.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()
    if not topic:
        conn.close()
        return HTMLResponse("<h1>选题不存在</h1>", status_code=404)

    # 所有评分记录
    scores = conn.execute(
        "SELECT * FROM topic_scores WHERE topic_id = ? ORDER BY scored_at DESC",
        (topic_id,)
    ).fetchall()

    # 事件历史
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

    production_tasks = conn.execute(
        "SELECT * FROM production_tasks WHERE topic_id = ? ORDER BY created_at DESC",
        (topic_id,),
    ).fetchall()

    # 同日相关选题（同一天的其他新闻）
    related = []
    if topic["news_date"]:
        related = conn.execute(
            "SELECT id, title, source_name, status FROM topics "
            "WHERE news_date = ? AND id != ? ORDER BY id",
            (topic["news_date"], topic_id)
        ).fetchall()

    conn.close()
    return templates.TemplateResponse(request, "topic_detail.html", {
        "topic": topic,
        "scores": scores,
        "latest_score": scores[0] if scores else None,
        "events": events,
        "activities": activities,
        "task_files": task_files,
        "production_tasks": production_tasks,
        "related": related,
    })


@app.get("/agent", response_class=HTMLResponse)
async def agent_workspace_page(request: Request):
    """Agent工作台 - 自动化控制中心"""
    from app.config import get_db
    conn = get_db()

    # Agent活动统计
    agent_stats = conn.execute(
        "SELECT agent_name, COUNT(*) as cnt, MAX(created_at) as last_time "
        "FROM agent_activities GROUP BY agent_name ORDER BY last_time DESC"
    ).fetchall()

    # 最近活动
    recent_activities = conn.execute(
        "SELECT a.*, t.title as topic_title "
        "FROM agent_activities a "
        "LEFT JOIN topics t ON t.id = a.target_id AND a.target_type = 'topic' "
        "ORDER BY a.created_at DESC LIMIT 50"
    ).fetchall()

    # 待评分 = 还没有任何评分记录的素材（事实判定）
    pending_count = conn.execute(
        "SELECT COUNT(*) as c FROM topics t "
        "LEFT JOIN topic_scores s ON s.topic_id = t.id WHERE s.id IS NULL"
    ).fetchone()["c"]

    # 已评分数量
    scored_count = conn.execute(
        "SELECT COUNT(*) as c FROM topics t "
        "WHERE EXISTS (SELECT 1 FROM topic_scores s WHERE s.topic_id = t.id)"
    ).fetchone()["c"]

    # 进行中的生产任务数量（不再把 Topic 的 selected 当作生产状态）。
    selected_count = conn.execute(
        "SELECT COUNT(*) as c FROM production_tasks WHERE status IN ('queued', 'claimed', 'running', 'blocked')"
    ).fetchone()["c"]

    # 已完成发布稿数量；真实发布事实仅在 OPC-ERP 同步后可见。
    published_count = conn.execute(
        "SELECT COUNT(*) as c FROM production_tasks WHERE status = 'publish_ready'"
    ).fetchone()["c"]

    # 最近评分记录（如果有）
    recent_scores = conn.execute(
        "SELECT s.*, t.title as topic_title "
        "FROM topic_scores s "
        "JOIN topics t ON t.id = s.topic_id "
        "ORDER BY s.scored_at DESC LIMIT 20"
    ).fetchall()

    conn.close()
    return templates.TemplateResponse(request, "agent_workspace.html", {
        "agent_stats": agent_stats,
        "recent_activities": recent_activities,
        "pending_count": pending_count,
        "scored_count": scored_count,
        "selected_count": selected_count,
        "published_count": published_count,
        "recent_scores": recent_scores,
    })


@app.get("/production", response_class=HTMLResponse)
async def production_page(request: Request):
    """生产任务总览：任务是父对象，运行记录和回传是子对象。"""
    from app.services.production_service import list_tasks
    from app.config import get_all_settings
    return templates.TemplateResponse(request, "production.html", {
        "tasks": list_tasks(limit=120),
        "settings": get_all_settings(),
    })


@app.get("/reviews", response_class=HTMLResponse)
async def reviews_page(request: Request):
    from app.config import get_db
    conn = get_db()
    review_counts = {row["status"]: row["count"] for row in conn.execute("SELECT status, COUNT(*) AS count FROM review_cases GROUP BY status").fetchall()}
    method_counts = {row["status"]: row["count"] for row in conn.execute("SELECT status, COUNT(*) AS count FROM method_candidates GROUP BY status").fetchall()}
    conn.close()
    return templates.TemplateResponse(request, "reviews.html", {
        "review_counts": review_counts,
        "method_counts": method_counts,
    })


@app.get("/manual", response_class=HTMLResponse)
async def manual_entry_page(request: Request):
    return templates.TemplateResponse(request, "manual_entry.html", {})


@app.get("/events", response_class=HTMLResponse)
async def events_page(request: Request, news_date: str = Query(None), days: int = Query(7)):
    """事件归并：给 Agent 的工作手册（在软件内）+ 当前候选组 + 已归并事件线。"""
    from app.routers.events import event_candidates, event_threads
    cand = event_candidates(news_date=news_date, days=days, min_shared=1, limit=40)
    threads = event_threads(limit=50)
    manual_path = BASE_DIR / "docs" / "事件归并-Agent工作手册.md"
    manual_text = manual_path.read_text(encoding="utf-8") if manual_path.exists() else "（手册文件缺失）"
    return templates.TemplateResponse(request, "events.html", {
        "candidates": cand["groups"],
        "scanned": cand["scanned"],
        "threads": threads["threads"],
        "manual_text": manual_text,
        "current_date": news_date or "",
        "days": days,
    })


@app.get("/tree", response_class=HTMLResponse)
async def tree_page(
    request: Request,
    period: str = Query(_DEFAULT_PERIOD),
    cat: str = Query(None),
    region: str = Query(None),
    entity: str = Query(None),
):
    """归类三级树：分类 > 中国/美国/其他 > 政府机构或公司主体。

    周老师 2026-10-03 定：这是主浏览入口，替代原来的"事件归并"。
    分类由动词判定（app/verbs.py），事件线整线统一 —— 默认不跨分类。
    """
    from app.routers.tree import build_tree
    from app.statuses import period_label
    data = build_tree(period, cat, region, entity)
    label, rng = period_label(data["period"])
    return templates.TemplateResponse(request, "tree.html", {
        **data,
        "periods": _PERIODS,
        "period_label": label,
        "period_range": rng,
    })


@app.get("/coverage", response_class=HTMLResponse)
async def coverage_page(request: Request):
    from app.config import get_db
    conn = get_db()
    records = conn.execute(
        "SELECT * FROM coverage_records ORDER BY published_at DESC NULLS LAST"
    ).fetchall()
    conn.close()
    return templates.TemplateResponse(request, "coverage.html", {
        "records": records,
    })


@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    from app.config import get_db
    conn = get_db()
    profiles = conn.execute("SELECT * FROM expertise_profile ORDER BY weight DESC").fetchall()
    conn.close()
    return templates.TemplateResponse(request, "profile.html", {
        "profiles": profiles,
    })


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    from app.config import get_all_settings
    settings = get_all_settings()
    return templates.TemplateResponse(request, "settings.html", {
        "settings": settings,
    })
