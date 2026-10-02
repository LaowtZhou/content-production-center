"""内容生产中心启动器 —— 后台启动 / 停止 8774 服务。

用法：
    python scripts/launcher.py                 后台启动服务（终端关闭后继续运行）
    python scripts/launcher.py --open-browser  后台启动并打开浏览器
    python scripts/launcher.py --stop          停止由本启动器启动的服务

说明（2026-10-03 重建）：原 scripts/launcher.py 被误删，本文件按 start.bat / stop.bat
的调用方式与 data/logs/launcher.log 的既有行为重写：

- 服务以独立后台进程运行（关闭终端不影响），日志写入 data/logs/server.log；
- 启动器只等待 http://127.0.0.1:8774/api/health 变为就绪后退出；
- 停止时只结束"已验证属于本服务"的进程（PID 文件记录，或 8774 端口占用者且命令行确为
  uvicorn app.main），不会误杀占用 8774 的其他程序。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
LOG_DIR = DATA_DIR / "logs"
RUN_DIR = DATA_DIR / "run"
LAUNCHER_LOG = LOG_DIR / "launcher.log"
SERVER_LOG = LOG_DIR / "server.log"
PID_FILE = RUN_DIR / "server.pid"

_UVICORN_TARGET = "app.main:app"

# 通过 app/runtime.py 取地址，保证启动器、服务与任务回传用同一个地址。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app.runtime import APP_HOST, APP_PORT, APP_BASE_URL  # noqa: E402

HEALTH_URL = f"{APP_BASE_URL}/api/health"
READY_TIMEOUT = 60.0


# ===== 日志 =====

def _log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    with open(LAUNCHER_LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(line)


# ===== 进程 / 端口探查 =====

def _port_owner_pids() -> set[int]:
    """返回正在监听 8774 的进程 PID（LISTENING）。"""
    pids: set[int] = set()
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception:
        return pids
    needle = f":{APP_PORT}"
    for raw in out.splitlines():
        parts = raw.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and "LISTENING" in raw.upper():
            local, pid = parts[1], parts[-1]
            if local.endswith(needle) and pid.isdigit():
                pids.add(int(pid))
    return pids


def _process_commandline(pid: int) -> str:
    """读取进程命令行（用于确认这个 PID 确实是本服务，而不是误伤其他程序）。"""
    try:
        out = subprocess.run(
            ["wmic", "process", "where", f"processid={pid}", "get", "commandline", "/value"],
            capture_output=True, text=True, timeout=15,
        ).stdout
        text = out.strip()
        if text:
            return text
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
            capture_output=True, text=True, timeout=20,
        ).stdout
        return (out or "").strip()
    except Exception:
        return ""


def _is_our_server(pid: int) -> bool:
    """命令行里同时出现 uvicorn 与 app.main 才认作本服务。"""
    cmd = _process_commandline(pid).lower()
    return "uvicorn" in cmd and _UVICORN_TARGET.lower() in cmd


def _read_pid_file() -> int | None:
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        return pid if pid > 0 else None
    except Exception:
        return None


def _find_running_server() -> int | None:
    """定位本服务进程：先信 PID 文件，再用端口占用者 + 命令行双重验证。"""
    pid = _read_pid_file()
    if pid and pid in _port_owner_pids() and _is_our_server(pid):
        return pid
    for candidate in _port_owner_pids():
        if _is_our_server(candidate):
            _write_pid_file(candidate)
            return candidate
    return None


def _write_pid_file(pid: int) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(pid), encoding="utf-8")


def _clear_pid_file() -> None:
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


# ===== 就绪探测 =====

def _is_ready(timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except urllib.error.HTTPError as exc:
        return 200 <= exc.code < 400
    except Exception:
        return False


# ===== 启动 / 停止 =====

def start(open_browser: bool = False) -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    RUN_DIR.mkdir(parents=True, exist_ok=True)

    running = _find_running_server()
    if running:
        _log(f"服务已在运行（PID {running}）：{APP_BASE_URL}")
        if open_browser:
            webbrowser.open(APP_BASE_URL)
        return 0

    others = _port_owner_pids()
    if others:
        _log(f"端口 {APP_PORT} 已被其他程序占用（PID {sorted(others)}），"
             f"启动器不会结束它。请先关闭该程序再启动。")
        return 1

    python_exe = ROOT / ".venv" / "Scripts" / "python.exe"
    if not python_exe.is_file():
        python_exe = Path(sys.executable)

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(ROOT)

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | \
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)

    _log(f"正在启动内容生产中心：{APP_BASE_URL}（服务日志：{SERVER_LOG}）")
    log_handle = open(SERVER_LOG, "ab")
    try:
        process = subprocess.Popen(
            [str(python_exe), "-m", "uvicorn", _UVICORN_TARGET,
             "--host", APP_HOST, "--port", str(APP_PORT)],
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            close_fds=True,
        )
    except Exception as exc:
        log_handle.close()
        _log(f"启动失败：{exc}")
        return 1

    _write_pid_file(process.pid)

    deadline = time.time() + READY_TIMEOUT
    while time.time() < deadline:
        if process.poll() is not None:
            _log(f"服务进程已退出（返回码 {process.returncode}）。"
                 f"请查看 {SERVER_LOG} 与 {LAUNCHER_LOG} 排查原因。")
            _clear_pid_file()
            log_handle.close()
            return 1
        if _is_ready():
            _log(f"服务已就绪（ready）：{APP_BASE_URL}")
            if open_browser:
                webbrowser.open(APP_BASE_URL)
            log_handle.close()
            return 0
        time.sleep(0.3)

    log_handle.close()
    _log(f"等待就绪超时（{READY_TIMEOUT:.0f} 秒）。请查看 {SERVER_LOG} 排查原因。")
    return 1


def stop() -> int:
    pid = _find_running_server()
    if not pid:
        _log("没有找到由本启动器启动的内容生产中心服务，无需停止。")
        return 0

    _log(f"正在停止已验证的内容生产中心进程（PID {pid}）。")
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:
        _log(f"停止失败：{exc}")
        return 1

    deadline = time.time() + 20
    while time.time() < deadline:
        if not _is_ready(timeout=1.0) and pid not in _port_owner_pids():
            break
        time.sleep(0.3)

    _clear_pid_file()
    _log("服务已停止。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="内容生产中心启动器")
    parser.add_argument("--stop", action="store_true", help="停止服务")
    parser.add_argument("--open-browser", action="store_true", help="启动后打开浏览器")
    args = parser.parse_args()

    if args.stop:
        return stop()
    return start(open_browser=args.open_browser)


if __name__ == "__main__":
    raise SystemExit(main())
