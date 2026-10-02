"""LLM 评分引擎 - 对选题进行三维评分"""

import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from statistics import median

from app.config import (
    get_db, get_setting, get_float_setting
)

PROMPT_VERSION = "v2.0"

SCORING_PROMPT_TEMPLATE = """你是一位AI内容选题评估专家。请对以下选题进行评估。

## 选题信息
标题：{title}
摘要：{summary}
原文：{raw_content}
来源：{source_type}

## 创作者专业画像
{expertise_profile}

## 近期已覆盖的主题关键词
{recent_coverage_keywords}

## 已发布内容与表现参考
{performance_references}

## 全量历史表现摘要
{performance_summary}

## 已沉淀的方法候选
{learning_methods}

## 评估要求
请从以下三个维度评分（0-10分），并给出详细理由：

1. 流量潜力（traffic_score）：这个选题有多大概率获得高流量？
   - 8-10分：重大发布/行业地震级别，几乎必然爆款
   - 5-7分：高关注度话题，有明确流量信号
   - 3-5分：中等关注度，需要好角度才能有流量
   - 1-3分：小众话题，流量天花板低

2. 观点匹配（opinion_score）：创作者对这个选题有多大概率有独特观点？
   - 8-10分：完全在创作者专业领域，且有明显可输出的独特观点
   - 5-7分：在创作者专业领域，有较好的观点基础
   - 3-5分：部分相关，需要较多研究才能形成观点
   - 1-3分：不在创作者专业领域，难以形成独特观点

3. 新颖度（novelty_score）：这个选题是否已被充分覆盖？
   - 8-10分：全新信息，几乎无人覆盖
   - 5-7分：较新，有差异化角度的空间
   - 3-5分：已被讨论，但还有新角度可挖
   - 1-3分：已被充分覆盖，很难做出差异化

## 输出格式
请只返回JSON，不要其他文字：
{{
  "traffic_score": 0-10的数字,
  "opinion_score": 0-10的数字,
  "novelty_score": 0-10的数字,
  "traffic_reasoning": "流量评分理由",
  "opinion_reasoning": "观点评分理由",
  "novelty_reasoning": "新颖度评分理由",
  "recommended_format": "video或article或xiaohongshu",
  "format_reasoning": "推荐形式理由",
  "key_angle": "建议的创作角度（一句话）"
}}
"""


def _get_expertise_profile_text(conn) -> str:
    """获取专业画像文本"""
    rows = conn.execute(
        "SELECT area, description, keywords FROM expertise_profile ORDER BY weight DESC"
    ).fetchall()
    if not rows:
        return "暂无专业画像数据"
    lines = []
    for r in rows:
        kws = json.loads(r["keywords"]) if r["keywords"] else []
        lines.append(f"- {r['area']}：{r['description']}（关键词：{', '.join(kws)}）")
    return "\n".join(lines)


def _get_recent_coverage_keywords(conn, limit=50) -> str:
    """获取近期覆盖的关键词"""
    rows = conn.execute(
        "SELECT key_topics FROM coverage_records ORDER BY published_at DESC LIMIT ?",
        (limit,)
    ).fetchall()
    if not rows:
        return "暂无覆盖记录"
    all_kws = set()
    for r in rows:
        kws = json.loads(r["key_topics"]) if r["key_topics"] else []
        all_kws.update(kws)
    return ", ".join(sorted(all_kws)) if all_kws else "暂无"


