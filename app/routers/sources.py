"""来源管理 API"""

from fastapi import APIRouter
from app.config import get_db, get_setting

router = APIRouter()


@router.get("/sources")
def list_sources(limit: int = 50):
    """获取来源批次列表"""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM topic_sources ORDER BY delivered_at DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.get("/source-issues")
def list_source_issues(status: str = "open", limit: int = 100):
    """查询来源读取、解析和格式异常，供主流程监控和重试工具使用。"""
    conn = get_db()
    try:
        sql = """SELECT issue.*, source.source_ref, source.file_hash, source.processed
                 FROM source_issues issue
                 JOIN topic_sources source ON source.id = issue.source_id"""
        params = []
        if status:
            sql += " WHERE issue.status = ?"
            params.append(status)
        sql += " ORDER BY issue.created_at DESC LIMIT ?"
        params.append(min(max(limit, 1), 500))
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


@router.post("/sources/scan")
def scan_directory():
    """手动触发文件扫描"""
    watch_dir = get_setting("openclaw_watch_dir", "")
    if not watch_dir:
        return {"error": "未配置OpenClaw监听目录"}
    from app.services.topic_parser import scan_directory as do_scan
    return do_scan(watch_dir)
