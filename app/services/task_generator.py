"""兼容旧入口的任务生成器。

旧版只生成一份 Markdown。本模块保留函数名，实际委托给生产编排服务创建
有状态、可回传、带 Skill 版本快照的任务契约。
"""

from app.services.production_service import create_task


def generate_task_file(topic_id: int) -> str:
    result = create_task(topic_id)
    task = result["task"]
    return result.get("contract_path") or task.get("contract_path") or ""
