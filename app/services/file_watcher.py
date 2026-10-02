"""文件监听器 - 递归监听OpenClaw输出目录，自动处理新文件

OpenClaw目录结构：
  ai-news/
    2026-08-07/
      2026-08-07-01-xxx.md
      2026-08-07-02-xxx.md
      AI日报-2026-08-07.html
      _duplicates/
      daily-images-2026-08-07/
    2026-08-08/
      ...
"""

import os
import threading
import time
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG_AVAILABLE = True
except ImportError:
    # 让工作台在依赖未安装时仍可启动、查看历史和手动扫描；不会静默假装正在监听。
    Observer = None
    WATCHDOG_AVAILABLE = False

    class FileSystemEventHandler:  # type: ignore[no-redef]
        pass

from app.config import get_setting

_watcher = None
_observer = None
_last_error = None

# 跳过的目录名
SKIP_DIRS = {"_duplicates"}

# 事件防抖：记录最近处理过的文件路径和时间戳
# key = filepath, value = timestamp
_recent_events: dict[str, float] = {}
_recent_events_lock = threading.Lock()
_DEBOUNCE_SECONDS = 5  # 同一文件5秒内只处理一次


class OpenClawFileHandler(FileSystemEventHandler):
    """处理OpenClaw输出的新文件"""

    def _should_process(self, filepath: str) -> bool:
        """判断文件是否应该处理"""
        # 只处理MD文件
        if not filepath.endswith(".md"):
            return False
        # 跳过 _duplicates 目录
        if "_duplicates" in filepath:
            return False
        # 跳过 daily-images 目录
        if "daily-images" in filepath:
            return False
        return True

    def _debounce(self, filepath: str) -> bool:
        """防抖检查：同一文件在 _DEBOUNCE_SECONDS 内只处理一次。
        返回 True 表示可以处理，False 表示应跳过。
        """
        now = time.time()
        with _recent_events_lock:
            last_time = _recent_events.get(filepath)
            if last_time is not None and (now - last_time) < _DEBOUNCE_SECONDS:
                return False
            _recent_events[filepath] = now
            # 清理过期记录（超过60秒的）
            expired = [k for k, v in _recent_events.items() if (now - v) > 60]
            for k in expired:
                del _recent_events[k]
        return True

    def on_created(self, event):
        if event.is_directory:
            return
        if not self._should_process(event.src_path):
            return
        if not self._debounce(event.src_path):
            print(f"[文件监听] 防抖跳过: {os.path.basename(event.src_path)}")
            return
        # 延迟1秒，等文件写入完成
        time.sleep(1)
        try:
            from app.services.topic_parser import process_file
            topic_id = process_file(event.src_path)
            if topic_id:
                print(f"[文件监听] 新文件已处理: {os.path.basename(event.src_path)} -> 选题#{topic_id}")
        except Exception as e:
            print(f"[文件监听] 处理文件失败 {event.src_path}: {e}")

    def on_moved(self, event):
        """有些工具先写临时文件再rename"""
        if event.is_directory:
            return
        if not self._should_process(event.dest_path):
            return
        if not self._debounce(event.dest_path):
            print(f"[文件监听] 防抖跳过(移动): {os.path.basename(event.dest_path)}")
            return
        time.sleep(1)
        try:
            from app.services.topic_parser import process_file
            topic_id = process_file(event.dest_path)
            if topic_id:
                print(f"[文件监听] 移动文件已处理: {os.path.basename(event.dest_path)} -> 选题#{topic_id}")
        except Exception as e:
            print(f"[文件监听] 处理文件失败 {event.dest_path}: {e}")


def start_watcher():
    """启动文件监听（递归监听所有子目录）"""
    global _watcher, _observer, _last_error
    if _observer and _observer.is_alive():
        return {"status": "running", "message": "文件监听已在运行"}
    if not WATCHDOG_AVAILABLE:
        _last_error = "watchdog 未安装"
        print("[文件监听] watchdog 未安装，已跳过实时监听；请安装 requirements.txt 后重启以启用自动采集")
        return {"status": "degraded", "message": _last_error}
    watch_dir = get_setting("openclaw_watch_dir", "")
    if not watch_dir or not os.path.isdir(watch_dir):
        _last_error = "OpenClaw 监听目录未配置或不存在"
        print(f"[文件监听] 监听目录未配置或不存在: {watch_dir}")
        return {"status": "degraded", "message": _last_error}

    _watcher = OpenClawFileHandler()
    _observer = Observer()
    # recursive=True: 监听所有子目录（日目录）
    _observer.schedule(_watcher, watch_dir, recursive=True)
    _observer.start()
    _last_error = None
    print(f"[文件监听] 已启动，递归监听: {watch_dir}")

    # 启动补扫：监听器只能捕获"启动之后"新建的文件事件，
    # 软件没运行期间写进来的文件（如 OpenClaw 离线采集的日目录）会被漏掉。
    # 因此启动时后台扫描一次已有文件，靠 process_file 的哈希去重保证幂等、不重复导入。
    _start_bootstrap_scan(watch_dir)

    return {"status": "running", "watch_dir": watch_dir}


def _start_bootstrap_scan(watch_dir: str):
    """后台补扫：启动时扫描监听目录中已存在但尚未入库的文件。

    用独立线程执行，不阻塞应用启动；scan_directory 内部有
    文件哈希 + 标题日期双重去重，重复扫描不会产生重复选题。
    """
    def _run():
        time.sleep(1)  # 等 observer 就绪后再扫，避免和实时事件竞争
        try:
            from app.services.topic_parser import scan_directory
            result = scan_directory(watch_dir)
            created = result.get("topics_created", 0)
            skipped = result.get("topics_skipped", 0)
            if created:
                print(f"[文件监听] 启动补扫完成：新增 {created} 条，跳过 {skipped} 个已存在文件")
            else:
                print(f"[文件监听] 启动补扫完成：无新增文件（已存在 {skipped} 个）")
        except Exception as e:
            print(f"[文件监听] 启动补扫失败: {e}")

    threading.Thread(target=_run, daemon=True).start()


def stop_watcher():
    """停止文件监听"""
    global _observer
    if _observer:
        _observer.stop()
        _observer.join()
        _observer = None
        print("[文件监听] 已停止")


def watcher_status() -> dict:
    """供健康检查使用，状态来自实际 observer，而不是 UI 猜测。"""
    watch_dir = get_setting("openclaw_watch_dir", "")
    if _observer and _observer.is_alive():
        return {"status": "running", "watch_dir": watch_dir}
    if not WATCHDOG_AVAILABLE:
        return {"status": "degraded", "message": "watchdog 未安装"}
    if not watch_dir or not os.path.isdir(watch_dir):
        return {"status": "degraded", "message": "OpenClaw 监听目录未配置或不存在", "watch_dir": watch_dir}
    return {"status": "stopped", "message": _last_error or "监听器未启动", "watch_dir": watch_dir}


def restart_watcher():
    """重启文件监听（配置变更后调用）"""
    stop_watcher()
    start_watcher()
