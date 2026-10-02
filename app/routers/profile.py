"""专业画像 API"""

import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.config import get_db

router = APIRouter()


class ProfileCreate(BaseModel):
    area: str
    description: str = ""
    weight: float = 0.5
    keywords: list = []


class ProfileUpdate(BaseModel):
    area: str = ""
    description: str = ""
    weight: float = 0.5
    keywords: list = []


@router.get("/profile")
def list_profile():
    """获取专业画像列表"""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM expertise_profile ORDER BY weight DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.post("/profile")
def create_profile(profile: ProfileCreate):
    """添加专业领域"""
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO expertise_profile (area, description, weight, keywords) "
        "VALUES (?, ?, ?, ?)",
        (profile.area, profile.description, profile.weight,
         json.dumps(profile.keywords, ensure_ascii=False))
    )
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return {"id": pid, "message": "专业领域已添加"}


@router.put("/profile/{profile_id}")
def update_profile(profile_id: int, profile: ProfileUpdate):
    """更新专业领域"""
    conn = get_db()
    existing = conn.execute(
        "SELECT * FROM expertise_profile WHERE id = ?", (profile_id,)
    ).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(status_code=404, detail="专业领域不存在")

    area = profile.area or existing["area"]
    desc = profile.description if profile.description is not None else existing["description"]
    weight = profile.weight if profile.weight > 0 else existing["weight"]
    kws = json.dumps(profile.keywords, ensure_ascii=False) if profile.keywords else existing["keywords"]

    conn.execute(
        "UPDATE expertise_profile SET area = ?, description = ?, weight = ?, "
        "keywords = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (area, desc, weight, kws, profile_id)
    )
    conn.commit()
    conn.close()
    return {"message": "已更新"}


@router.delete("/profile/{profile_id}")
def delete_profile(profile_id: int):
    """删除专业领域"""
    conn = get_db()
    conn.execute("DELETE FROM expertise_profile WHERE id = ?", (profile_id,))
    conn.commit()
    conn.close()
    return {"message": "已删除"}
