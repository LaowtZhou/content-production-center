"""复盘案例、方法候选与 Skill 升格记录 API。"""

import json
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.config import get_db
from app.services.production_service import now_iso

router = APIRouter()


class ReviewUpdate(BaseModel):
    status: Optional[str] = None
    human_notes: Optional[str] = None
    ai_notes: Optional[str] = None
    conclusion: Optional[str] = None


class MethodCreate(BaseModel):
    title: str
    method_text: str
    evidence_text: str = ""
    target_skill_name: str = "content-creation-workflow"
    proposed_by: str = "human"


class MethodPromote(BaseModel):
    promotion_note: str = ""


@router.get("/reviews")
def list_reviews(status: Optional[str] = None, limit: int = Query(100, ge=1, le=500)):
    conn = get_db()
    try:
        sql = """SELECT review.*, observation.platform, observation.external_url, observation.published_at,
        task.vault_project_path, topic.title AS topic_title
        FROM review_cases review
        LEFT JOIN publication_observations observation ON observation.id = review.observation_id
        LEFT JOIN production_tasks task ON task.id = review.task_id
        LEFT JOIN topics topic ON topic.id = review.topic_id"""
        params = []
        if status:
            sql += " WHERE review.status = ?"
            params.append(status)
        sql += " ORDER BY CASE review.status WHEN 'candidate' THEN 0 WHEN 'in_review' THEN 1 ELSE 2 END, review.created_at DESC LIMIT ?"
        params.append(limit)
        rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
        for row in rows:
            row["methods"] = [dict(method) for method in conn.execute("SELECT * FROM method_candidates WHERE review_case_id = ? ORDER BY created_at DESC", (row["id"],)).fetchall()]
        return rows
    finally:
        conn.close()


@router.put("/reviews/{review_id}")
def update_review(review_id: str, body: ReviewUpdate):
    allowed = {"candidate", "in_review", "closed"}
    if body.status and body.status not in allowed:
        raise HTTPException(status_code=400, detail="复盘状态无效")
    conn = get_db()
    try:
        existing = conn.execute("SELECT * FROM review_cases WHERE id = ?", (review_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="复盘案例不存在")
        timestamp = now_iso()
        status = body.status or existing["status"]
        conn.execute(
            """UPDATE review_cases SET status = ?, human_notes = ?, ai_notes = ?, conclusion = ?,
            closed_at = ?, updated_at = ? WHERE id = ?""",
            (
                status,
                body.human_notes if body.human_notes is not None else existing["human_notes"],
                body.ai_notes if body.ai_notes is not None else existing["ai_notes"],
                body.conclusion if body.conclusion is not None else existing["conclusion"],
                timestamp if status == "closed" else None,
                timestamp,
                review_id,
            ),
        )
        conn.commit()
        return {"message": "复盘已保存", "status": status}
    finally:
        conn.close()


@router.post("/reviews/{review_id}/methods")
def create_method(review_id: str, body: MethodCreate):
    if not body.title.strip() or not body.method_text.strip():
        raise HTTPException(status_code=400, detail="方法标题和内容不能为空")
    conn = get_db()
    try:
        review = conn.execute("SELECT id FROM review_cases WHERE id = ?", (review_id,)).fetchone()
        if not review:
            raise HTTPException(status_code=404, detail="复盘案例不存在")
        method_id = str(uuid.uuid4())
        timestamp = now_iso()
        conn.execute(
            """INSERT INTO method_candidates
            (id, review_case_id, title, method_text, evidence_text, target_skill_name, status, proposed_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'candidate', ?, ?, ?)""",
            (method_id, review_id, body.title.strip(), body.method_text.strip(), body.evidence_text.strip(), body.target_skill_name.strip(), body.proposed_by.strip(), timestamp, timestamp),
        )
        conn.commit()
        return {"message": "已沉淀为方法候选，尚未改写正式 SKILL", "method_id": method_id}
    finally:
        conn.close()


@router.get("/methods")
def list_methods(status: Optional[str] = None, limit: int = Query(100, ge=1, le=500)):
    conn = get_db()
    try:
        sql = "SELECT * FROM method_candidates"
        params = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY CASE status WHEN 'candidate' THEN 0 WHEN 'promoted' THEN 1 ELSE 2 END, created_at DESC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


@router.post("/methods/{method_id}/promote")
def promote_method(method_id: str, body: MethodPromote):
    """人工确认升格：记录待写入哪份 Skill，不擅自改动内容 vault 的规则文件。"""
    conn = get_db()
    try:
        method = conn.execute("SELECT * FROM method_candidates WHERE id = ?", (method_id,)).fetchone()
        if not method:
            raise HTTPException(status_code=404, detail="方法候选不存在")
        if method["status"] == "promoted":
            return {"message": "该方法已经标记为待写入 SKILL"}
        timestamp = now_iso()
        conn.execute(
            """UPDATE method_candidates SET status = 'promoted', promoted_at = ?, promotion_note = ?, updated_at = ? WHERE id = ?""",
            (timestamp, body.promotion_note, timestamp, method_id),
        )
        conn.commit()
        return {"message": f"已确认升格为 {method['target_skill_name']} 的待更新规则；请在修改 SKILL 后由下一次任务重新采集版本。"}
    finally:
        conn.close()


@router.get("/skills")
def list_skill_versions(limit: int = Query(200, ge=1, le=500)):
    conn = get_db()
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM skill_versions ORDER BY discovered_at DESC LIMIT ?", (limit,)).fetchall()]
    finally:
        conn.close()
