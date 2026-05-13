import time
import subprocess
import sys
import os
import shutil
import threading
import json
from urllib.parse import urlparse, parse_qs, unquote
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from watchdog.observers import Observer

from backend.utils import configure_stdio, log, resolve_workspace_root, safe_rel
from backend.metadata import (
    delete_paper, list_recycle_bin, restore_paper, purge_paper, purge_all_papers,
    save_metadata, update_paper, load_metadata, atomic_write_metadata,
)
from backend.speedread import generate_speedread, test_speedread_config
from backend.watcher import PDFHandler

WORKSPACE_ROOT = resolve_workspace_root()

# Paths resolved from the workspace root.
PDF_DIR = os.path.join(WORKSPACE_ROOT, "papers")
BUILD_SCRIPT = os.path.join(WORKSPACE_ROOT, "build.py")
METADATA_FILE = os.path.join(WORKSPACE_ROOT, "metadata.json")
METADATA_DEMO_FILE = os.path.join(WORKSPACE_ROOT, "metadata.demo.json")

# Bootstrap metadata.json from the tracked demo file on first run.
if not os.path.exists(METADATA_FILE) and os.path.exists(METADATA_DEMO_FILE):
    shutil.copy2(METADATA_DEMO_FILE, METADATA_FILE)

RECYCLE_DIR = os.path.join(WORKSPACE_ROOT, ".recycle_bin")
SPEEDREAD_CACHE_DIR = os.path.join(WORKSPACE_ROOT, ".speedread_cache")
LOG_FILE = os.path.join(WORKSPACE_ROOT, "waatchdog.log")

HTTP_PORT = 8000
SPEEDREAD_MAX_IMAGE_PAGES = 4
SPEEDREAD_IMAGE_WIDTH = 1400
SPEEDREAD_MAX_SOURCE_CHARS = 24000


