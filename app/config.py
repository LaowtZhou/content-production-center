"""配置管理 - 从数据库system_settings表读取配置，提供默认值"""

import sqlite3
import os
from pathlib import Path
from app.runtime import APP_BASE_URL

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent

# 数据库路径
DB_PATH = BASE_DIR / "data" / "production_center.db"

# 模板目录
TEMPLATE_DIR = BASE_DIR / "app" / "templates"

# 静态文件目录
STATIC_DIR = BASE_DIR / "static"

# 默认配置
DEFAULTS = {
    "openclaw_watch_dir": "",
    "openclaw_cron_jobs_path": "",
    "task_output_dir": "",
    "llm_api_base": "https://api.openai.com/v1",
    "llm_api_key": "",
    "llm_model": "gpt-4o-mini",
    "traffic_weight": "0.4",
    "opinion_weight": "0.4",
    "novelty_weight": "0.2",
    "expire_days": "7",
    "opc_erp_api_base": "",
    "production_mode": "manual",
    "daily_task_limit": "1",
    "content_vault_dir": "",
    "content_skills_dir": "",
    "task_contract_dir": "",
    "production_center_base_url": APP_BASE_URL,
}


def get_db():
    """获取数据库连接"""
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_setting(key: str, default: str = "") -> str:
    """读取单个配置项"""
    conn = get_db()
    row = conn.execute(
        "SELECT value FROM system_settings WHERE key = ?", (key,)
    ).fetchone()
    conn.close()
    if row and row["value"]:
        return row["value"]
    return default or DEFAULTS.get(key, "")


def get_all_settings() -> dict:
    """读取所有配置"""
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM system_settings").fetchall()
    conn.close()
    result = dict(DEFAULTS)
    for row in rows:
        result[row["key"]] = row["value"]
    return result


def set_setting(key: str, value: str):
    """更新配置项"""
    conn = get_db()
    conn.execute(
        "INSERT INTO system_settings (key, value, updated_at) "
        "VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = CURRENT_TIMESTAMP",
        (key, str(value), str(value)),
    )
    conn.commit()
    conn.close()


def get_float_setting(key: str, default: float = 0.0) -> float:
    """读取浮点配置"""
    val = get_setting(key)
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def get_int_setting(key: str, default: int = 0) -> int:
    """读取整数配置"""
    val = get_setting(key)
    try:
        return int(val)
    except (ValueError, TypeError):
        return default
