"""数据库初始化脚本 - 创建所有表和默认数据

说明（2026-10-03 重建）：本文件按 data/revert-backup-20261002/init_db.py 备份 +
2026-10-03 的三段迁移（类别/事件列、素材使用状态收敛、三级树 region/entity）还原。
"""

import sqlite3
import os
import json
import hashlib
from datetime import datetime
from app.runtime import APP_BASE_URL

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "production_center.db")


def get_db():
    """获取数据库连接"""
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_database():
    """创建所有表"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    cur = conn.cursor()

    # 选题表
    cur.execute("""
    CREATE TABLE IF NOT EXISTS topics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        summary TEXT,
        raw_content TEXT,
        source_type TEXT NOT NULL DEFAULT 'manual',
        source_ref TEXT,
        source_id INTEGER,
        source_delivered_at TIMESTAMP,
        status TEXT NOT NULL DEFAULT 'new',
        tags TEXT DEFAULT '[]',
        recommended_format TEXT DEFAULT 'undecided',
        coverage_status TEXT DEFAULT 'not_checked',
        coverage_note TEXT,
        selected_at TIMESTAMP,
        selected_note TEXT,
        abandoned_reason TEXT,
        abandoned_at TIMESTAMP,
        source_name TEXT,
        source_level TEXT,
        source_author TEXT,
        published_date TEXT,
        news_date TEXT,
        original_url TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (source_id) REFERENCES topic_sources(id)
    )
    """)

    # 评分记录表（不覆盖历史）
    cur.execute("""
    CREATE TABLE IF NOT EXISTS topic_scores (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic_id INTEGER NOT NULL,
        traffic_score REAL NOT NULL,
        opinion_score REAL NOT NULL,
        novelty_score REAL NOT NULL,
        total_score REAL NOT NULL,
        traffic_reasoning TEXT,
        opinion_reasoning TEXT,
        novelty_reasoning TEXT,
        format_reasoning TEXT,
        recommended_format TEXT,
        key_angle TEXT,
        model_used TEXT,
        prompt_version TEXT,
        scored_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (topic_id) REFERENCES topics(id)
    )
    """)

    # 选题事件流（审计）
    cur.execute("""
    CREATE TABLE IF NOT EXISTS topic_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic_id INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        event_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        metadata TEXT,
        FOREIGN KEY (topic_id) REFERENCES topics(id)
    )
    """)

    # 来源批次表
    cur.execute("""
    CREATE TABLE IF NOT EXISTS topic_sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_type TEXT NOT NULL,
        source_ref TEXT,
        delivered_at TIMESTAMP,
        file_hash TEXT,
        processed INTEGER DEFAULT 0,
        processed_at TIMESTAMP,
        topic_count INTEGER DEFAULT 0
    )
    """)

    # 覆盖记录表
    cur.execute("""
    CREATE TABLE IF NOT EXISTS coverage_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic_id INTEGER,
        title TEXT NOT NULL,
        platform TEXT,
        published_at TIMESTAMP,
        content_url TEXT,
        key_topics TEXT DEFAULT '[]',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (topic_id) REFERENCES topics(id)
    )
    """)

    # 专业画像表
    cur.execute("""
    CREATE TABLE IF NOT EXISTS expertise_profile (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        area TEXT NOT NULL,
        description TEXT,
        weight REAL DEFAULT 0.5,
        keywords TEXT DEFAULT '[]',
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # 创作任务文件记录表
    cur.execute("""
    CREATE TABLE IF NOT EXISTS task_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic_id INTEGER NOT NULL,
        file_path TEXT NOT NULL,
        template_used TEXT,
        generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (topic_id) REFERENCES topics(id)
    )
    """)

    # 生产任务：Topic 只管理选题决策；任务与运行记录独立管理生产过程。
    # 这样不会把“写完发布稿”和“平台确实发布”混成同一个状态。
    cur.execute("""
    CREATE TABLE IF NOT EXISTS production_tasks (
        id TEXT PRIMARY KEY,
        topic_id INTEGER NOT NULL,
        mode TEXT NOT NULL DEFAULT 'manual',
        status TEXT NOT NULL DEFAULT 'queued',
        requested_platform TEXT NOT NULL DEFAULT 'wechat',
        requested_deliverables TEXT NOT NULL DEFAULT '[]',
        executor_hint TEXT,
        created_by TEXT NOT NULL DEFAULT 'human',
        selected_note TEXT,
        contract_path TEXT,
        callback_token TEXT NOT NULL,
        skill_manifest_json TEXT NOT NULL DEFAULT '[]',
        vault_project_path TEXT,
        primary_article_path TEXT,
        artifact_manifest_json TEXT NOT NULL DEFAULT '[]',
        blocked_reason TEXT,
        claimed_at TEXT,
        started_at TEXT,
        completed_at TEXT,
        closed_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(topic_id) REFERENCES topics(id)
    )
    """)

    # 每次 Agent 接手、重试或回调都是一条运行事实，不覆盖上一轮。
    cur.execute("""
    CREATE TABLE IF NOT EXISTS task_runs (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        agent_name TEXT NOT NULL,
        status TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        summary TEXT,
        error_detail TEXT,
        used_skills_json TEXT NOT NULL DEFAULT '[]',
        self_check_json TEXT NOT NULL DEFAULT '{}',
        callback_payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(task_id) REFERENCES production_tasks(id)
    )
    """)

    # 多代理内容工作流：每个岗位阶段都是独立运行事实，不用一个 Agent 的自报替代岗位协作。
    cur.execute("""
    CREATE TABLE IF NOT EXISTS production_stage_runs (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        stage_key TEXT NOT NULL,
        role_name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        attempt INTEGER NOT NULL DEFAULT 1,
        output_paths_json TEXT NOT NULL DEFAULT '[]',
        result_json TEXT NOT NULL DEFAULT '{}',
        error_detail TEXT,
        started_at TEXT,
        completed_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(task_id) REFERENCES production_tasks(id),
        UNIQUE(task_id, stage_key, attempt)
    )
    """)

    # OPC-ERP 的发布和指标是外部事实。这里仅保存同步观察和可选关联，不反向宣称发布。
    cur.execute("""
    CREATE TABLE IF NOT EXISTS publication_observations (
        id TEXT PRIMARY KEY,
        opc_publication_id TEXT NOT NULL UNIQUE,
        opc_content_item_id TEXT,
        task_id TEXT,
        topic_id INTEGER,
        platform TEXT,
        title TEXT NOT NULL,
        external_url TEXT,
        published_at TEXT,
        captured_at TEXT,
        metrics_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'observed',
        matched_by TEXT,
        matched_at TEXT,
        review_flag_reason TEXT,
        source_payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(task_id) REFERENCES production_tasks(id),
        FOREIGN KEY(topic_id) REFERENCES topics(id)
    )
    """)

    # 内容决策反馈：只保存对 ERP 发布观察的派生判断，不复制 ERP 原始指标。
    # 一个发布观察在同一规则版本下每类信号最多一条，保证重复同步幂等。
    cur.execute("""
    CREATE TABLE IF NOT EXISTS feedback_signals (
        id TEXT PRIMARY KEY,
        observation_id TEXT NOT NULL,
        signal_type TEXT NOT NULL,
        priority TEXT NOT NULL DEFAULT 'medium',
        status TEXT NOT NULL DEFAULT 'open',
        headline TEXT NOT NULL,
        detail TEXT NOT NULL,
        suggested_action TEXT NOT NULL,
        evidence_json TEXT NOT NULL DEFAULT '{}',
        rule_version TEXT NOT NULL DEFAULT 'feedback-v1',
        handled_by TEXT,
        handling_note TEXT,
        handled_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(observation_id) REFERENCES publication_observations(id),
        UNIQUE(observation_id, signal_type, rule_version)
    )
    """)

    # 人与 AI 共创的复盘不直接改写 Skill；先沉淀为可审查的案例和方法候选。
    cur.execute("""
    CREATE TABLE IF NOT EXISTS review_cases (
        id TEXT PRIMARY KEY,
        observation_id TEXT,
        task_id TEXT,
        topic_id INTEGER,
        status TEXT NOT NULL DEFAULT 'candidate',
        trigger_type TEXT NOT NULL DEFAULT 'manual',
        trigger_reason TEXT,
        title_snapshot TEXT,
        performance_snapshot_json TEXT NOT NULL DEFAULT '{}',
        human_notes TEXT,
        ai_notes TEXT,
        conclusion TEXT,
        created_by TEXT NOT NULL DEFAULT 'system',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        closed_at TEXT,
        FOREIGN KEY(observation_id) REFERENCES publication_observations(id),
        FOREIGN KEY(task_id) REFERENCES production_tasks(id),
        FOREIGN KEY(topic_id) REFERENCES topics(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS method_candidates (
        id TEXT PRIMARY KEY,
        review_case_id TEXT,
        title TEXT NOT NULL,
        method_text TEXT NOT NULL,
        evidence_text TEXT,
        target_skill_name TEXT,
        status TEXT NOT NULL DEFAULT 'candidate',
        proposed_by TEXT NOT NULL DEFAULT 'human',
        promoted_at TEXT,
        promotion_note TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(review_case_id) REFERENCES review_cases(id)
    )
    """)

    # Skill 版本是任务执行时的“声明依据”。它记录快照哈希，不试图读取 AI 的思考过程。
    cur.execute("""
    CREATE TABLE IF NOT EXISTS skill_versions (
        id TEXT PRIMARY KEY,
        skill_name TEXT NOT NULL,
        skill_path TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        discovered_at TEXT NOT NULL,
        notes TEXT,
        UNIQUE(skill_path, content_hash)
    )
    """)

    # 来源文件仍留在 OpenClaw。中心只保存可验证指针及异常，避免复制并污染原始资料库。
    cur.execute("""
    CREATE TABLE IF NOT EXISTS source_issues (
        id TEXT PRIMARY KEY,
        source_id INTEGER NOT NULL,
        issue_type TEXT NOT NULL,
        severity TEXT NOT NULL DEFAULT 'warning',
        status TEXT NOT NULL DEFAULT 'open',
        detail TEXT,
        created_at TEXT NOT NULL,
        resolved_at TEXT,
        resolution_note TEXT,
        FOREIGN KEY(source_id) REFERENCES topic_sources(id)
    )
    """)

    # Codex 运营周期：保存采集调整和主编主动选题的决策结果，便于重试和审计。
    cur.execute("""
    CREATE TABLE IF NOT EXISTS operator_cycles (
        id TEXT PRIMARY KEY,
        status TEXT NOT NULL DEFAULT 'running',
        trigger_type TEXT NOT NULL DEFAULT 'scheduled',
        summary TEXT,
        basis_json TEXT NOT NULL DEFAULT '[]',
        plan_json TEXT NOT NULL DEFAULT '{}',
        result_json TEXT NOT NULL DEFAULT '{}',
        error_detail TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        completed_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS collector_directives (
        id TEXT PRIMARY KEY,
        cycle_id TEXT NOT NULL,
        job_name TEXT NOT NULL,
        instruction TEXT NOT NULL,
        instruction_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'proposed',
        reason TEXT,
        evidence_json TEXT NOT NULL DEFAULT '[]',
        before_hash TEXT,
        after_hash TEXT,
        backup_path TEXT,
        error_detail TEXT,
        created_at TEXT NOT NULL,
        applied_at TEXT,
        FOREIGN KEY(cycle_id) REFERENCES operator_cycles(id)
    )
    """)

    # 系统配置表
    cur.execute("""
    CREATE TABLE IF NOT EXISTS system_settings (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Agent活动记录表
    cur.execute("""
    CREATE TABLE IF NOT EXISTS agent_activities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_name TEXT NOT NULL,
        activity_type TEXT NOT NULL,
        target_type TEXT,
        target_id INTEGER,
        summary TEXT,
        metadata TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # 创建索引
    cur.execute("CREATE INDEX IF NOT EXISTS idx_topics_status ON topics(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_topics_source_type ON topics(source_type)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_topics_created ON topics(created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_topics_news_date ON topics(news_date)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_scores_topic ON topic_scores(topic_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_topic ON topic_events(topic_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sources_hash ON topic_sources(file_hash)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_activities_created ON agent_activities(created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_activities_agent ON agent_activities(agent_name)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_activities_target ON agent_activities(target_type, target_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tasks_topic ON production_tasks(topic_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON production_tasks(status, created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_runs_task ON task_runs(task_id, created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_stage_runs_task ON production_stage_runs(task_id, stage_key, attempt)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_stage_runs_status ON production_stage_runs(status, updated_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_observations_status ON publication_observations(status, published_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_feedback_status ON feedback_signals(status, priority, updated_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_feedback_observation ON feedback_signals(observation_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_feedback_type ON feedback_signals(signal_type, created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_reviews_status ON review_cases(status, created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_methods_status ON method_candidates(status, created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_operator_cycles_status ON operator_cycles(status, created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_collector_directives_cycle ON collector_directives(cycle_id, created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_collector_directives_job ON collector_directives(job_name, instruction_hash)")

    # 事件线表：把"同一事件多天多源"的选题串成一条可追踪的线索
    cur.execute("""
    CREATE TABLE IF NOT EXISTS event_threads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        first_seen TEXT,
        last_seen TEXT,
        topic_count INTEGER NOT NULL DEFAULT 0,
        keywords TEXT DEFAULT '[]',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # 事件线-选题关联：一条线对应多条选题，保持先后顺序
    cur.execute("""
    CREATE TABLE IF NOT EXISTS event_thread_topics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        thread_id INTEGER NOT NULL,
        topic_id INTEGER NOT NULL UNIQUE,
        matched_score REAL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (thread_id) REFERENCES event_threads(id),
        FOREIGN KEY (topic_id) REFERENCES topics(id)
    )
    """)

    # 弹药包表：选题确认后抓取原文全文并（配置 LLM 后自动）提取结构化素材
    cur.execute("""
    CREATE TABLE IF NOT EXISTS topic_ammo (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic_id INTEGER NOT NULL UNIQUE,
        source_url TEXT,
        full_text TEXT,
        fetch_status TEXT NOT NULL DEFAULT 'pending',
        fetch_error TEXT,
        facts_json TEXT,
        quotes_json TEXT,
        timeline_json TEXT,
        actors_json TEXT,
        numbers_json TEXT,
        extraction_status TEXT NOT NULL DEFAULT 'pending_key',
        extraction_model TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (topic_id) REFERENCES topics(id)
    )
    """)

    # 每日新闻处理任务的运行记录：日报与失败点名的事实源
    cur.execute("""
    CREATE TABLE IF NOT EXISTS daily_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_date TEXT NOT NULL,
        trigger_type TEXT NOT NULL DEFAULT 'scheduled',
        steps_json TEXT NOT NULL DEFAULT '{}',
        report_path TEXT,
        ok_count INTEGER NOT NULL DEFAULT 0,
        fail_count INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_threads_status ON event_threads(status, last_seen)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_thread_topics_thread ON event_thread_topics(thread_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ammo_topic ON topic_ammo(topic_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ammo_fetch ON topic_ammo(fetch_status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_daily_runs_date ON daily_runs(run_date)")

    # 无损迁移：topics 加可空 thread_id（已存在则跳过）
    cols = {r[1] for r in cur.execute("PRAGMA table_info(topics)").fetchall()}
    if "thread_id" not in cols:
        cur.execute("ALTER TABLE topics ADD COLUMN thread_id INTEGER REFERENCES event_threads(id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_topics_thread ON topics(thread_id)")

    # 插入默认配置
    defaults = {
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
    for key, value in defaults.items():
        cur.execute(
            "INSERT OR IGNORE INTO system_settings (key, value) VALUES (?, ?)",
            (key, value),
        )

    # 插入默认专业画像
    default_profiles = [
        ("大模型", "GPT、Claude、Gemini等大语言模型的发布、能力、对比", 0.9,
         '["GPT", "Claude", "Gemini", "Llama", "开源模型", "大模型", "LLM"]'),
        ("AI工具", "AI效率工具、自动化、Agent生态", 0.8,
         '["AI工具", "效率工具", "自动化", "Agent", "AI助手", "工作流"]'),
        ("内容创作方法论", "自媒体内容创作、B站运营、视频制作", 0.7,
         '["内容创作", "自媒体", "B站", "视频制作", "选题", "运营"]'),
    ]
    for area, desc, weight, keywords in default_profiles:
        cur.execute(
            "INSERT OR IGNORE INTO expertise_profile (area, description, weight, keywords) "
            "SELECT ?, ?, ?, ? "
            "WHERE NOT EXISTS (SELECT 1 FROM expertise_profile WHERE area = ?)",
            (area, desc, weight, keywords, area),
        )

    conn.commit()
    _migrate_legacy_data(conn)
    conn.close()
    print(f"数据库初始化完成: {DB_PATH}")


def _add_column_if_missing(conn, table: str, column: str, definition: str):
    """兼容已有数据库：只增加字段，绝不覆盖历史原始数据。"""
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migrate_legacy_data(conn):
    """将旧系统的“生产/发布”Topic 状态收敛为选题决策状态。"""
    # 选题类别（7 类固定清单，见 app/taxonomy.py）。空表示尚未归类。
    _add_column_if_missing(conn, "topics", "category", "TEXT DEFAULT ''")
    # 事件归并：同一条事件的多条选题共享同一个 event_key。空表示未归并。
    _add_column_if_missing(conn, "topics", "event_key", "TEXT DEFAULT ''")
    _add_column_if_missing(conn, "topic_sources", "source_format", "TEXT")
    _add_column_if_missing(conn, "topic_sources", "source_size_bytes", "INTEGER")
    _add_column_if_missing(conn, "topic_sources", "source_mtime", "TEXT")
    _add_column_if_missing(conn, "topic_sources", "last_verified_at", "TEXT")
    _add_column_if_missing(conn, "topic_sources", "availability_status", "TEXT DEFAULT 'unknown'")

    # 同一来源文件在服务重启重试时只保留一个开放异常，历史重复项保留但标记为已合并。
    duplicate_rows = conn.execute(
        """SELECT issue.id, source.file_hash, issue.issue_type
        FROM source_issues issue JOIN topic_sources source ON source.id = issue.source_id
        WHERE issue.status = 'open' ORDER BY issue.created_at ASC, issue.id ASC"""
    ).fetchall()
    seen_issues = set()
    for row in duplicate_rows:
        issue_key = (row["file_hash"], row["issue_type"])
        if issue_key in seen_issues:
            conn.execute(
                """UPDATE source_issues
                SET status = 'dismissed', resolved_at = ?, resolution_note = '同一来源的重复异常已合并'
                WHERE id = ?""",
                (datetime.now().isoformat(), row["id"]),
            )
        else:
            seen_issues.add(issue_key)

    # 旧版默认回传地址写成 8770；服务与脚本实际统一使用 8774。
    conn.execute(
        "UPDATE system_settings SET value = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE key = 'production_center_base_url' AND value = 'http://127.0.0.1:8770'",
        (APP_BASE_URL,),
    )

    # 未完成任务切换到主编负责制流水线；已完成任务不补写不存在的历史岗位事实。
    now = datetime.now().isoformat()
    stage_definitions = [
        ("chief_editor_plan", "主编 Agent"),
        ("writer", "作者 Agent"),
        ("editor", "编辑 Agent"),
        ("chief_editor_review", "主编 Agent"),
    ]
    active_tasks = conn.execute(
        "SELECT id FROM production_tasks WHERE status IN ('queued', 'claimed', 'running', 'blocked')"
    ).fetchall()
    for task in active_tasks:
        for stage_key, role_name in stage_definitions:
            conn.execute(
                """INSERT OR IGNORE INTO production_stage_runs
                (id, task_id, stage_key, role_name, status, attempt, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'queued', 1, ?, ?)""",
                (hashlib.sha256(f"{task['id']}:{stage_key}".encode()).hexdigest()[:36], task["id"], stage_key, role_name, now, now),
            )

    # 没有运行对象的旧 producing/published 不能被当作真实生产或真实发布；保守回退为已选题。
    rows = conn.execute("SELECT id, status FROM topics WHERE status IN ('producing', 'published')").fetchall()
    for row in rows:
        conn.execute("UPDATE topics SET status = 'selected', updated_at = ? WHERE id = ?", (now, row["id"]))
        conn.execute(
            "INSERT INTO topic_events (topic_id, event_type, metadata) VALUES (?, 'legacy_status_normalized', ?)",
            (row["id"], '{"from":"' + row["status"] + '","to":"selected"}')
        )

    # 2026-10-03：素材使用状态收敛（唯一事实来源见 app/statuses.py 的 LEGACY_STATUS_MAP）。
    # 平台只回答"这条素材我用没用"：unused / used / dropped。逐条写审计事件，可回溯；
    # 已收敛的行不再匹配旧状态，因此重复启动幂等。
    from app.statuses import LEGACY_STATUS_MAP, DEFAULT_USAGE

    migrated = 0
    for old_status, new_status in LEGACY_STATUS_MAP.items():
        rows = conn.execute("SELECT id FROM topics WHERE status = ?", (old_status,)).fetchall()
        for row in rows:
            conn.execute("UPDATE topics SET status = ?, updated_at = ? WHERE id = ?", (new_status, now, row["id"]))
            conn.execute(
                "INSERT INTO topic_events (topic_id, event_type, metadata) VALUES (?, 'usage_status_migrated', ?)",
                (row["id"], json.dumps({"from": old_status, "to": new_status}, ensure_ascii=False)),
            )
            migrated += 1
    if migrated:
        print(f"素材使用状态迁移完成：{migrated} 条选题从旧流水线状态收敛为 {DEFAULT_USAGE}/used/dropped")

    # 2026-10-03：三级树落地（分类 > 中国/美国/其他 > 政府或公司）。
    # 分类主导权改到动词/动名词（app/verbs.py）：失控=安全，不管是大模型失控还是智能体失控。
    # 地域与主体见 app/geo.py。事件线整线统一到一个分类，实现"默认不跨分类"。
    # 用 system_settings 打标记：只跑一次，避免每次启动重判覆盖日后的人工修正。
    _add_column_if_missing(conn, "topics", "region", "TEXT DEFAULT ''")
    _add_column_if_missing(conn, "topics", "entity", "TEXT DEFAULT ''")

    marked = conn.execute(
        "SELECT value FROM system_settings WHERE key = 'tree_v1_migrated'"
    ).fetchone()
    if not marked:
        from app.geo import judge as _judge
        from app.verbs import classify as _vclassify

        rows = conn.execute(
            "SELECT id, title, COALESCE(summary,'') AS summary, category, "
            "COALESCE(event_key,'') AS ek FROM topics"
        ).fetchall()

        line_titles: dict[str, list] = {}
        line_ids: dict[str, list] = {}
        changed = 0
        for row in rows:
            region, entity, _kind = _judge(row["title"], row["summary"])
            new_cat = _vclassify(row["title"], row["summary"])
            conn.execute(
                "UPDATE topics SET region = ?, entity = ?, category = ?, updated_at = ? WHERE id = ?",
                (region, entity, new_cat, now, row["id"]),
            )
            if (row["category"] or "") != new_cat:
                changed += 1
                conn.execute(
                    "INSERT INTO topic_events (topic_id, event_type, metadata) "
                    "VALUES (?, 'category_reclassified', ?)",
                    (row["id"], json.dumps({"from": row["category"] or "", "to": new_cat},
                                           ensure_ascii=False)),
                )
            if row["ek"]:
                line_titles.setdefault(row["ek"], []).append(row["title"] or "")
                line_ids.setdefault(row["ek"], []).append(row["id"])

        # 事件线整线统一：把整线标题拼起来按动词再判一次，全成员随线分类（默认不跨分类）。
        unified = 0
        for ek, titles in line_titles.items():
            line_cat = _vclassify(" ".join(titles))
            for tid in line_ids[ek]:
                conn.execute("UPDATE topics SET category = ? WHERE id = ?", (line_cat, tid))
                conn.execute(
                    "INSERT INTO topic_events (topic_id, event_type, metadata) "
                    "VALUES (?, 'event_category_unified', ?)",
                    (tid, json.dumps({"event_key": ek, "to": line_cat}, ensure_ascii=False)),
                )
            unified += 1

        conn.execute(
            "INSERT OR REPLACE INTO system_settings (key, value, updated_at) "
            "VALUES ('tree_v1_migrated', '1', ?)",
            (now,),
        )
        print(f"三级树迁移完成：{len(rows)} 条打地域/主体标签，"
              f"分类按动词重判改动 {changed} 条，{unified} 条事件线统一分类")

    # 地域/主体判定：规则自带版本号（app.geo.JUDGE_VERSION），版本变了才全库重判。
    # v1 标题→摘要；v2 标题→全文（按实体出现次数，≥2 次才认，宁可判"未识别"也不误挂）；
    # v3 主体词典补齐常见公司/机构（微软、谷歌、小红书…），并纠正 Stability AI（英国）、
    #    Cohere（加拿大）此前误挂"美国"。
    # 注意：版本升级会覆盖全表 region/entity。目前页面只读、没有人工修正入口，
    # 所以安全；将来开放人工修正时，必须加"人工字段"标记并在重判时跳过。
    from app.geo import JUDGE_VERSION, judge as _judge
    stored_ver = conn.execute(
        "SELECT value FROM system_settings WHERE key = 'geo_judge_version'"
    ).fetchone()
    if not stored_ver or str(stored_ver["value"]) != str(JUDGE_VERSION):
        rows2 = conn.execute(
            "SELECT id, title, COALESCE(raw_content,'') AS rc, COALESCE(region,'') AS region, "
            "COALESCE(entity,'') AS entity, COALESCE(event_key,'') AS ek, news_date "
            "FROM topics"
        ).fetchall()

        moved = 0
        line_rows: dict[str, list] = {}
        for row in rows2:
            nr, ne, _k = _judge(row["title"], row["rc"])
            if (nr, ne) != (row["region"], row["entity"]):
                moved += 1
            conn.execute(
                "UPDATE topics SET region = ?, entity = ?, updated_at = ? WHERE id = ?",
                (nr, ne, now, row["id"]),
            )
            if row["ek"]:
                line_rows.setdefault(row["ek"], []).append(row)

        # 事件线必须整线待在同一格子里：以最早一条（与树页线名一致）为准。
        unified2 = 0
        for ek, members in line_rows.items():
            if len(members) < 2:
                continue
            members.sort(key=lambda r: (r["news_date"] or "", r["id"]))
            rep = members[0]
            for m in members:
                conn.execute(
                    "UPDATE topics SET region = ?, entity = ? WHERE id = ?",
                    (rep["region"], rep["entity"], m["id"]),
                )
            unified2 += 1

        conn.execute(
            "INSERT OR REPLACE INTO system_settings (key, value, updated_at) "
            "VALUES ('geo_judge_version', ?, ?)",
            (str(JUDGE_VERSION), now),
        )
        print(f"地域/主体判定 v{JUDGE_VERSION} 完成：{len(rows2)} 条重判，"
              f"改动 {moved} 条，{unified2} 条事件线统一地域/主体")

    # 补漏闸：region/entity 为空的行意味着它在归类树里查不到（树按这两列分格）。
    # 存量由上面的版本迁移处理，但抓取、手动创建、Codex 入库是三条彼此独立的
    # INSERT，任何一条漏了列，新素材就会悄悄消失。这里每次启动扫一遍，只处理
    # 漏写的行，是"新素材进不了树"这类问题的兜底，不替代入库处的正常写入。
    orphans = conn.execute(
        "SELECT id, title, COALESCE(raw_content,'') AS rc, COALESCE(summary,'') AS sm "
        "FROM topics WHERE COALESCE(region,'') = '' OR COALESCE(entity,'') = ''"
    ).fetchall()
    if orphans:
        for row in orphans:
            nr, ne, _k = _judge(row["title"], row["rc"] or row["sm"])
            conn.execute(
                "UPDATE topics SET region = ?, entity = ?, updated_at = ? WHERE id = ?",
                (nr, ne, now, row["id"]),
            )
        print(f"地域/主体补漏：{len(orphans)} 条此前未打标签的选题已补齐")

    conn.commit()


if __name__ == "__main__":
    init_database()
