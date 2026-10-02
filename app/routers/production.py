"""创作任务、运行回传和 OPC-ERP 发布观察 API。"""

import uuid
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import FileResponse
from app.services import stage_audit
from pydantic import BaseModel, Field

from app.config import get_db
from app.services import production_service
from app.services import feedback_service

router = APIRouter()


class TaskCreate(BaseModel):
    requested_platform: str = "wechat"
    requested_deliverables: list[str] = Field(default_factory=lambda: ["wechat_publish_ready"])
    executor_hint: str = "Codex"
    selected_note: str = ""
    mode: Optional[str] = None


class TaskClaim(BaseModel):
    agent_name: str = "Codex"


class TaskClaimNext(BaseModel):
    agent_name: str = "Codex"


class TaskCallback(BaseModel):
    callback_token: str
    status: str
    agent_name: str = "Codex"
    summary: str = ""
    blocked_reason: str = ""
    error_detail: str = ""
    vault_project_path: str = ""
    primary_article_path: str = ""
    artifacts: list[Any] = Field(default_factory=list)
    used_skills: list[Any] = Field(default_factory=list)
    self_check: dict[str, Any] = Field(default_factory=dict)


class StageStart(BaseModel):
    agent_name: str = "Codex"


class StageComplete(BaseModel):
    agent_name: str = "Codex"
    output_paths: list[str] = Field(default_factory=list)
    result: dict[str, Any] = Field(default_factory=dict)


class StageFailure(BaseModel):
    agent_name: str = "Codex"
    error_detail: str
    result: dict[str, Any] = Field(default_factory=dict)


class ObservationMatch(BaseModel):
    task_id: Optional[str] = None
    topic_id: Optional[int] = None
    matched_by: str = "human"


class ReviewFlag(BaseModel):
    reason: str
    created_by: str = "human"


class PdcaReviewFlag(BaseModel):
    opc_publication_id: str
    reason: str
    created_by: str = "opc_erp_pdca"


class FeedbackHandle(BaseModel):
    status: str
    handled_by: str = "human"
    note: str = ""


class FeedbackReview(BaseModel):
    created_by: str = "feedback_layer"


def _handle(action):
    try:
        return action()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"操作失败：{exc}") from exc


@router.get("/production/tasks")
def tasks(status: Optional[str] = None, limit: int = Query(100, ge=1, le=500)):
    return production_service.list_tasks(status, limit)


@router.get("/production/notifications")
def notifications(since: Optional[str] = None, limit: int = Query(20, ge=1, le=100)):
    return production_service.list_publish_ready_notifications(since, limit)


@router.get("/production/tasks/auto-queue")
def auto_queue_method_hint():
    """GET 只返回方法提示，避免 Agent 误调用时产生重复任务。"""
    return {
        "ok": False,
        "method_required": "POST",
        "endpoint": "/api/production/tasks/auto-queue",
        "message": "自动派单是写操作，请改用 POST；本次 GET 没有创建任务。",
    }


@router.post("/production/tasks/claim-next")
def claim_next(body: TaskClaimNext = Body(default=TaskClaimNext())):
    """供 Codex 等执行器领取下一条任务，领取与排他锁在服务端完成。"""
    return _handle(lambda: production_service.claim_next_task(body.agent_name))


@router.get("/production/tasks/{task_id}")
def task_detail(task_id: str):
    task = production_service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="创作任务不存在")
    return task


@router.post("/topics/{topic_id}/production-task")
def queue_task(topic_id: int, body: TaskCreate):
    return _handle(lambda: production_service.create_task(
        topic_id,
        mode=body.mode,
        requested_platform=body.requested_platform,
        requested_deliverables=body.requested_deliverables,
        executor_hint=body.executor_hint,
        selected_note=body.selected_note,
        created_by="human",
    ))


