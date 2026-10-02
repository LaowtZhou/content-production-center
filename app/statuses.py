"""选题"使用状态"与"统计周期"——唯一事实来源。

背景（2026-10-03 周老师定）：
平台只做选题供给（素材库）。素材不存在"生产中/已发布"这类流水线状态，
也不存在自动过期；只回答一个问题——**这条素材我用没用**。

因此 topics.status 只有三个合法值：
- unused  未用：还没拿去写稿（默认值）
- used    已用：周老师打过钩，确认拿它写过东西
- dropped 弃用：明确不会用它

旧机器状态（new/scored/selected/scoring/abandoned/expired）通过
LEGACY_STATUS_MAP 一次性收敛，之后不允许再出现（见 scripts/init_db.py 的启动迁移）。

"是否已评分"不是状态，而是事实：topic_scores 里有没有记录。
这样同一件事只有一份实现，不会出现状态与评分记录互相打架。
"""

from __future__ import annotations

from datetime import date

DEFAULT_USAGE = "unused"

USAGE_STATUSES = ("unused", "used", "dropped")

USAGE_LABELS = {
    "unused": "未用",
    "used": "已用",
    "dropped": "弃用",
}

USAGE_DESCRIPTIONS = {
    "unused": "还没拿去写稿的素材",
    "used": "已确认用它写过内容",
    "dropped": "明确不会用这条素材",
}

# 旧流水线状态 → 使用状态。启动迁移据此收敛，映射关系只写在这一处。
LEGACY_STATUS_MAP = {
    "new": "unused",
    "scored": "unused",
    "selected": "unused",
    "scoring": "unused",
    "abandoned": "dropped",
    "expired": "dropped",
    "producing": "unused",
    "published": "unused",
}

# 列表筛选里的两个"事实型"虚拟筛选项：按有无评分记录过滤，而不是按 status。
SCORE_FILTERS = {"unscored": "待评分", "scored": "已评分"}

# ===== 类别聚焦的统计周期 =====
# key 用于 URL 参数 period；label 用于展示；days 表示"含今天在内往前几天"。
PERIODS = [
    {"key": "7d", "label": "近7日", "days": 7},
    {"key": "30d", "label": "近30日", "days": 30},
    {"key": "quarter", "label": "本季度", "days": None},
    {"key": "year", "label": "本年", "days": None},
    {"key": "all", "label": "历史全部", "days": None},
]

PERIOD_KEYS = [p["key"] for p in PERIODS]
DEFAULT_PERIOD = "7d"

PERIOD_LABELS = {p["key"]: p["label"] for p in PERIODS}


def period_start(period: str, today: date | None = None) -> str | None:
    """返回该周期的起始日期（含当天），'历史全部' 返回 None。"""
    today = today or date.today()
    if period == "7d":
        days = 7
    elif period == "30d":
        days = 30
    elif period == "quarter":
        first_month = (today.month - 1) // 3 * 3 + 1
        return today.replace(month=first_month, day=1).isoformat()
    elif period == "year":
        return today.replace(month=1, day=1).isoformat()
    else:
        return None
    return date.fromordinal(today.toordinal() - (days - 1)).isoformat()


def is_valid_period(period: str | None) -> bool:
    return period in PERIOD_KEYS


def period_label(period: str) -> tuple[str, str]:
    """返回 (周期名, 区间说明)，用于把统计口径直接写在界面上，避免数字对不上。"""
    start = period_start(period)
    label = PERIOD_LABELS.get(period, PERIOD_LABELS[DEFAULT_PERIOD])
    if not start:
        return label, "全部历史"
    return label, f"{start[5:]} 起"
