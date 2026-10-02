"""内容生产中心的本地 HTTP 桥接器 —— Codex 运营执行器的唯一写入口。

约定（见 docs/codex-operator.md）：Codex 不要用 PowerShell / Invoke-RestMethod /
自行拼接 HTTP 请求，所有本地操作都通过本脚本完成。请求地址统一取自 app.runtime，
保证与服务、任务回传使用同一个地址（默认 http://127.0.0.1:8774）。

子命令：
    operator-context [--limit N]        读取运营上下文（ERP 表现、开放反馈、最近选题/任务、采集任务）
    operator-plan <计划文件>            提交本轮结构化运营计划（JSON 文件）
    scoring-pending                     读取待评分选题
    score <评分文件>                    提交评分（JSON，形如 {"scores": [...]}）
    auto-queue                          按自主模式生成并领取任务
    claim [--agent Codex]               领取下一个创作任务
    start <task_id> [--agent Codex]     开始某个任务
    task <task_id>                      查看任务详情
    stages <task_id>                    查看任务的岗位阶段
    stage-complete <stage_id> [--result 文件] [--output 路径 ...]
    stage-fail <stage_id> --error "原因" [--result 文件]
    callback <task_id> <回调文件>       回传任务结果（JSON）
    feedback-sync                       触发一次 ERP 反馈同步（常规由服务每小时自动执行）

说明（2026-10-03 重建）：原 scripts/codex_operator_bridge.py 被误删，本文件按
docs/codex-operator.md 的子命令清单与运行中服务的接口契约重写。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.runtime import APP_BASE_URL  # noqa: E402


def _emit(payload) -> None:
    if isinstance(payload, (dict, list)):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(payload)


def _request(method: str, path: str, params: dict | None = None,
             body: dict | None = None, timeout: int = 180):
    url = APP_BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        print(f"请求失败（HTTP {exc.code}）：{detail}", file=sys.stderr)
        raise SystemExit(1)
    except urllib.error.URLError as exc:
        print(f"无法连接内容生产中心（{APP_BASE_URL}）：{exc.reason}", file=sys.stderr)
        raise SystemExit(1)
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return raw


def _load_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="内容生产中心本地 HTTP 桥接器")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("operator-context", help="读取运营上下文")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("operator-plan", help="提交运营计划（JSON 文件）")
    p.add_argument("plan_file")

    sub.add_parser("scoring-pending", help="读取待评分选题")

    p = sub.add_parser("score", help="提交评分（JSON 文件）")
    p.add_argument("score_file")

    sub.add_parser("auto-queue", help="按自主模式生成并领取任务")

    p = sub.add_parser("claim", help="领取下一个创作任务")
    p.add_argument("--agent", default="Codex")

    p = sub.add_parser("start", help="开始某个任务")
    p.add_argument("task_id")
    p.add_argument("--agent", default="Codex")

    p = sub.add_parser("task", help="查看任务详情")
    p.add_argument("task_id")

    p = sub.add_parser("stages", help="查看任务的岗位阶段")
    p.add_argument("task_id")

    p = sub.add_parser("stage-complete", help="回传岗位阶段完成")
    p.add_argument("stage_id")
    p.add_argument("--agent", default="Codex")
    p.add_argument("--result", help="结果 JSON 文件")
    p.add_argument("--output", action="append", default=[], help="产物路径，可重复")

    p = sub.add_parser("stage-fail", help="回传岗位阶段失败")
    p.add_argument("stage_id")
    p.add_argument("--agent", default="Codex")
    p.add_argument("--error", required=True, help="失败原因")
    p.add_argument("--result", help="结果 JSON 文件")

    p = sub.add_parser("callback", help="回传任务结果（JSON 文件）")
    p.add_argument("task_id")
    p.add_argument("callback_file")

    sub.add_parser("feedback-sync", help="触发一次 ERP 反馈同步")

    args = parser.parse_args()

    if args.command == "operator-context":
        _emit(_request("GET", "/api/operations/operator-context", {"limit": args.limit}))

    elif args.command == "operator-plan":
        _emit(_request("POST", "/api/operations/operator-plan", body=_load_json(args.plan_file)))

    elif args.command == "scoring-pending":
        _emit(_request("GET", "/api/scoring-pending"))

    elif args.command == "score":
        _emit(_request("POST", "/api/topics/agent-score", body=_load_json(args.score_file)))

    elif args.command == "auto-queue":
        _emit(_request("POST", "/api/production/tasks/auto-queue", body={}))

    elif args.command == "claim":
        _emit(_request("POST", "/api/production/tasks/claim-next",
                       body={"agent_name": args.agent}))

    elif args.command == "start":
        _emit(_request("POST", f"/api/production/tasks/{args.task_id}/start",
                       body={"agent_name": args.agent}))

    elif args.command == "task":
        _emit(_request("GET", f"/api/production/tasks/{args.task_id}"))

    elif args.command == "stages":
        _emit(_request("GET", f"/api/production/tasks/{args.task_id}/stages"))

    elif args.command == "stage-complete":
        body = {
            "agent_name": args.agent,
            "result": _load_json(args.result) if args.result else {},
            "output_paths": list(args.output),
        }
        _emit(_request("POST", f"/api/production/stages/{args.stage_id}/complete", body=body))

    elif args.command == "stage-fail":
        body = {
            "agent_name": args.agent,
            "error_detail": args.error,
            "result": _load_json(args.result) if args.result else {},
        }
        _emit(_request("POST", f"/api/production/stages/{args.stage_id}/fail", body=body))

    elif args.command == "callback":
        _emit(_request("POST", f"/api/production/tasks/{args.task_id}/callback",
                       body=_load_json(args.callback_file)))

    elif args.command == "feedback-sync":
        _emit(_request("POST", "/api/production/feedback/sync", body={}))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
