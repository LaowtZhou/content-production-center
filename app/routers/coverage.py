"""覆盖记录 API"""

import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from app.config import get_db

router = APIRouter()


class CoverageCreate(BaseModel):
    topic_id: Optional[int] = None
    title: str
    platform: str = "bilibili"
    published_at: Optional[str] = None
    content_url: str = ""
    key_topics: list = []


@router.get("/coverage")
def list_coverage(limit: int = 100):
    """获取覆盖记录列表"""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM coverage_records ORDER BY "
        "COALESCE(published_at, created_at) DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.post("/coverage")
def create_coverage(record: CoverageCreate):
    """添加覆盖记录"""
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO coverage_records "
        "(topic_id, title, platform, published_at, content_url, key_topics) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (record.topic_id, record.title, record.platform,
         record.published_at, record.content_url,
         json.dumps(record.key_topics, ensure_ascii=False))
    )
    record_id = cur.lastrowid
    conn.commit()
    conn.close()
    return {"id": record_id, "message": "覆盖记录已添加"}


@router.delete("/coverage/{record_id}")
def delete_coverage(record_id: int):
    """删除覆盖记录"""
    conn = get_db()
    conn.execute("DELETE FROM coverage_records WHERE id = ?", (record_id,))
    conn.commit()
    conn.close()
    return {"message": "已删除"}


@router.get("/coverage/check")
def check_coverage(query: str):
    """检查关键词是否已被覆盖"""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM coverage_records WHERE key_topics LIKE ?",
        (f"%{query}%",)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
