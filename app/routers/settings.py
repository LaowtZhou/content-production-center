"""系统设置 API"""

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
from app.config import get_all_settings, set_setting

router = APIRouter()


class SettingsUpdate(BaseModel):
    openclaw_watch_dir: Optional[str] = None
    openclaw_cron_jobs_path: Optional[str] = None
    task_output_dir: Optional[str] = None
    llm_api_base: Optional[str] = None
    llm_api_key: Optional[str] = None
    llm_model: Optional[str] = None
    traffic_weight: Optional[str] = None
    opinion_weight: Optional[str] = None
    novelty_weight: Optional[str] = None
    expire_days: Optional[str] = None
    opc_erp_api_base: Optional[str] = None
    production_mode: Optional[str] = None
    daily_task_limit: Optional[str] = None
    content_vault_dir: Optional[str] = None
    content_skills_dir: Optional[str] = None
    task_contract_dir: Optional[str] = None
    production_center_base_url: Optional[str] = None


@router.get("/settings")
def get_settings():
    """获取所有设置"""
    return get_all_settings()


@router.put("/settings")
def update_settings(settings: SettingsUpdate):
    """更新设置"""
    updates = settings.dict(exclude_none=True)
    if updates.get("production_mode") not in (None, "manual", "autonomous"):
        return {"message": "生产模式只能是 manual 或 autonomous"}
    if "daily_task_limit" in updates:
        try:
            value = int(updates["daily_task_limit"])
        except (TypeError, ValueError):
            return {"message": "每日任务上限必须是 1 到 3"}
        if not 1 <= value <= 3:
            return {"message": "每日任务上限必须是 1 到 3"}
    for key, value in updates.items():
        set_setting(key, str(value))

    # 如果监听目录变了，重启文件监听
    if "openclaw_watch_dir" in updates:
        try:
            from app.services.file_watcher import restart_watcher
            restart_watcher()
        except Exception:
            pass

    return {"message": "设置已更新"}
