"""选题解析器 - 将OpenClaw输出文件解析为独立选题

OpenClaw格式：每个MD文件 = 一条新闻
- H1标题
- blockquote元数据（来源/作者/日期/来源等级）
- 正文内容
- 可选原文链接
- 文件名格式：YYYY-MM-DD-NN-标题.md
"""

import json
import os
import hashlib
import re
import threading
import uuid
from datetime import datetime

from app.config import get_db
from app.statuses import DEFAULT_USAGE

# 全局文件处理锁：防止同一文件被多个线程/事件并发处理
_file_locks: dict[str, threading.Lock] = {}
_file_locks_guard = threading.Lock()


def _get_file_lock(filepath: str) -> threading.Lock:
    """获取指定文件路径的锁（每个路径一把锁）"""
    with _file_locks_guard:
        if filepath not in _file_locks:
            _file_locks[filepath] = threading.Lock()
        return _file_locks[filepath]


def _file_hash(filepath: str) -> str:
    """计算文件哈希"""
    with open(filepath, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def _is_processed(conn, file_hash: str) -> bool:
    """检查文件是否已处理过"""
    row = conn.execute(
        "SELECT id FROM topic_sources WHERE file_hash = ? AND processed = 1",
        (file_hash,)
    ).fetchone()
    return row is not None


def _record_source_issue(conn, source_id: int, issue_type: str, detail: str) -> None:
    """让解析失败可查询、可重试，不再只留在控制台日志里。"""
    existing = conn.execute(
        "SELECT id FROM source_issues WHERE source_id = ? AND issue_type = ? AND status = 'open' LIMIT 1",
        (source_id, issue_type),
    ).fetchone()
    if existing:
        return
    conn.execute(
        """INSERT INTO source_issues
        (id, source_id, issue_type, severity, status, detail, created_at)
        VALUES (?, ?, ?, 'warning', 'open', ?, ?)""",
        (str(uuid.uuid4()), source_id, issue_type, detail, datetime.now().isoformat()),
    )


def _resolve_source_issues(conn, source_id: int, note: str) -> None:
    """来源成功解析或确认重复后，关闭此前同一来源的开放异常。"""
    conn.execute(
        """UPDATE source_issues
        SET status = 'resolved', resolved_at = ?, resolution_note = ?
        WHERE source_id = ? AND status = 'open'""",
        (datetime.now().isoformat(), note, source_id),
    )


def _resolve_source_issues_for_path(conn, source_ref: str, note: str) -> None:
    """文件内容更新导致哈希变化时，按稳定路径关闭旧来源记录上的异常。"""
    conn.execute(
        """UPDATE source_issues
        SET status = 'resolved', resolved_at = ?, resolution_note = ?
        WHERE status = 'open' AND source_id IN
            (SELECT id FROM topic_sources WHERE source_ref = ?)""",
        (datetime.now().isoformat(), note, source_ref),
    )


def _is_duplicate_topic(conn, title: str, news_date: str | None) -> bool:
    """检查相同标题+新闻日期的选题是否已存在（防止并发重复创建）"""
    if not title:
        return False
    if news_date:
        row = conn.execute(
            "SELECT id FROM topics WHERE title = ? AND news_date = ? LIMIT 1",
            (title, news_date)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM topics WHERE title = ? AND news_date IS NULL LIMIT 1",
            (title,)
        ).fetchone()
    return row is not None


def _parse_filename(filepath: str) -> dict:
    """从文件名提取日期和序号

    支持两种格式：
    - YYYY-MM-DD-NN-标题.md（普通批次）
    - YYYY-MM-DD-bN-NN-标题.md（追加批次，如 b1-01、b3-04）
    返回：{"news_date": "2026-08-07", "seq": 1}
    """
    filename = os.path.basename(filepath)
    # 匹配 YYYY-MM-DD-NN-标题.md 或 YYYY-MM-DD-bN-NN-标题.md
    m = re.match(r'(\d{4}-\d{2}-\d{2})-(?:b\d+-)?(\d+)-', filename)
    if m:
        return {"news_date": m.group(1), "seq": int(m.group(2))}
    # 兜底：早期文件名为 NN-标题.md，日期在父目录名里（ai-news/YYYY-MM-DD/）
    parent = os.path.basename(os.path.dirname(filepath))
    m = re.match(r'(\d{4}-\d{2}-\d{2})$', parent)
    if m:
        seq = re.match(r'(\d+)-', filename)
        return {"news_date": m.group(1), "seq": int(seq.group(1)) if seq else None}
    return {"news_date": None, "seq": None}


def _parse_metadata(lines: list) -> dict:
    """解析blockquote元数据行
    
    支持两种格式：
    1. > 来源：xxx | 作者：xxx | 报道日期：xxx
    2. > 来源：xxx（独立行）
    """
    meta = {
        "source_name": "",
        "source_author": "",
        "source_level": "",
        "published_date": "",
        "report_date": "",
        "original_url": "",
    }
    
    for line in lines:
        line = line.strip()
        if not line.startswith(">"):
            continue
        # 去掉 > 前缀
        content = line.lstrip(">").strip()
        if not content:
            continue
        
        # 原文链接
        if "原文链接" in content or content.startswith("📌"):
            url_match = re.search(r'https?://\S+', content)
            if url_match:
                meta["original_url"] = url_match.group(0)
            continue
        
        # 竖线分隔的格式：来源：xxx | 作者：xxx | 报道日期：xxx
        if "|" in content:
            parts = content.split("|")
            for part in parts:
                part = part.strip()
                if part.startswith("来源：") or part.startswith("来源:"):
                    meta["source_name"] = part.split("：", 1)[-1].split(":", 1)[-1].strip()
                elif part.startswith("作者：") or part.startswith("作者:"):
                    meta["source_author"] = part.split("：", 1)[-1].split(":", 1)[-1].strip()
                elif part.startswith("报道日期：") or part.startswith("报道日期:"):
                    meta["report_date"] = part.split("：", 1)[-1].split(":", 1)[-1].strip()
                elif part.startswith("发布时间：") or part.startswith("发布时间:"):
                    meta["published_date"] = part.split("：", 1)[-1].split(":", 1)[-1].strip()
                elif part.startswith("来源等级：") or part.startswith("来源等级:"):
                    meta["source_level"] = part.split("：", 1)[-1].split(":", 1)[-1].strip()
        else:
            # 独立行格式
            if content.startswith("来源：") or content.startswith("来源:"):
                meta["source_name"] = content.split("：", 1)[-1].split(":", 1)[-1].strip()
            elif content.startswith("作者：") or content.startswith("作者:"):
                meta["source_author"] = content.split("：", 1)[-1].split(":", 1)[-1].strip()
            elif content.startswith("报道日期：") or content.startswith("报道日期:"):
                meta["report_date"] = content.split("：", 1)[-1].split(":", 1)[-1].strip()
            elif content.startswith("发布时间：") or content.startswith("发布时间:"):
                meta["published_date"] = content.split("：", 1)[-1].split(":", 1)[-1].strip()
            elif content.startswith("来源等级：") or content.startswith("来源等级:"):
                meta["source_level"] = content.split("：", 1)[-1].split(":", 1)[-1].strip()
    
    return meta


def parse_openclaw_md(content: str) -> dict:
    """解析OpenClaw格式的单个MD文件
    
    返回：{
        "title": "标题",
        "summary": "摘要（前200字）",
        "raw_content": "完整正文（不含元数据）",
        "source_name": "来源名称",
        "source_level": "来源等级",
        "source_author": "作者",
        "published_date": "发布时间",
        "original_url": "原文链接",
    }
    解析失败返回 None
    """
    # Windows/编辑器常写入 UTF-8 BOM；它不能改变 Markdown 的 H1 语义。
    lines = content.lstrip("\ufeff").split("\n")
    
    # 提取H1标题
    title = None
    title_line_idx = None
    for i, line in enumerate(lines):
        if line.strip().startswith("# "):
            title = line.strip()[2:].strip()
            title_line_idx = i
            break
    
    if not title:
        return None
    
    # 提取blockquote元数据行（标题之后的连续blockquote）
    meta_lines = []
    body_start_idx = title_line_idx + 1
    for i in range(title_line_idx + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith(">"):
            meta_lines.append(lines[i])
        elif stripped == "" or stripped == "---":
            # 空行或分隔线，跳过
            continue
        else:
            # 遇到非blockquote非空行，正文开始
            body_start_idx = i
            break
    
    meta = _parse_metadata(meta_lines)
    
    # 提取正文（从body_start_idx到文件末尾，去掉末尾的原文链接blockquote）
    body_lines = lines[body_start_idx:]
    # 去掉末尾的原文链接和空行，同时提取URL
    cleaned_body = []
    for line in body_lines:
        stripped = line.strip()
        if stripped.startswith(">") and ("原文链接" in stripped or "📌" in stripped):
            # 提取URL
            if not meta["original_url"]:
                url_match = re.search(r'https?://\S+', stripped)
                if url_match:
                    meta["original_url"] = url_match.group(0)
            continue
        cleaned_body.append(line)
    
    raw_content = "\n".join(cleaned_body).strip()
    # 去掉首尾的 --- 分隔线
    raw_content = re.sub(r'^---\s*\n', '', raw_content)
    raw_content = re.sub(r'\n---\s*$', '', raw_content)
    raw_content = raw_content.strip()
    
    # 生成摘要：取正文前200字，去掉markdown标记
    summary_text = re.sub(r'[*#>`\-]', '', raw_content)
    summary_text = re.sub(r'\n+', ' ', summary_text).strip()
    summary = summary_text[:200] if summary_text else ""
    
    return {
        "title": title,
        "summary": summary,
        "raw_content": raw_content,
        "source_name": meta["source_name"],
        "source_level": meta["source_level"],
        "source_author": meta["source_author"],
        "published_date": meta["published_date"],
        "original_url": meta["original_url"],
    }


def process_file(filepath: str) -> int:
    """
    处理一个OpenClaw输出文件
    返回创建的选题ID，失败返回0

    防重复机制（三重）：
    1. 文件级锁 — 同一路径不会并发处理
    2. 文件哈希去重 — 相同内容的文件不会重复入库
    3. 标题+日期去重 — 相同标题+新闻日期的选题不会重复创建
    """
    if not os.path.exists(filepath):
        return 0

    # 跳过HTML日报文件
    if filepath.endswith(".html") or filepath.endswith(".htm"):
        return 0

    # 获取文件级锁，防止 watchdog 多次事件并发处理同一文件
    lock = _get_file_lock(filepath)
    if not lock.acquire(blocking=False):
        # 另一个线程正在处理这个文件，跳过
        print(f"[选题解析] 文件正在处理中，跳过: {os.path.basename(filepath)}")
        return 0

    try:
        fhash = _file_hash(filepath)
        filename = os.path.basename(filepath)
        delivered_at = datetime.now().isoformat()

        conn = get_db()

        # 去重检查1：文件哈希
        if _is_processed(conn, fhash):
            _resolve_source_issues_for_path(conn, filepath, "文件已处理成功，关闭历史开放异常")
            conn.commit()
            conn.close()
            return 0

        existing_source = conn.execute(
            "SELECT * FROM topic_sources WHERE file_hash = ? ORDER BY processed DESC, id ASC LIMIT 1",
            (fhash,),
        ).fetchone()
        if existing_source and existing_source["processed"]:
            conn.close()
            return 0
        if existing_source:
            source_id = existing_source["id"]
            conn.execute(
                "UPDATE topic_sources SET source_ref = ?, delivered_at = ?, source_format = ? WHERE id = ?",
                (filepath, delivered_at, os.path.splitext(filename)[1].lower().lstrip('.'), source_id),
            )
        else:
            source_cur = conn.execute(
                """INSERT INTO topic_sources
                (source_type, source_ref, delivered_at, file_hash, processed, topic_count, source_format)
                VALUES ('openclaw', ?, ?, ?, 0, 0, ?)""",
                (filepath, delivered_at, fhash, os.path.splitext(filename)[1].lower().lstrip('.')),
            )
            source_id = source_cur.lastrowid

        # 读取文件内容
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            print(f"[选题解析] 读取文件失败 {filename}: {e}")
            _record_source_issue(conn, source_id, "source_read_failed", f"读取文件失败: {e}")
            conn.commit()
            conn.close()
            return 0

        # 解析MD文件
        if filepath.endswith(".md"):
            parsed = parse_openclaw_md(content)
            if not parsed:
                print(f"[选题解析] 无法解析文件: {filename}")
                _record_source_issue(conn, source_id, "source_parse_failed", "Markdown 缺少可识别的 H1 标题或元数据格式")
                conn.commit()
                conn.close()
                return 0
        elif filepath.endswith(".json"):
            _record_source_issue(conn, source_id, "source_unsupported_format", "JSON 文件暂不由 Markdown 选题解析器处理")
            conn.commit()
            conn.close()
            return 0
        else:
            _record_source_issue(conn, source_id, "source_unsupported_format", f"不支持的来源格式: {filename}")
            conn.commit()
            conn.close()
            return 0

        # 从文件名提取日期
        fname_info = _parse_filename(filepath)

        # 去重检查2：标题+新闻日期
        if _is_duplicate_topic(conn, parsed["title"], fname_info["news_date"]):
            print(f"[选题解析] 选题已存在，跳过: {parsed['title'][:40]}")
            conn.execute(
                "UPDATE topic_sources SET processed = 1, processed_at = ?, topic_count = 0 WHERE id = ?",
                (datetime.now().isoformat(), source_id),
            )
            _resolve_source_issues(conn, source_id, "文件已成功解析，但标题与新闻日期对应的选题已存在")
            _resolve_source_issues_for_path(conn, filepath, "文件已成功解析，但标题与新闻日期对应的选题已存在")
            conn.commit()
            conn.close()
            return 0

        # 解析成功后将预登记的来源标记为已处理
        now_iso = datetime.now().isoformat()
        conn.execute(
            "UPDATE topic_sources SET processed = 1, processed_at = ?, topic_count = 1 WHERE id = ?",
            (now_iso, source_id),
        )
        _resolve_source_issues(conn, source_id, "文件已成功解析并进入选题池")
        _resolve_source_issues_for_path(conn, filepath, "文件已成功解析并进入选题池")

        # 创建选题
        now = datetime.now().isoformat()
        # 分类必须走 app.verbs.classify（唯一判定实现，含来源类判定）——
        # 此前这里 import 的是 app.taxonomy.classify（名词弱信号），与启动重判
        # 使用的规则不是同一套，导致新素材入库分类与重判结果长期不一致。
        from app.verbs import classify
        from app.geo import resolve_geo
        # source_ref（filepath）一并传入：来自「XX昨日工作挖掘」目录的素材
        # 按来源归入「周老师AI日记」，不参与内容判定。
        category = classify(parsed["title"] or "", parsed["summary"] or "", filepath)
        # 地域/主体必须与分类同一次落定，并且**必须把分类和来源一起传进去**：
        # 日记类的地域固定中国、主体取 Agent 名，不看正文。漏写这两列 =
        # 新素材进不了归类树，所以入库时就算，不留给事后迁移。
        region, entity, _kind = resolve_geo(
            parsed["title"] or "",
            parsed["raw_content"] or parsed["summary"] or "",
            category, filepath,
        )
        cur = conn.execute(
            "INSERT INTO topics (title, summary, raw_content, source_type, source_ref, "
            "source_id, source_delivered_at, status, tags, created_at, updated_at, "
            "source_name, source_level, source_author, published_date, news_date, original_url, category, "
            "region, entity) "
            "VALUES (?, ?, ?, 'openclaw', ?, ?, ?, ?, '[]', ?, ?, "
            "?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                parsed["title"],
                parsed["summary"],
                parsed["raw_content"],
                filepath,
                source_id,
                delivered_at,
                DEFAULT_USAGE,
                now, now,
                parsed["source_name"],
                parsed["source_level"],
                parsed["source_author"],
                parsed["published_date"],
                fname_info["news_date"],
                parsed["original_url"],
                category,
                region,
                entity,
            )
        )
        topic_id = cur.lastrowid

        # 记录事件
        conn.execute(
            "INSERT INTO topic_events (topic_id, event_type, metadata) "
            "VALUES (?, 'discovered', ?)",
            (topic_id, json.dumps({
                "source": "openclaw",
                "source_file": filename,
                "source_id": source_id,
                "news_date": fname_info["news_date"],
                "source_name": parsed["source_name"],
            }, ensure_ascii=False))
        )

        # 记录Agent活动
        conn.execute(
            "INSERT INTO agent_activities (agent_name, activity_type, target_type, target_id, summary) "
            "VALUES ('OpenClaw', 'collected', 'topic', ?, ?)",
            (topic_id, f"采集: {parsed['title'][:50]}")
        )

        conn.commit()
        conn.close()

        # 异步触发评分（如果有API Key）
        try:
            from app.services.scoring_engine import score_topic_async
            score_topic_async(topic_id)
        except Exception:
            pass  # 没有API Key时静默跳过

        return topic_id
    finally:
        lock.release()


def scan_directory(directory: str) -> dict:
    """
    递归扫描目录中的所有未处理MD文件
    跳过 _duplicates、daily-images 和 03-projects 目录

    `03-projects`（周老师 2026-10-03 加）——那是**项目工作目录**，不是新闻来源。
    此前它有两条文件（DseWiki 多 Agent 共享通道）被当成 AI 新闻抓进选题池，
    跟外部新闻混在一个树上。项目自己的过程文件永远不该进选题池，所以在这里挡掉。
    """
    if not os.path.isdir(directory):
        return {"error": f"目录不存在: {directory}"}

    total_files = 0
    total_created = 0
    total_skipped = 0
    skipped_total = 0
    errors = []
    skipped_dirs = {"_duplicates", "daily-images", "03-projects"}

    for root, dirs, files in os.walk(directory):
        # 跳过指定目录
        before = len(dirs)
        dirs[:] = [d for d in dirs if d not in skipped_dirs and not d.startswith("daily-images-")]
        skipped_total += before - len(dirs)

        for filename in sorted(files):
            if not filename.endswith(".md"):
                continue
            filepath = os.path.join(root, filename)
            total_files += 1
            try:
                topic_id = process_file(filepath)
                if topic_id:
                    total_created += 1
                else:
                    total_skipped += 1
            except Exception as e:
                errors.append(f"{filename}: {str(e)}")
                total_skipped += 1

    return {
        "total_files": total_files,
        "topics_created": total_created,
        "topics_skipped": total_skipped,
        "skipped_dirs": skipped_total,
        "errors": errors,
    }
