"""把一批"热点"选题灌入 topics 表（一次性导入工具，可复用）。

输入文件两种格式：
- .json：[{"title": "...", "summary": "...", "news_date": "YYYY-MM-DD",
           "source_name": "...", "original_url": "..."}, ...]
- .txt ：每行一条标题（其余字段留空）

默认只试算（dry-run），加 --apply 才写库。已存在的同名标题会跳过，保证重复执行幂等。

用法：
    python scripts/add_trending_topics.py <文件>            # 试算
    python scripts/add_trending_topics.py <文件> --apply    # 写库

说明（2026-10-03 重建）：原 scripts/add_trending_topics.py 被误删。原件是 2026-08-10
一次性灌数据的脚本（见 docs/无日期选题溯源-2026-10-02.md），其确切输入格式已无法还原，
这里按其行为（写入 source_type='manual' 的选题）重写为通用版本。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.init_db import get_db  # noqa: E402
from app.verbs import classify  # noqa: E402
from app.geo import judge  # noqa: E402


def _load(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("topics", [])
        return [row for row in data if isinstance(row, dict) and row.get("title")]
    return [{"title": line.strip()} for line in text.splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="导入热点选题")
    parser.add_argument("source_file")
    parser.add_argument("--apply", action="store_true", help="写回数据库（默认只试算）")
    args = parser.parse_args()

    rows = _load(Path(args.source_file))
    conn = get_db()
    now = datetime.now().isoformat()

    inserted = skipped = 0
    for row in rows:
        title = (row.get("title") or "").strip()
        exists = conn.execute(
            "SELECT 1 FROM topics WHERE title = ? LIMIT 1", (title,)
        ).fetchone()
        if exists:
            skipped += 1
            continue
        summary = row.get("summary") or ""
        region, entity, _kind = judge(title, summary)
        category = classify(title, summary)
        if args.apply:
            conn.execute(
                "INSERT INTO topics (title, summary, raw_content, source_type, status, "
                "news_date, source_name, original_url, category, region, entity, "
                "created_at, updated_at) "
                "VALUES (?, ?, '', 'manual', 'unused', ?, ?, ?, ?, ?, ?, ?, ?)",
                (title, summary, row.get("news_date"), row.get("source_name"),
                 row.get("original_url"), category, region, entity, now, now),
            )
        inserted += 1

    if args.apply:
        conn.commit()
    conn.close()

    print(f"读取 {len(rows)} 条：待导入 {inserted} 条，已存在跳过 {skipped} 条。")
    print("已写回数据库。" if args.apply else "试算结束（未改动数据库，加 --apply 才写库）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