@router.post("/production/tasks/auto-queue")
def auto_queue(limit: Optional[int] = Query(None, ge=1, le=3), body: Optional[dict[str, Any]] = None):
    requested_limit = limit
    if requested_limit is None and body and body.get("limit") is not None:
        try:
            requested_limit = int(body["limit"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="limit 必须是 1-3 的整数") from exc
        if not 1 <= requested_limit <= 3:
            raise HTTPException(status_code=400, detail="limit 必须是 1-3 的整数")
    return _handle(lambda: production_service.auto_queue_tasks(requested_limit))


@router.post("/production/tasks/{task_id}/claim")
def claim(task_id: str, body: TaskClaim):
    return _handle(lambda: production_service.claim_task(task_id, body.agent_name))


@router.post("/production/tasks/{task_id}/start")
def start(task_id: str, body: TaskClaim):
    return _handle(lambda: production_service.start_task(task_id, body.agent_name))


@router.post("/production/tasks/{task_id}/retry")
def retry(task_id: str, body: TaskClaim = Body(default=TaskClaim(agent_name="human"))):
    return _handle(lambda: production_service.retry_task(task_id, body.agent_name))


@router.post("/production/tasks/{task_id}/callback")
def callback(task_id: str, body: TaskCallback):
    return _handle(lambda: production_service.callback_task(task_id, body.model_dump()))


@router.post("/production/tasks/{task_id}/reconcile")
def reconcile(task_id: str):
    """只在四阶段证据完整且主编终审通过时恢复被执行器退出误收敛的任务。"""
    return _handle(lambda: production_service.reconcile_completed_task(task_id))


@router.post("/production/tasks/recover-stale")
def recover_stale(max_age_minutes: int = Query(30, ge=5, le=1440)):
    """结束失联执行器留下的 claimed/running 任务；不会自动重试。"""
    return _handle(lambda: production_service.recover_stale_tasks(max_age_minutes, requested_by="api"))


@router.get("/production/tasks/{task_id}/stages")
def stages(task_id: str):
    task = production_service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="创作任务不存在")
    return task["stage_summary"]


@router.get('/production/tasks/{task_id}/stages/{stage_key}/audit')
def stage_audit_detail(task_id: str, stage_key: str):
    return _handle(lambda: stage_audit.audit(task_id, stage_key))


@router.get('/production/tasks/{task_id}/images/{image_id}')
def stage_image(task_id: str, image_id: str):
    try:
        path = stage_audit.image_path(task_id, image_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})


@router.post("/production/tasks/{task_id}/stages/{stage_key}/start")
def start_stage(task_id: str, stage_key: str, body: StageStart):
    return _handle(lambda: production_service.start_stage(task_id, stage_key, body.agent_name))


@router.post("/production/stages/{stage_id}/complete")
def complete_stage(stage_id: str, body: StageComplete):
    return _handle(lambda: production_service.complete_stage(stage_id, body.output_paths, body.result, body.agent_name))


@router.post("/production/stages/{stage_id}/fail")
def fail_stage(stage_id: str, body: StageFailure):
    return _handle(lambda: production_service.fail_stage(stage_id, body.error_detail, body.result, body.agent_name))


@router.post("/production/opc-sync")
def sync_opc():
    return _handle(production_service.sync_opc_performance)


@router.post("/production/feedback/sync")
def sync_feedback():
    """同步 ERP 事实并计算内容中心派生反馈，不向 ERP 写入。"""
    return _handle(feedback_service.sync_and_generate_feedback)


@router.post("/production/feedback/recompute")
def recompute_feedback():
    return _handle(feedback_service.recompute_signals)


