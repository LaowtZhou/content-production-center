"""类别 / 地域 / 主体 回填脚本（可复用）。

按 app/verbs.py 的动词优先分类重判 topics.category，并同时重算 app/geo.py 的
region / entity。默认只试算（dry-run），不改动数据库。

用法：
    python scripts/backfill_categories.py            # 只对"分类为空"的选题试算
    python scripts/backfill_categories.py --all      # 对全部选题试算
    python scripts/backfill_categories.py --apply    # 试算并写库
    python scripts/backfill_categories.py --all --apply

说明（2026-10-03 重建）：原 scripts/backfill_categories.py 被误删，按
docs/变更记录-2026-10-02-类别与事件归并.md 描述的能力（dry-run / --apply / --all）重写。
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.init_db import get_db  # noqa: E402
from app.geo import judge  # noqa: E402
from app.verbs import classify  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="类别/地域/主体回填")
    parser.add_argument("--all", action="store_true", help="处理全部选题，而不只是分类为空的")
    parser.add_argument("--apply", action="store_true", help="写回数据库（默认只试算）")
    args = parser.parse_args()

    conn = get_db()
    where = "" if args.all else " WHERE COALESCE(category,'') = ''"
    rows = conn.execute(
        "SELECT id, title, COALESCE(summary,'') AS summary, "
        "COALESCE(raw_content,'') AS raw_content, "
        "COALESCE(category,'') AS category FROM topics" + where
    ).fetchall()

    changes: Counter = Counter()
    for row in rows:
        region, entity, _kind = judge(row["title"], row["raw_content"])
        new_cat = classify(row["title"], row["summary"])
        if new_cat != row["category"]:
            changes[f"{row['category'] or '(空)'} -> {new_cat}"] += 1
        if args.apply:
            conn.execute(
                "UPDATE topics SET category = ?, region = ?, entity = ? WHERE id = ?",
                (new_cat, region, entity, row["id"]),
            )
    if args.apply:
        conn.commit()
    conn.close()

    print(f"扫描 {len(rows)} 条，分类变化 {sum(changes.values())} 条。")
    for pair, count in changes.most_common():
        print(f"  {pair}: {count}")
    print("已写回数据库。" if args.apply else "试算结束（未改动数据库，加 --apply 才写库）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