def _bring_explorer_to_front(target_path: str) -> None:
    """Best-effort attempt to focus the Explorer window for target_path."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return

    user32 = ctypes.windll.user32
    target_dir = os.path.basename(os.path.dirname(target_path)) or os.path.basename(target_path)
    if not target_dir:
        return

    EnumWindows = user32.EnumWindows
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    GetWindowText = user32.GetWindowTextW
    GetWindowTextLength = user32.GetWindowTextLengthW
    IsWindowVisible = user32.IsWindowVisible
    GetClassName = user32.GetClassNameW

    found = []

    def callback(hwnd, _lparam):
        if not IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(256)
        GetClassName(hwnd, cls, 256)
        if cls.value not in ("CabinetWClass", "ExploreWClass"):
            return True
        length = GetWindowTextLength(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        GetWindowText(hwnd, buf, length + 1)
        if target_dir.lower() in buf.value.lower():
            found.append(hwnd)
            return False
        return True

    # Give Explorer a brief moment to create the target window.
    for _ in range(20):
        time.sleep(0.1)
        found.clear()
        EnumWindows(EnumWindowsProc(callback), 0)
        if found:
            hwnd = found[0]
            try:
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
                user32.BringWindowToTop(hwnd)
            except Exception:
                pass
            return


class PaperRequestHandler(SimpleHTTPRequestHandler):
    """Static file handler with extra local management APIs."""

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/open-folder":
            self.handle_open_folder(parse_qs(parsed.query))
            return
        if parsed.path == "/api/log":
            self.handle_getlog(parse_qs(parsed.query))
            return
        if parsed.path == "/api/recycle-list":
            self.handle_recycle_list()
            return
        if parsed.path == "/api/ping":
            self._send_json(200, {"ok": True, "service": "waatchdog"})
            return
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        body = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            self._send_json(400, {"ok": False, "error": "JSON 解析失败"})
            return

        if parsed.path == "/api/delete-paper":
            self.handle_delete_paper(payload)
            return
        if parsed.path == "/api/recycle-restore":
            self.handle_recycle_restore(payload)
            return
        if parsed.path == "/api/recycle-purge":
            self.handle_recycle_purge(payload)
            return
        if parsed.path == "/api/recycle-purge-all":
            self.handle_recycle_purge_all()
            return
        if parsed.path == "/api/save-metadata":
            self.handle_save_metadata(payload)
            return
        if parsed.path == "/api/update-paper":
            self.handle_update_paper(payload)
            return
        if parsed.path == "/api/generate-speedread":
            self.handle_generate_speedread(payload)
            return
        if parsed.path == "/api/test-speedread-config":
            self.handle_test_speedread_config(payload)
            return
        self._send_json(404, {"ok": False, "error": "未知接口"})

    # ------- recycle bin -------
    def handle_delete_paper(self, payload):
        file_key = (payload.get("file_key") or "").strip()
        result = delete_paper(file_key, WORKSPACE_ROOT, METADATA_FILE, RECYCLE_DIR)
        self._send_json(result.status, result.payload)

    def handle_recycle_list(self):
        result = list_recycle_bin(RECYCLE_DIR)
        self._send_json(result.status, result.payload)

    def handle_recycle_restore(self, payload):
        rid = payload.get("id")
        result = restore_paper(rid, WORKSPACE_ROOT, BUILD_SCRIPT, METADATA_FILE, RECYCLE_DIR)
        self._send_json(result.status, result.payload)

    def handle_recycle_purge(self, payload):
        rid = payload.get("id")
        result = purge_paper(rid, RECYCLE_DIR)
        self._send_json(result.status, result.payload)

    def handle_recycle_purge_all(self):
        result = purge_all_papers(RECYCLE_DIR)
        self._send_json(result.status, result.payload)

    # ------- metadata persistence -------
    def handle_save_metadata(self, payload):
        data = payload.get("data")
        result = save_metadata(data, METADATA_FILE)
        self._send_json(result.status, result.payload)

    def handle_update_paper(self, payload):
        file_key = (payload.get("file_key") or "").strip()
        fields = payload.get("fields")
        result = update_paper(file_key, fields, METADATA_FILE)
        self._send_json(result.status, result.payload)

    # ------- paper speed-read -------
    # ------- paper speed-read -------
    def handle_generate_speedread(self, payload):
        file_key = (payload.get("file_key") or "").strip()
        api_config = payload.get("apiConfig") or {}
        force = bool(payload.get("force"))
        result = generate_speedread(
            file_key, api_config, force,
            WORKSPACE_ROOT, METADATA_FILE, SPEEDREAD_CACHE_DIR,
            SPEEDREAD_MAX_IMAGE_PAGES, SPEEDREAD_IMAGE_WIDTH, SPEEDREAD_MAX_SOURCE_CHARS,
        )
        self._send_json(result.status, result.payload)

    def handle_test_speedread_config(self, payload):
        api_config = payload.get("apiConfig") or {}
        result = test_speedread_config(api_config, WORKSPACE_ROOT)
        self._send_json(result.status, result.payload)

    def handle_getlog(self, query):
        try:
            tail = int((query.get("tail") or ["500"])[0])
        except (TypeError, ValueError):
            tail = 500
        tail = max(1, min(tail, 5000))

        if not os.path.exists(LOG_FILE):
            self._send_json(200, {"ok": True, "path": LOG_FILE, "lines": [], "truncated": False})
            return
        try:
            with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": f"读取日志失败: {exc}"})
            return
        truncated = len(all_lines) > tail
        lines = all_lines[-tail:]
        self._send_json(200, {
            "ok": True,
            "path": LOG_FILE,
            "total": len(all_lines),
            "returned": len(lines),
            "truncated": truncated,
            "lines": [ln.rstrip("\n") for ln in lines],
        })

    def handle_open_folder(self, query):
        raw = (query.get("path") or [""])[0]
        rel_path = unquote(raw).replace("/", os.sep).replace("\\", os.sep)
        if not rel_path:
            self._send_json(400, {"ok": False, "error": "缺少 path 参数"})
            return

        # Prevent path traversal outside the workspace.
        abs_path = os.path.abspath(os.path.join(WORKSPACE_ROOT, rel_path))
        try:
            common = os.path.commonpath([WORKSPACE_ROOT, abs_path])
        except ValueError:
            common = ""
        if common != WORKSPACE_ROOT:
            self._send_json(403, {"ok": False, "error": "禁止访问工作区外的路径"})
            return

        if not os.path.exists(abs_path):
            self._send_json(404, {"ok": False, "error": f"路径不存在: {abs_path}"})
            return

        try:
            if sys.platform.startswith("win"):
                # Let the new process request foreground window focus.
                try:
                    import ctypes
                    ASFW_ANY = -1
                    ctypes.windll.user32.AllowSetForegroundWindow(ASFW_ANY)
                except Exception:
                    pass

                if os.path.isdir(abs_path):
                    os.startfile(abs_path)  # type: ignore[attr-defined]
                else:
                    # Open the parent folder and select the target file.
                    subprocess.Popen(
                        ["explorer", "/select,", abs_path],
                        close_fds=True,
                    )

                # Second pass: bring the matching Explorer window forward.
                threading.Thread(
                    target=_bring_explorer_to_front,
                    args=(abs_path,),
                    daemon=True,
                ).start()
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", abs_path] if os.path.isfile(abs_path) else ["open", abs_path])
            else:
                # Linux: try to reveal/select the file in the file manager.
                if os.path.isfile(abs_path):
                    # GNOME / Nautilus via org.freedesktop.FileManager1 DBus
                    try:
                        subprocess.Popen([
                            "dbus-send", "--print-reply",
                            "--dest=org.freedesktop.FileManager1",
                            "/org/freedesktop/FileManager1",
                            "org.freedesktop.FileManager1.ShowItems",
                            f"array:string:file://{abs_path}",
                            "string:openpaper",
                        ])
                    except FileNotFoundError:
                        pass
                    else:
                        self._send_json(200, {"ok": True, "path": abs_path})
                        return
                    # KDE / Dolphin
                    try:
                        subprocess.Popen(["dolphin", "--select", abs_path])
                    except FileNotFoundError:
                        pass
                    else:
                        self._send_json(200, {"ok": True, "path": abs_path})
                        return
                # Fallback: open parent directory
                target = abs_path if os.path.isdir(abs_path) else os.path.dirname(abs_path)
                subprocess.Popen(["xdg-open", target])
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": f"打开失败: {exc}"})
            return

        self._send_json(200, {"ok": True, "path": abs_path})

    def log_message(self, format, *args):  # 静默模式
        return

def main() -> None:
    """Start the HTTP server and file watcher."""
    configure_stdio()
    # Serve files from the repository root regardless of launch location.
    os.chdir(WORKSPACE_ROOT)

    # Parse an optional port override: python -m backend --port 8001
    port = HTTP_PORT
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg in ("--port", "-p") and i + 1 < len(argv):
            try:
                port = int(argv[i + 1])
            except ValueError:
                print(f"无效端口: {argv[i + 1]}，使用默认 {HTTP_PORT}")
                port = HTTP_PORT
            break

    # Start the HTTP server for static files and local APIs.
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), PaperRequestHandler)
    except OSError as exc:
        print(f"无法绑定 127.0.0.1:{port}: {exc}")
        if getattr(exc, "winerror", None) == 10048 or "Address already in use" in str(exc):
            print("   端口已被占用，可能是之前的 backend/server.py 仍在运行。")
            print("   解决办法:")
            if sys.platform == "win32":
                print("     1) 关闭旧服务: 在 PowerShell 执行:")
                print(f"        Get-NetTCPConnection -LocalPort {port} | Select OwningProcess")
                print("        Stop-Process -Id <PID> -Force")
            elif sys.platform == "darwin":
                print("     1) 关闭旧服务: 在 Terminal 执行:")
                print(f"        lsof -ti :{port} | xargs kill")
            else:
                print("     1) 关闭旧服务: 在 Terminal 执行:")
                print(f"        fuser -k {port}/tcp")
            print(f"     2) 或换端口启动: python -m backend --port {port + 1}")
        sys.exit(1)

    http_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    http_thread.start()
    log(f"HTTP 服务已启动: http://127.0.0.1:{port}")

    os.makedirs(PDF_DIR, exist_ok=True)

    event_handler = PDFHandler(
        build_callback=lambda: subprocess.run([sys.executable, BUILD_SCRIPT], check=False),
        workspace_root=WORKSPACE_ROOT,
    )
    observer = Observer()
    observer.schedule(event_handler, PDF_DIR, recursive=True)
    observer.start()
    log(f"监控已启动: {PDF_DIR}")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        observer.join()
        httpd.shutdown()
        httpd.server_close()
        log("监控和 HTTP 服务已停止")


if __name__ == "__main__":
    main()