@router.get("/production/feedback")
def feedback(status: Optional[str] = None, platform: Optional[str] = None, signal_type: Optional[str] = None, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    return feedback_service.list_signals(status, platform, signal_type, limit, offset)


@router.get("/production/feedback/summary")
def feedback_summary():
    return feedback_service.signal_summary()


@router.post("/production/feedback/{signal_id}/handle")
def handle_feedback(signal_id: str, body: FeedbackHandle):
    return _handle(lambda: feedback_service.handle_signal(signal_id, body.status, body.handled_by, body.note))


@router.post("/production/feedback/{signal_id}/create-review")
def create_feedback_review(signal_id: str, body: FeedbackReview):
    return _handle(lambda: feedback_service.create_review_from_signal(signal_id, body.created_by))






@router.get("/production/publications")
def publications(status: Optional[str] = None, limit: int = Query(100, ge=1, le=500)):
    conn = get_db()
    try:
        sql = """SELECT observation.*, task.status AS task_status, task.vault_project_path, topic.title AS topic_title
        FROM publication_observations observation
        LEFT JOIN production_tasks task ON task.id = observation.task_id
        LEFT JOIN topics topic ON topic.id = observation.topic_id"""
        params: list[Any] = []
        if status:
            sql += " WHERE observation.status = ?"
            params.append(status)
        sql += " ORDER BY COALESCE(observation.published_at, observation.created_at) DESC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


@router.post("/production/publications/{observation_id}/match")
def match_publication(observation_id: str, body: ObservationMatch):
    conn = get_db()
    try:
        observation = conn.execute("SELECT * FROM publication_observations WHERE id = ?", (observation_id,)).fetchone()
        if not observation:
            raise HTTPException(status_code=404, detail="发布观察不存在")
        if not body.task_id and not body.topic_id:
            raise HTTPException(status_code=400, detail="至少要关联一个创作任务或选题")
        topic_id = body.topic_id
        if body.task_id:
            task = conn.execute("SELECT topic_id FROM production_tasks WHERE id = ?", (body.task_id,)).fetchone()
            if not task:
                raise HTTPException(status_code=400, detail="创作任务不存在")
            if topic_id and topic_id != task["topic_id"]:
                raise HTTPException(status_code=400, detail="选题与创作任务不一致")
            topic_id = task["topic_id"]
        conn.execute(
            """UPDATE publication_observations SET task_id = ?, topic_id = ?, status = 'matched', matched_by = ?, matched_at = ?, updated_at = ? WHERE id = ?""",
            (body.task_id, topic_id, body.matched_by, production_service.now_iso(), production_service.now_iso(), observation_id),
        )
        if topic_id:
            production_service._record_topic_event(conn, topic_id, "publication_observation_matched", {"observation_id": observation_id, "task_id": body.task_id, "matched_by": body.matched_by})
        conn.commit()
        return {"message": "已关联发布观察；这不改变平台发布事实", "observation_id": observation_id}
    finally:
        conn.close()


@router.post("/production/publications/{observation_id}/flag-review")
def flag_review(observation_id: str, body: ReviewFlag):
    conn = get_db()
    try:
        observation = conn.execute("SELECT * FROM publication_observations WHERE id = ?", (observation_id,)).fetchone()
        if not observation:
            raise HTTPException(status_code=404, detail="发布观察不存在")
        existing = conn.execute("SELECT id FROM review_cases WHERE observation_id = ? AND status != 'closed'", (observation_id,)).fetchone()
        if existing:
            return {"message": "已有待处理复盘", "review_case_id": existing["id"]}
        timestamp = production_service.now_iso()
        review_id = str(uuid.uuid4())
        conn.execute("UPDATE publication_observations SET status = 'review_candidate', review_flag_reason = ?, updated_at = ? WHERE id = ?", (body.reason, timestamp, observation_id))
        conn.execute(
            """INSERT INTO review_cases (id, observation_id, task_id, topic_id, status, trigger_type, trigger_reason,
            title_snapshot, performance_snapshot_json, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'candidate', 'performance_flag', ?, ?, ?, ?, ?, ?)""",
            (review_id, observation_id, observation["task_id"], observation["topic_id"], body.reason, observation["title"], observation["metrics_json"], body.created_by, timestamp, timestamp),
        )
        conn.commit()
        return {"message": "已进入复盘候选区", "review_case_id": review_id}
    finally:
        conn.close()


@router.post("/production/reviews/from-pdca")
def flag_review_from_pdca(body: PdcaReviewFlag):
    """为未来 OPC-ERP PDCA 自动化预留的低风险入口：只创建复盘候选。"""
    conn = get_db()
    try:
        observation = conn.execute(
            "SELECT id FROM publication_observations WHERE opc_publication_id = ?",
            (body.opc_publication_id,),
        ).fetchone()
        if not observation:
            raise HTTPException(status_code=404, detail="尚未同步对应发布观察；请先同步 OPC-ERP 内容表现")
    finally:
        conn.close()
    return flag_review(observation["id"], ReviewFlag(reason=body.reason, created_by=body.created_by))
