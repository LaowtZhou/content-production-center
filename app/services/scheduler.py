"""定时任务调度器 - 运营器唤醒与反馈同步。

说明：2026-10-03 起不再有"过期选题"概念（周老师明确：素材长期保留、不自动消失），
原每日凌晨 3 点的自动过期任务已整体移除。
"""

from apscheduler.schedulers.background import BackgroundScheduler
from pathlib import Path
import subprocess

_scheduler = None
_last_error = None
CODEX_OPERATOR_DISABLE_MARKER = (
    Path(__file__).resolve().parents[2] / "data" / "codex-operator.disabled"
)


def launch_codex_operator() -> dict:
    """启动单个 Codex 运营执行器；执行器内部用文件锁保证幂等。"""
    project_root = Path(__file__).resolve().parents[2]
    runner = project_root / "scripts" / "run_codex_operator.ps1"
    if not runner.is_file():
        raise FileNotFoundError(f"Codex 运营执行器不存在: {runner}")

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(runner),
            # 内容库是本系统的受控工作区。用户已明确授权自动内容运营写入，
            # 调度器必须把授权传到运行器，否则后台任务永远只能停在策划阶段。
            "-AllowUnrestrictedCodex",
        ],
        cwd=str(project_root),
        creationflags=creation_flags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {"started": True, "pid": process.pid, "runner": str(runner)}


def start_scheduler():
    """启动定时任务"""
    global _scheduler, _last_error
    if _scheduler and _scheduler.running:
        return {"status": "running", "message": "定时任务已在运行"}
    _scheduler = BackgroundScheduler()
    # 内容中心脚本只负责唤醒 Codex 运营器，不直接评分、派单或创建内容任务。
    # 运营计划、评分、派单、领取和四阶段创作全部由 Codex 通过 8774 完成。
    def content_pipeline_cycle():
        try:
            launch_result = launch_codex_operator()
            print(f"[定时任务] 已启动 Codex 内容运营执行器 pid={launch_result['pid']}")
        except Exception as exc:
            print(f"[定时任务] Codex 内容运营启动失败: {exc}")

    if CODEX_OPERATOR_DISABLE_MARKER.is_file():
        print(f"[定时任务] Codex 内容运营执行器已停用：{CODEX_OPERATOR_DISABLE_MARKER}")
    else:
        _scheduler.add_job(content_pipeline_cycle, "interval", minutes=30, id="content_pipeline", max_instances=1, coalesce=True)
    # ERP 是事实源；反馈层定时只读同步并计算派生信号，未配置时自动降级。
    def sync_feedback_loop():
        try:
            from app.services.feedback_service import sync_and_generate_feedback
            result = sync_and_generate_feedback()
            signal_result = result.get("signals", {})
            if signal_result.get("inserted"):
                print(f"[定时任务] 内容反馈新增 {signal_result['inserted']} 条信号")
        except ValueError:
            pass
        except Exception as exc:
            print(f"[定时任务] 内容反馈同步失败: {exc}")

    _scheduler.add_job(sync_feedback_loop, "interval", hours=1, id="feedback_sync", max_instances=1, coalesce=True)

    _scheduler.start()
    _last_error = None
    print("[定时任务] 调度器已启动")
    return {"status": "running"}


def stop_scheduler():
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        print("[定时任务] 调度器已停止")


def scheduler_status() -> dict:
    if _scheduler and _scheduler.running:
        return {"status": "running", "jobs": len(_scheduler.get_jobs())}
    return {"status": "stopped", "message": _last_error or "调度器未启动"}
