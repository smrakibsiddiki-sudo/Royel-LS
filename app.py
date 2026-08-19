import html
import json
import os
import contextlib
import asyncio
import inspect
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("ROYELLS_HUGGINGFACE_SPACE", "0")
os.environ.setdefault("ROYELLS_E2_MICRO_PROFILE", "1")
os.environ.setdefault("ROYELLS_KEEPALIVE_HTTP", "0")

import royells_media_bot_ready as bot


BOT_STATUS = {
    "started_at": "",
    "state": "not started",
    "last_error": "",
    "restart_count": 0,
}


def status_snapshot():
    return {
        "ok": True,
        "service": "royells-mirror-bot",
        "state": BOT_STATUS.get("state") or "unknown",
        "started_at": BOT_STATUS.get("started_at") or "starting",
        "restart_count": int(BOT_STATUS.get("restart_count") or 0),
        "last_error": BOT_STATUS.get("last_error") or "",
        "queue": {
            "download": bot.channel_download_queue.qsize(),
            "upload": bot.upload_queue.qsize(),
            "link": bot.link_process_queue.qsize(),
        },
        "guard": bot.last_source_guard.get("status", "starting"),
        "sync": bot.last_auto_sync.get("status", "waiting"),
        "uploaded": bot.auto_count,
        "metrics": bot.runtime_metrics_snapshot(),
        "time": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
    }


def render_html(snapshot):
    queue = snapshot["queue"]
    rows = [
        ("Bot state", snapshot["state"]),
        ("Started", snapshot["started_at"]),
        ("Restarts", snapshot["restart_count"]),
        ("Queue", f"D{queue['download']} U{queue['upload']} L{queue['link']}"),
        ("Guard", snapshot["guard"]),
        ("Sync", snapshot["sync"]),
        ("Uploaded", snapshot["uploaded"]),
        ("Loop lag", f"{snapshot['metrics'].get('event_loop_lag_seconds', 0)}s"),
        ("Peak lag", f"{snapshot['metrics'].get('event_loop_lag_peak_seconds', 0)}s"),
        ("Slow DB/API", f"{snapshot['metrics'].get('slow_db_operations', 0)}/{snapshot['metrics'].get('slow_api_operations', 0)}"),
        ("UTC", snapshot["time"]),
    ]
    body_rows = "\n".join(
        f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}</td></tr>" for k, v in rows
    )
    error = ""
    if snapshot.get("last_error"):
        error = f"<h2>Last error</h2><pre>{html.escape(snapshot['last_error'])}</pre>"
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="15">
  <title>Royells Bot</title>
  <style>
    body {{ font-family: system-ui, Arial, sans-serif; margin: 32px; color: #17212b; }}
    h1 {{ margin: 0 0 16px; }}
    table {{ border-collapse: collapse; min-width: min(560px, 100%); }}
    th, td {{ border-bottom: 1px solid #e5e7eb; padding: 10px 12px; text-align: left; }}
    th {{ width: 150px; color: #4b5563; }}
    pre {{ white-space: pre-wrap; background: #f8fafc; padding: 12px; border: 1px solid #e5e7eb; }}
  </style>
</head>
<body>
  <h1>Royells Telegram Mirror Bot</h1>
  <table>{body_rows}</table>
  {error}
  <p><a href="/health">Health JSON</a></p>
</body>
</html>"""


class RoyellsStatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/health", "/ping"):
            self.send_response(404)
            self.end_headers()
            return
        snapshot = status_snapshot()
        if self.path == "/":
            body = render_html(snapshot).encode("utf-8")
            content_type = "text/html; charset=utf-8"
        else:
            body = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.send_response(405)
        self.end_headers()

    def log_message(self, fmt, *args):
        return


def main():
    port = int(os.getenv("PORT", "7860"))
    server = ThreadingHTTPServer(("0.0.0.0", port), RoyellsStatusHandler)
    print(f"[ROYELLS APP] status server ready on port {port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def stop_client_safely(client):
    with contextlib.suppress(Exception):
        result = client.stop()
        if inspect.isawaitable(result):
            asyncio.run(result)


if __name__ == "__main__":
    BOT_STATUS["started_at"] = bot.format_display_datetime(seconds=True)
    BOT_STATUS["state"] = "starting"
    status_thread = threading.Thread(target=main, name="royells-status-http", daemon=True)
    status_thread.start()
    bot.bootstrap_log("app.py wrapper reached; running bot in main thread")
    try:
        BOT_STATUS["state"] = "running"
        BOT_STATUS["last_error"] = ""
        bot.app.run(bot.main())
        BOT_STATUS["state"] = "returned unexpectedly"
        BOT_STATUS["last_error"] = "Royells main loop returned without a shutdown request."
        print("Royells main loop returned unexpectedly.", flush=True)
        if not bot.request_automatic_process_restart(BOT_STATUS["last_error"]):
            BOT_STATUS["state"] = "restart circuit open"
            threading.Event().wait()
    except KeyboardInterrupt:
        BOT_STATUS["state"] = "stopped"
        print("Bot stopped by user.", flush=True)
    except Exception as exc:
        stop_client_safely(bot.app)
        stop_client_safely(bot.userbot)
        BOT_STATUS["state"] = "crashed"
        BOT_STATUS["last_error"] = repr(exc)[:1200]
        BOT_STATUS["restart_count"] = int(BOT_STATUS.get("restart_count") or 0) + 1
        print("ROYELLS BOT CRASH:", repr(exc), flush=True)
        if "AUTH_KEY_DUPLICATED" in repr(exc) or "AuthKeyDuplicated" in repr(exc):
            BOT_STATUS["state"] = "fatal session duplicate"
            print(
                "Fatal session duplicate detected. Stop every other process using this user session, "
                "generate a fresh session string, update the Secret, then Factory Rebuild.",
                flush=True,
            )
            threading.Event().wait()
        if not bot.request_automatic_process_restart(f"app main crashed: {repr(exc)[:400]}"):
            BOT_STATUS["state"] = "restart circuit open"
            print(
                "Automatic restart is disabled. Status server remains online; "
                "inspect /health and logs before a manual Factory Restart.",
                flush=True,
            )
            threading.Event().wait()
