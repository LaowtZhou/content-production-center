import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from app import config
from app.services import production_service as service
from app.services import feedback_service
from app.services import operator_service
from app.services import operations_service
from app.services import publish_validator
from app.services import scoring_engine
from app.services import topic_parser
from app.services import stage_audit
from app.routers import topics as topics_router
from scripts import init_db


class ProductionServiceTest(unittest.TestCase):
    def test_stage_audit_keeps_history_and_limits_media(self):
        task = service.create_task(self.topic_id)['task']
        stage = service.start_stage(task['id'], 'chief_editor_plan', 'test')
        service.fail_stage(stage['stage_id'], 'connection lost', {}, 'test')
        stage = service.start_stage(task['id'], 'chief_editor_plan', 'test')
        service.complete_stage(stage['stage_id'], [str(self.cover), str(self.root / 'outside.png')],
                               {'decision': 'pass', 'used_skills': ['test-skill']}, 'test')
        details = stage_audit.audit(task['id'], 'chief_editor_plan')
        self.assertEqual(details['attempts'][0]['used_skills'], ['test-skill'])
        self.assertIsNone(details['attempts'][0]['model'])
        self.assertEqual(details['attempts'][1]['error'], 'connection lost')
        self.assertNotIn('callback_token', details)
        images = stage_audit.audit(task['id'], 'editor')['images']
        self.assertEqual(len(images), 1)
        self.assertEqual(stage_audit.image_path(task['id'], images[0]['id']), self.cover.resolve())
        with self.assertRaises(ValueError):
            stage_audit.image_path(task['id'], '../outside.png')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.original_config_db = config.DB_PATH
        self.original_init_db = init_db.DB_PATH
        self.db_path = self.root / "production_center.db"
        config.DB_PATH = self.db_path
        init_db.DB_PATH = str(self.db_path)
        init_db.init_database()

        self.vault = self.root / "vault"
        self.project = self.vault / "projects" / "test-project"
        self.project.mkdir(parents=True)
        self.article = self.project / "article.md"
        self.article.write_text(
            "---\nauthor: 老顽童周老师\ndigest: 测试摘要\nbanner_path: assets/cover.png\n---\n\n## 测试判断\n\n> **这是测试稿的核心判断。**\n\n**正文重点一。**\n\n## 测试结论\n\n**正文重点二。**\n\n- 条件一\n- 条件二\n- 条件三\n",
            encoding="utf-8",
        )
        self.cover = self.project / "cover.png"
        Image.new("RGB", (1200, 675), "#0f172a").save(self.cover)
        self.skills = self.root / "skills"
        for skill_name in service.REQUIRED_SKILLS:
            skill_dir = self.skills / skill_name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(f"# {skill_name}\n", encoding="utf-8")

        config.set_setting("content_vault_dir", str(self.vault))
        config.set_setting("content_skills_dir", str(self.skills))
        config.set_setting("task_contract_dir", str(self.root / "contracts"))
        config.set_setting("production_center_base_url", "http://127.0.0.1:8774")
        conn = config.get_db()
        cursor = conn.execute(
            """INSERT INTO topics (title, summary, raw_content, source_type, status, tags, created_at, updated_at)
            VALUES ('Test topic', 'summary', 'raw source', 'manual', 'unused', '[]', ?, ?)""",
            (service.now_iso(), service.now_iso()),
        )
        self.topic_id = cursor.lastrowid
        # 建创作任务的前提是已有评分记录（使用状态与之无关）
        conn.execute(
            """INSERT INTO topic_scores
            (topic_id, traffic_score, opinion_score, novelty_score, total_score, model_used)
            VALUES (?, 7, 7, 7, 7, 'test')""",
            (self.topic_id,),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        config.DB_PATH = self.original_config_db
        init_db.DB_PATH = self.original_init_db
        self.temp.cleanup()

    def test_task_contract_lifecycle_preserves_publish_boundary(self):
        result = service.create_task(self.topic_id, selected_note="test")
        task = result["task"]
        self.assertTrue(result["created"])
        self.assertTrue(Path(result["contract_path"]).is_file())
        self.assertEqual(9, len(json.loads(task["skill_manifest_json"])))
        contract_text = Path(result["contract_path"]).read_text(encoding="utf-8")
        self.assertIn(str(self.vault), contract_text)
        self.assertIn(str(self.skills), contract_text)

        self.assertEqual("claimed", service.claim_task(task["id"], "Codex-test")["status"])
        self.assertEqual("running", service.start_task(task["id"], "Codex-test")["status"])
        for stage in service.PRODUCTION_STAGES:
            started = service.start_stage(task["id"], stage["key"], stage["role"])
            service.complete_stage(started["stage_id"], [], {"decision": "pass"}, stage["role"])
        callback = service.callback_task(task["id"], {
            "callback_token": task["callback_token"],
            "status": "publish_ready",
            "agent_name": "Codex-test",
            "vault_project_path": str(self.project),
            "primary_article_path": str(self.article),
            "artifacts": [{"kind": "cover", "path": str(self.cover), "generator": "imagegen", "generation_id": "test-generation"}],
            "used_skills": ["imagegen"],
            "self_check": {"image_generation": {"mode": "imagegen", "reviewed": True, "quality": "pass"}},
        })
        self.assertEqual("publish_ready", callback["status"])
        self.assertEqual("publish_ready", service.get_task(task["id"])["status"])

        conn = config.get_db()
        topic = conn.execute("SELECT status, selected_at FROM topics WHERE id = ?", (self.topic_id,)).fetchone()
        conn.close()
        # 建创作任务只补记"被选去创作"的时间，不再替人改素材使用状态
        self.assertEqual("unused", topic["status"])
        self.assertTrue(topic["selected_at"])

    def test_auto_queue_is_idempotent_for_repeated_triggers(self):
        config.set_setting("production_mode", "autonomous")
        conn = config.get_db()
        conn.execute(
            """INSERT INTO topic_scores
            (topic_id, traffic_score, opinion_score, novelty_score, total_score, model_used)
            VALUES (?, 8, 7, 9, 8, 'Codex')""",
            (self.topic_id,),
        )
        conn.commit()
        conn.close()

        first = service.auto_queue_tasks()
        second = service.auto_queue_tasks()

        self.assertEqual(1, len(first["created"]))
        self.assertEqual(0, len(second["created"]))
        self.assertIn("上限", second["message"])
        conn = config.get_db()
        count = conn.execute("SELECT COUNT(*) AS count FROM production_tasks WHERE topic_id = ?", (self.topic_id,)).fetchone()["count"]
        conn.close()
        self.assertEqual(1, count)

    def test_callback_normalises_string_artifacts(self):
        result = service.create_task(self.topic_id, selected_note="string artifacts")
        task = result["task"]
        service.claim_task(task["id"], "OpenClaw-test")
        service.start_task(task["id"], "OpenClaw-test")
        for stage in service.PRODUCTION_STAGES:
            started = service.start_stage(task["id"], stage["key"], stage["role"])
            service.complete_stage(started["stage_id"], [], {"decision": "pass"}, stage["role"])
        callback = service.callback_task(task["id"], {
            "callback_token": task["callback_token"],
            "status": "draft_ready",
            "agent_name": "OpenClaw-test",
            "vault_project_path": str(self.project),
            "primary_article_path": str(self.article),
            "artifacts": [str(self.article)],
            "used_skills": ["content-creation-workflow"],
        })
        self.assertEqual("draft_ready", callback["status"])
        self.assertEqual(3, len(callback["artifact_checks"]))

    def test_publish_ready_rejects_non_imagegen_artifact(self):
        result = service.create_task(self.topic_id, selected_note="quality gate")
        task = result["task"]
        service.claim_task(task["id"], "Codex-test")
        service.start_task(task["id"], "Codex-test")
        for stage in service.PRODUCTION_STAGES:
            started = service.start_stage(task["id"], stage["key"], stage["role"])
            service.complete_stage(started["stage_id"], [], {"decision": "pass"}, stage["role"])
        with self.assertRaisesRegex(ValueError, "imagegen"):
            service.callback_task(task["id"], {
                "callback_token": task["callback_token"],
                "status": "publish_ready",
                "agent_name": "Codex-test",
                "vault_project_path": str(self.project),
                "primary_article_path": str(self.article),
                "artifacts": [{"kind": "cover", "path": str(self.cover), "generator": "PIL", "generation_id": "fake"}],
                "used_skills": ["imagegen"],
                "self_check": {"image_generation": {"mode": "imagegen", "reviewed": True, "quality": "pass"}},
            })

    def test_publish_ready_requires_all_editorial_stages(self):
        result = service.create_task(self.topic_id, selected_note="stage gate")
        task = result["task"]
        service.claim_task(task["id"], "Codex-test")
        service.start_task(task["id"], "Codex-test")
        with self.assertRaisesRegex(ValueError, "多代理工作流尚未完成"):
            service.callback_task(task["id"], {
                "callback_token": task["callback_token"],
                "status": "publish_ready",
                "agent_name": "Codex-test",
                "vault_project_path": str(self.project),
                "primary_article_path": str(self.article),
                "artifacts": [{"kind": "cover", "path": str(self.cover), "generator": "imagegen", "generation_id": "test-generation"}],
                "used_skills": ["imagegen"],
                "self_check": {"image_generation": {"mode": "imagegen", "reviewed": True, "quality": "pass"}},
            })

    def test_wechat_publish_validator_requires_scan_layout(self):
        plain = "---\nauthor: 老顽童周老师\n---\n\n## 只有标题\n\n这是一段没有任何重点层级的普通正文。"
        result = publish_validator._validate_wechat_scan_layout(plain)
        self.assertFalse(result["ok"])
        self.assertIn("核心判断加粗", "；".join(result["errors"]))

    def test_reconcile_completed_stage_evidence_promotes_publish_ready(self):
        article_text = """---\nauthor: 老顽童周老师\ndigest: 一篇可发布的测试稿\nbanner_path: assets/cover.png\n---\n\n## 核心判断\n\n> **这是一句需要被看见的判断。**\n\n**这是正文重点一。**\n\n正文解释。\n\n## 下一步观察\n\n**这是正文重点二。**\n\n- 观察一\n- 观察二\n- 观察三\n"""
        self.article.write_text(article_text, encoding="utf-8")
        record = self.project / "图片生成记录与视觉质检.md"
        record.write_text("| `cover.png` | 封面 | `exec-test-generation` |", encoding="utf-8")
        plan = self.project / "plan.md"
        writer = self.project / "writer.md"
        review = self.project / "质检报告.md"
        for path in (plan, writer, review):
            path.write_text("test", encoding="utf-8")

        created = service.create_task(self.topic_id, selected_note="reconcile")
        task = created["task"]
        service.claim_task(task["id"], "Codex-test")
        service.start_task(task["id"], "Codex-test")
        stage_outputs = {
            "chief_editor_plan": [str(plan)],
            "writer": [str(writer)],
            "editor": [str(self.article), str(self.cover), str(record)],
            "chief_editor_review": [str(review)],
        }
        for stage in service.PRODUCTION_STAGES:
            started = service.start_stage(task["id"], stage["key"], "Codex-test")
            result = {"decision": "pass", "used_skills": ["content-publish-prepare", "imagegen"]}
            service.complete_stage(started["stage_id"], stage_outputs[stage["key"]], result, "Codex-test")

        recovered = service.reconcile_completed_task(task["id"])

        self.assertEqual("publish_ready", recovered["status"])
        self.assertEqual("publish_ready", service.get_task(task["id"])["status"])

    def test_draft_ready_rejects_incomplete_editorial_stages(self):
        result = service.create_task(self.topic_id, selected_note="draft gate")
        task = result["task"]
        service.claim_task(task["id"], "Codex-test")
        service.start_task(task["id"], "Codex-test")
        with self.assertRaisesRegex(ValueError, "不能以 draft_ready 收口"):
            service.callback_task(task["id"], {
                "callback_token": task["callback_token"],
                "status": "draft_ready",
                "agent_name": "Codex-test",
                "vault_project_path": str(self.project),
                "primary_article_path": str(self.article),
                "artifacts": [str(self.article)],
                "used_skills": ["content-creation-workflow"],
            })

    def test_failed_callback_closes_running_stage_for_retry(self):
        result = service.create_task(self.topic_id, selected_note="failure recovery")
        task = result["task"]
        service.claim_task(task["id"], "Codex-test")
        service.start_task(task["id"], "Codex-test")
        stage = service.start_stage(task["id"], "chief_editor_plan", "主编 Agent")
        service.callback_task(task["id"], {
            "callback_token": task["callback_token"],
            "status": "failed",
            "agent_name": "Codex-test",
            "summary": "执行器中断",
            "error_detail": "阶段无输出",
        })
        failed = next(item for item in service.get_task(task["id"])["stages"] if item["id"] == stage["stage_id"])
        self.assertEqual("failed", failed["status"])

    def test_stage_complete_rejects_rework_decision(self):
        result = service.create_task(self.topic_id, selected_note="stage decision")
        task = result["task"]
        started = service.start_stage(task["id"], "chief_editor_plan", "主编 Agent")
        with self.assertRaisesRegex(ValueError, "decision=pass"):
            service.complete_stage(started["stage_id"], [], {"decision": "rework", "return_to_stage": "writer"}, "主编 Agent")

    def test_review_rework_can_restart_completed_target_stage(self):
        result = service.create_task(self.topic_id, selected_note="review rework")
        task = result["task"]
        started = {}
        for stage_key in ("chief_editor_plan", "writer", "editor"):
            started[stage_key] = service.start_stage(task["id"], stage_key, "Codex-test")
            service.complete_stage(started[stage_key]["stage_id"], [], {"decision": "pass"}, "Codex-test")
        review = service.start_stage(task["id"], "chief_editor_review", "Codex-test")
        service.fail_stage(
            review["stage_id"],
            "编辑稿含内部说明",
            {"decision": "rework", "return_to_stage": "editor"},
            "Codex-test",
        )

        retried = service.start_stage(task["id"], "editor", "Codex-test")

        self.assertFalse(retried["idempotent"])
        self.assertEqual(3, retried["attempt"])
        self.assertNotEqual(started["editor"]["stage_id"], retried["stage_id"])

    def test_stage_start_enforces_editorial_order(self):
        result = service.create_task(self.topic_id, selected_note="stage order")
        task = result["task"]
        with self.assertRaisesRegex(ValueError, "必须先完成主编策划"):
            service.start_stage(task["id"], "writer", "作者 Agent")

        plan = service.start_stage(task["id"], "chief_editor_plan", "主编 Agent")
        service.complete_stage(plan["stage_id"], [], {"decision": "pass"}, "主编 Agent")
        with self.assertRaisesRegex(ValueError, "必须先完成研究与正文"):
            service.start_stage(task["id"], "editor", "编辑 Agent")

    def test_claim_next_is_atomic_and_returns_contract(self):
        created = service.create_task(self.topic_id, selected_note="agent bridge")
        result = service.claim_next_task("OpenClaw-test")
        self.assertTrue(result["claimed"])
        self.assertEqual(created["task"]["id"], result["task_id"])
        self.assertEqual("claimed", service.get_task(result["task_id"])["status"])
        self.assertIn(result["task_id"], result["contract_text"])
        self.assertFalse(service.claim_next_task("OpenClaw-test")["claimed"])

    def test_claim_next_resumes_autonomous_task_with_running_stage(self):
        config.set_setting("production_mode", "autonomous")
        created = service.create_task(self.topic_id, selected_note="resume orphaned stage", mode="autonomous")
        task = created["task"]
        service.claim_task(task["id"], "Codex-test")
        service.start_task(task["id"], "Codex-test")
        for stage_key in ("chief_editor_plan", "writer"):
            started = service.start_stage(task["id"], stage_key, "Codex-test")
            service.complete_stage(started["stage_id"], [], {"decision": "pass"}, "Codex-test")
        conn = config.get_db()
        conn.execute("UPDATE production_tasks SET status = 'draft_ready' WHERE id = ?", (task["id"],))
        conn.commit()
        conn.close()
        editor = service.start_stage(task["id"], "editor", "Codex-test")

        resumed = service.claim_next_task("Codex-recovery")

        self.assertTrue(resumed["claimed"])
        self.assertEqual(task["id"], resumed["task_id"])
        refreshed = service.get_task(task["id"])
        self.assertEqual("claimed", refreshed["status"])
        self.assertEqual("running", next(item for item in refreshed["stage_summary"]["stages"] if item["key"] == "editor")["status"])
        self.assertEqual(editor["stage_id"], next(item for item in refreshed["stages"] if item["stage_key"] == "editor" and item["status"] == "running")["id"])

    def test_agent_score_accepts_nested_aliases_and_reports_input_errors(self):
        response = topics_router.agent_submit_scores({
            "pending_topics": [{
                "id": self.topic_id,
                "score": {"traffic": 8, "opinion": 7, "novelty": 6},
                "recommended_format": "article",
                "key_angle": "test angle",
            }, {
                "id": self.topic_id,
                "traffic_score": 11,
                "opinion_score": 5,
                "novelty_score": 5,
            }],
        })
        self.assertEqual(2, response["total"])
        self.assertEqual(1, response["success"])
        self.assertEqual(1, response["failed"])
        self.assertEqual(1, len(response["input_errors"]))
        self.assertEqual("scored", response["results"][0]["status"])

    def test_scoring_context_includes_full_history_summary(self):
        conn = config.get_db()
        now = service.now_iso()
        conn.execute(
            """INSERT INTO publication_observations
            (id, opc_publication_id, platform, title, metrics_json, created_at, updated_at)
            VALUES ('history-1', 'history-publication-1', 'wechat', 'History article 1', ?, ?, ?)""",
            (json.dumps({"latest_read_count": 100}), now, now),
        )
        conn.execute(
            """INSERT INTO publication_observations
            (id, opc_publication_id, platform, title, metrics_json, created_at, updated_at)
            VALUES ('history-2', 'history-publication-2', 'bilibili', 'History video 1', ?, ?, ?)""",
            (json.dumps({"latest_play_count": 300}), now, now),
        )
        conn.commit()
        summary = scoring_engine._get_performance_summary(conn)
        conn.close()
        self.assertIn("全量发布观察 2 条", summary)
        self.assertIn("wechat", summary)
        self.assertIn("中位数", summary)

    def test_parse_failure_is_recorded_as_unprocessed_source_issue(self):
        source_file = self.root / "2026-08-31-01-invalid.md"
        source_file.write_text("这不是一个可识别的 OpenClaw Markdown 文件", encoding="utf-8")
        self.assertEqual(0, topic_parser.process_file(str(source_file)))
        self.assertEqual(0, topic_parser.process_file(str(source_file)))

        conn = config.get_db()
        source = conn.execute("SELECT processed FROM topic_sources WHERE source_ref = ?", (str(source_file),)).fetchone()
        issue = conn.execute("SELECT issue_type, status FROM source_issues WHERE source_id = (SELECT id FROM topic_sources WHERE source_ref = ?)", (str(source_file),)).fetchone()
        issue_count = conn.execute("SELECT COUNT(*) AS count FROM source_issues WHERE source_id = (SELECT id FROM topic_sources WHERE source_ref = ?)", (str(source_file),)).fetchone()["count"]
        conn.close()
        self.assertEqual(0, source["processed"])
        self.assertEqual(("source_parse_failed", "open"), (issue["issue_type"], issue["status"]))
        self.assertEqual(1, issue_count)

    def test_utf8_bom_markdown_is_parsed(self):
        source_file = self.root / "2026-08-31-02-bom.md"
        source_file.write_text("\ufeff# BOM 标题\n\n正文内容", encoding="utf-8")
        self.assertGreater(topic_parser.process_file(str(source_file)), 0)

    def test_recovered_source_issue_is_resolved(self):
        source_file = self.root / "2026-08-31-03-recovered.md"
        source_file.write_text("无效内容", encoding="utf-8")
        self.assertEqual(0, topic_parser.process_file(str(source_file)))
        source_file.write_text("\ufeff# 恢复标题\n\n正文内容", encoding="utf-8")
        self.assertGreater(topic_parser.process_file(str(source_file)), 0)
        self.assertEqual(0, topic_parser.process_file(str(source_file)))

        conn = config.get_db()
        open_count = conn.execute(
            "SELECT COUNT(*) AS count FROM source_issues WHERE source_id = (SELECT id FROM topic_sources WHERE source_ref = ?) AND status = 'open'",
            (str(source_file),),
        ).fetchone()["count"]
        conn.close()
        self.assertEqual(0, open_count)

    def test_opc_sync_is_idempotent(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return [{
                    "content_item_id": "content-1",
                    "latest_publication_id": "publication-1",
                    "platform": "wechat",
                    "title": "Published title",
                    "published_at": "2026-08-10T10:00:00",
                    "captured_at": "2026-08-10T12:00:00",
                    "latest_read_count": 123,
                }]

        config.set_setting("opc_erp_api_base", "http://opc.test")
        original_get = service.httpx.get
        service.httpx.get = lambda url, timeout: Response()
        try:
            self.assertEqual({"received": 1, "inserted": 1, "updated": 0}, service.sync_opc_performance())
            self.assertEqual({"received": 1, "inserted": 0, "updated": 1}, service.sync_opc_performance())
        finally:
            service.httpx.get = original_get

    def test_feedback_signals_are_derived_idempotently(self):
        conn = config.get_db()
        for index, reads in enumerate((100, 200, 300, 400, 500), start=1):
            observation_id = f"observation-{index}"
            conn.execute(
                """INSERT INTO publication_observations
                (id, opc_publication_id, platform, title, published_at, captured_at, metrics_json, created_at, updated_at)
                VALUES (?, ?, 'wechat', ?, ?, ?, ?, ?, ?)""",
                (
                    observation_id,
                    f"publication-{index}",
                    f"Article {index}",
                    f"2026-08-{index:02d}T10:00:00",
                    f"2026-08-{index:02d}T12:00:00",
                    json.dumps({"latest_read_count": reads, "latest_like_count": reads // 10}),
                    service.now_iso(),
                    service.now_iso(),
                ),
            )
        conn.commit()
        conn.close()

        first = feedback_service.recompute_signals()
        self.assertEqual(5, first["observations"])
        self.assertGreater(first["inserted"], 0)
        second = feedback_service.recompute_signals()
        self.assertEqual(0, second["inserted"])
        self.assertGreater(second["updated"], 0)

        signals = feedback_service.list_signals(status="open", platform="wechat")
        self.assertTrue(any(signal["signal_type"] == "high_reach" for signal in signals))
        signal_id = signals[0]["id"]
        handled = feedback_service.handle_signal(signal_id, "accepted", "test", "verified")
        self.assertEqual("accepted", handled["status"])
        refreshed = feedback_service.list_signals(platform="wechat")
        self.assertEqual("accepted", next(item["status"] for item in refreshed if item["id"] == signal_id))

    def test_feedback_review_creation_is_audited_and_idempotent(self):
        conn = config.get_db()
        timestamp = service.now_iso()
        for index, reads in enumerate((100, 200, 300, 400), start=1):
            baseline_time = f"2026-08-{index:02d}T10:00:00"
            conn.execute(
                """INSERT INTO publication_observations
                (id, opc_publication_id, platform, title, published_at, metrics_json, created_at, updated_at)
                VALUES (?, ?, 'wechat', ?, ?, ?, ?, ?)""",
                (
                    f"observation-baseline-{index}",
                    f"publication-baseline-{index}",
                    f"Baseline article {index}",
                    baseline_time,
                    json.dumps({"latest_read_count": reads}),
                    timestamp,
                    timestamp,
                ),
            )
        conn.execute(
            """INSERT INTO publication_observations
            (id, opc_publication_id, platform, title, published_at, metrics_json, created_at, updated_at)
            VALUES ('observation-review', 'publication-review', 'wechat', 'Review article', ?, ?, ?, ?)""",
            (timestamp, json.dumps({"latest_read_count": 1000}), timestamp, timestamp),
        )
        conn.commit()
        conn.close()
        feedback_service.recompute_signals()
        signals = feedback_service.list_signals(platform="wechat")
        signal = next(item for item in signals if item["observation_id"] == "observation-review")
        first = feedback_service.create_review_from_signal(signal["id"])
        second = feedback_service.create_review_from_signal(signal["id"])
        self.assertEqual(first["review_case_id"], second["review_case_id"])
        conn = config.get_db()
        count = conn.execute("SELECT COUNT(*) AS count FROM review_cases WHERE observation_id = 'observation-review'").fetchone()["count"]
        conn.close()
        self.assertEqual(1, count)

    def test_operations_overview_is_derived_from_existing_objects(self):
        conn = config.get_db()
        now = service.now_iso()
        # 两条没有评分记录的素材：待评分是事实（有无 topic_scores），与使用状态无关
        conn.execute(
            "INSERT INTO topics (title, source_type, status, tags, created_at, updated_at) VALUES ('Unscored topic', 'manual', 'unused', '[]', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO topics (title, source_type, status, tags, created_at, updated_at) VALUES ('Used topic', 'manual', 'used', '[]', ?, ?)",
            (now, now),
        )
        conn.commit()
        conn.close()

        overview = operations_service.get_operations_overview()
        self.assertEqual(3, overview["counts"]["topics"])
        self.assertEqual(0, overview["counts"]["source_issues_open"])
        self.assertEqual(2, next(item["count"] for item in overview["pipeline"] if item["key"] == "unscored"))
        self.assertEqual(1, next(item["count"] for item in overview["pipeline"] if item["key"] == "used"))
        self.assertEqual("unscored", overview["next_action"]["key"])
        self.assertEqual("degraded", next(item["status"] for item in overview["health"] if item["key"] == "llm"))

    def test_operator_plan_adjusts_collector_and_starts_editorial_task_idempotently(self):
        jobs_path = self.root / "openclaw" / "jobs.json"
        jobs_path.parent.mkdir(parents=True)
        jobs_path.write_text(json.dumps({"jobs": [
            {"name": "公众号数据周更", "enabled": True, "payload": {"message": "抓取公众号文章数据"}},
            {"name": "AI日报每日推送", "enabled": True, "payload": {"message": "抓取 AI 日报"}},
        ]}, ensure_ascii=False), encoding="utf-8")
        config.set_setting("openclaw_cron_jobs_path", str(jobs_path))
        config.set_setting("production_mode", "autonomous")

        plan = {
            "cycle_id": "cycle-test-1",
            "summary": "根据近期公众号表现补抓标题与首屏数据，并主动做一篇选题",
            "basis": ["signal-test-1"],
            "collector_adjustments": [{
                "job_name": "公众号数据周更",
                "instruction": "下轮重点补抓每篇文章的标题、发表时间、阅读、点赞、评论、分享和推荐来源，并保留完整字段。",
                "reason": "近期需要比较标题与分发入口",
                "evidence_ids": ["signal-test-1"],
            }],
            "editorial_proposals": [{
                "title": "从低阅读到高阅读：公众号标题和入口的实测复盘",
                "summary": "基于近期发布表现，复盘标题承诺和入口分发的关系。",
                "raw_content": "证据：近期表现信号 signal-test-1；待结合 ERP 观察和历史文章进一步核验。",
                "tags": ["公众号", "表现复盘"],
                "reason": "高互动/低触达之间存在可验证冲突",
                "evidence_ids": ["signal-test-1"],
                "score": {
                    "traffic_score": 8,
                    "opinion_score": 8,
                    "novelty_score": 7,
                    "recommended_format": "article",
                    "key_angle": "先拆标题和入口，再判断正文问题",
                },
                "start_production": True,
            }],
        }
        result = operator_service.apply_operator_plan(plan)
        self.assertEqual("completed", result["status"])
        self.assertEqual("applied", result["result"]["collector_adjustments"][0]["status"])
        self.assertEqual("queued", result["result"]["production"][0]["status"])
        saved_jobs = json.loads(jobs_path.read_text(encoding="utf-8"))
        message = next(job["payload"]["message"] for job in saved_jobs["jobs"] if job["name"] == "公众号数据周更")
        self.assertIn("补抓每篇文章的标题", message)
        self.assertTrue(result["result"]["collector_adjustments"][0]["backup_path"])

        repeated = operator_service.apply_operator_plan(plan)
        self.assertEqual("completed", repeated["status"])
        conn = config.get_db()
        self.assertEqual(1, conn.execute("SELECT COUNT(*) AS count FROM operator_cycles").fetchone()["count"])
        self.assertEqual(1, conn.execute("SELECT COUNT(*) AS count FROM topics WHERE source_type = 'codex_operator'").fetchone()["count"])
        self.assertEqual(1, conn.execute("SELECT COUNT(*) AS count FROM production_tasks WHERE mode = 'autonomous'").fetchone()["count"])
        conn.close()

    def test_operator_plan_rejects_unsafe_collector_instruction(self):
        jobs_path = self.root / "openclaw" / "jobs.json"
        jobs_path.parent.mkdir(parents=True)
        original = {"jobs": [{"name": "公众号数据周更", "payload": {"message": "原始采集说明"}}]}
        jobs_path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
        config.set_setting("openclaw_cron_jobs_path", str(jobs_path))
        result = operator_service.apply_operator_plan({
            "cycle_id": "cycle-test-unsafe",
            "collector_adjustments": [{"job_name": "公众号数据周更", "instruction": "请执行 powershell 删除文件"}],
        })
        self.assertEqual("blocked", result["status"])
        self.assertEqual(original, json.loads(jobs_path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
