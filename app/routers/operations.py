"""统一内容运营主线的摘要和 Codex 运营闭环 API。"""

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from app.config import get_db
from app.services.operations_service import get_operations_overview
from app.services import operator_service

router = APIRouter()


@router.get("/operations/overview")
def operations_overview():
    return get_operations_overview()


@router.get("/operations/operator-context")
def operator_context(limit: int = Query(20, ge=1, le=100)):
    """供 Codex 主编读取 ERP 表现、反馈信号、采集任务和生产队列。"""
    return operator_service.get_operator_context(limit)


@router.get("/operations/collector-jobs")
def collector_jobs():
    """只返回白名单采集任务摘要，不暴露完整任务提示。"""
    return operator_service.get_collector_jobs()


@router.get("/operations/cycles")
def operator_cycles(status: str | None = None, limit: int = Query(50, ge=1, le=200)):
    conn = get_db()
    try:
        sql = "SELECT * FROM operator_cycles"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


@router.post("/operations/operator-plan")
def operator_plan(payload: dict[str, Any] = Body(...)):
    """幂等应用一轮 Codex 运营计划。"""
    try:
        return operator_service.apply_operator_plan(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"运营计划执行失败：{exc}") from exc