def _get_performance_references(conn, limit=30) -> str:
    """把 OPC-ERP 的只读发布观察作为下一轮选题的参考，而不是强制标题去重。"""
    rows = conn.execute(
        """SELECT title, platform, metrics_json, status FROM publication_observations
        ORDER BY COALESCE(published_at, created_at) DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    if not rows:
        return "暂无已同步发布表现；不要假设历史内容已经覆盖。"
    lines = []
    for row in rows:
        try:
            metrics = json.loads(row["metrics_json"] or "{}")
        except json.JSONDecodeError:
            metrics = {}
        signal = metrics.get("latest_read_count") or metrics.get("latest_play_count") or "—"
        lines.append(f"- {row['title']}（{row['platform'] or '未知平台'}，阅读/播放 {signal}，状态 {row['status']}）")
    return "\n".join(lines)


def _get_performance_summary(conn) -> str:
    """汇总全部 ERP 发布观察，避免评分只依赖最近明细。"""
    rows = conn.execute(
        "SELECT title, platform, metrics_json FROM publication_observations"
    ).fetchall()
    by_platform = defaultdict(list)
    samples = []
    for row in rows:
        try:
            metrics = json.loads(row["metrics_json"] or "{}")
        except json.JSONDecodeError:
            metrics = {}
        value = metrics.get("latest_read_count")
        if value is None:
            value = metrics.get("latest_play_count")
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        platform = row["platform"] or "未知平台"
        by_platform[platform].append(value)
        samples.append((value, row["title"], platform))
    if not rows:
        return "暂无历史发布观察。"
    if not samples:
        return f"全量发布观察 {len(rows)} 条，但没有可比较的阅读/播放指标。"

    lines = [f"全量发布观察 {len(rows)} 条，其中 {len(samples)} 条有可比较的阅读/播放指标："]
    for platform, values in sorted(by_platform.items()):
        lines.append(
            f"- {platform}：{len(values)} 条，平均 {sum(values) / len(values):.1f}，"
            f"中位数 {median(values):.1f}，峰值 {max(values):.1f}"
        )
    lines.append("全量高表现样本（仅作模式参考，不等于保证复现）：")
    for value, title, platform in sorted(samples, reverse=True)[:5]:
        lines.append(f"- {title}（{platform}，{value:.0f}）")
    return "\n".join(lines)


def _get_learning_methods(conn, limit=12) -> str:
    """把已复盘的方法候选提供给下一轮评分，避免复盘停留在孤立记录。"""
    rows = conn.execute(
        "SELECT title, method_text, target_skill_name, status FROM method_candidates "
        "WHERE status IN ('candidate', 'promoted') ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    if not rows:
        return "暂无已沉淀方法；请根据选题本身和历史表现判断。"
    return "\n".join(
        f"- {row['title']}（{row['status']}，目标 Skill：{row['target_skill_name'] or '未指定'}）：{row['method_text'][:300]}"
        for row in rows
    )


def _build_prompt(conn, topic) -> str:
    """构建评分prompt"""
    return SCORING_PROMPT_TEMPLATE.format(
        title=topic["title"],
        summary=topic["summary"] or "无",
        raw_content=(topic["raw_content"] or "无")[:2000],
        source_type=topic["source_type"],
        expertise_profile=_get_expertise_profile_text(conn),
        recent_coverage_keywords=_get_recent_coverage_keywords(conn),
        performance_references=_get_performance_references(conn),
        performance_summary=_get_performance_summary(conn),
        learning_methods=_get_learning_methods(conn),
    )


def _call_llm(prompt: str) -> dict:
    """调用LLM API"""
    import httpx

    api_base = get_setting("llm_api_base", "https://api.openai.com/v1")
    api_key = get_setting("llm_api_key", "")
    model = get_setting("llm_model", "gpt-4o-mini")

    if not api_key:
        raise ValueError("LLM API Key 未配置，请在设置页面配置")

    url = f"{api_base.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是一位专业的AI内容选题评估专家。请严格按照要求输出JSON。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
    }

    with httpx.Client(timeout=60) as client:
        resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    content = data["choices"][0]["message"]["content"]
    # 清理可能的markdown代码块标记
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1] if "\n" in content else content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

    return json.loads(content)


def _save_score(conn, topic_id: int, result: dict, model: str):
    """保存评分到数据库"""
    traffic = float(result.get("traffic_score", 0))
    opinion = float(result.get("opinion_score", 0))
    novelty = float(result.get("novelty_score", 0))

    tw = get_float_setting("traffic_weight", 0.4)
    ow = get_float_setting("opinion_weight", 0.4)
    nw = get_float_setting("novelty_weight", 0.2)
    total = traffic * tw + opinion * ow + novelty * nw

    reasoning = {
        "traffic": result.get("traffic_reasoning", ""),
        "opinion": result.get("opinion_reasoning", ""),
        "novelty": result.get("novelty_reasoning", ""),
        "format": result.get("format_reasoning", ""),
    }

    cur = conn.execute(
        "INSERT INTO topic_scores "
        "(topic_id, traffic_score, opinion_score, novelty_score, total_score, "
        "traffic_reasoning, opinion_reasoning, novelty_reasoning, format_reasoning, "
        "recommended_format, key_angle, model_used, prompt_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (topic_id, traffic, opinion, novelty, total,
         reasoning["traffic"], reasoning["opinion"], reasoning["novelty"],
         reasoning["format"], result.get("recommended_format", "undecided"),
         result.get("key_angle", ""), model, PROMPT_VERSION)
    )
    score_id = cur.lastrowid

    # 更新推荐形式。注意：评分不改选题状态——状态只表达"素材用没用"，
    # 是否已评分由 topic_scores 有没有记录表达，避免两处记同一件事。
    conn.execute(
        "UPDATE topics SET recommended_format = ?, updated_at = ? WHERE id = ?",
        (result.get("recommended_format", "undecided"),
         datetime.now().isoformat(), topic_id)
    )
    # 记录事件
    conn.execute(
        "INSERT INTO topic_events (topic_id, event_type, metadata) "
        "VALUES (?, 'scored', ?)",
        (topic_id, json.dumps({
            "score_id": score_id,
            "total_score": round(total, 2),
            "traffic": traffic,
            "opinion": opinion,
            "novelty": novelty,
        }, ensure_ascii=False))
    )
    conn.commit()
    return score_id


def score_topic_sync(topic_id: int) -> dict:
    """同步评分（阻塞）"""
    conn = get_db()
    try:
        topic = conn.execute(
            "SELECT * FROM topics WHERE id = ?", (topic_id,)
        ).fetchone()
        if not topic:
            return None

        # 记录评分开始事件（不动选题状态：状态只表示素材用没用）
        conn.execute(
            "INSERT INTO topic_events (topic_id, event_type, metadata) "
            "VALUES (?, 'scoring_started', ?)",
            (topic_id, json.dumps({}))
        )
        conn.commit()

        # 构建prompt并调用LLM
        prompt = _build_prompt(conn, topic)
        model = get_setting("llm_model", "gpt-4o-mini")
        result = _call_llm(prompt)

        # 保存评分
        score_id = _save_score(conn, topic_id, result, model)
        return {"score_id": score_id, "result": result}

    except Exception as e:
        # 评分失败只记录事件，不回退选题状态（评分不再是状态的一部分）
        conn.execute(
            "INSERT INTO topic_events (topic_id, event_type, metadata) "
            "VALUES (?, 'scoring_failed', ?)",
            (topic_id, json.dumps({"error": str(e)}, ensure_ascii=False))
        )
        conn.commit()
        raise
    finally:
        conn.close()


def score_topic_async(topic_id: int):
    """异步评分（非阻塞，在后台线程执行）。
    没有配置LLM API Key时静默跳过——选题保持new状态，
    等待Agent通过 POST /api/topics/{id}/score 直接提交评分。
    """
    api_key = get_setting("llm_api_key", "")
    if not api_key:
        # 没有API Key，选题保持new状态，等Agent通过API评分
        return

    import threading
    def _run():
        try:
            score_topic_sync(topic_id)
        except Exception:
            pass  # 错误已在score_topic_sync中记录

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()


def auto_score_pending_topics(limit: int = 3) -> dict:
    """后台补齐待评分选题；没有 LLM 凭证时明确跳过，不改变选题状态。

    "待评分" = topic_scores 里还没有记录的选题（事实判定，不看 status）。
    """
    if not get_setting("llm_api_key", "").strip():
        return {"selected": 0, "scored": 0, "failed": 0, "skipped": "llm_not_configured"}

    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT t.id FROM topics t LEFT JOIN topic_scores s ON s.topic_id = t.id "
            "WHERE s.id IS NULL ORDER BY t.news_date DESC, t.id DESC LIMIT ?",
            (min(max(limit, 1), 10),),
        ).fetchall()
    finally:
        conn.close()

    scored = 0
    failed = 0
    for row in rows:
        try:
            score_topic_sync(row["id"])
            scored += 1
        except Exception:
            failed += 1
    return {"selected": len(rows), "scored": scored, "failed": failed}
