import html
import json
import os
import contextlib
import asyncio
import inspect
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Runtime identity is deployment-owned.  Forcing the Hugging Face profile here made
# Oracle VMs use Space-specific SQLite, queue, and resource settings even when the
# operator supplied an Oracle profile.
os.environ.setdefault("ROYELLS_KEEPALIVE_HTTP", "0")

import royells_media_bot_ready as bot


BOT_STATUS = {
    "started_at": "",
    "state": "not started",
    "last_error": "",
    "restart_count": 0,
}


def status_snapshot():
    architecture = bot.v20_health_snapshot()
    readiness = architecture.get("readiness") or {}
    heartbeat_age = max(
        0.0,
        time.monotonic() - float(getattr(bot, "EVENT_LOOP_HEARTBEAT_TS", 0.0) or 0.0),
    )
    return {
        "ok": bool(readiness.get("ok", architecture.get("ok", False))),
        "service": "royells-mirror-bot",
        "architecture": architecture,
        "state": BOT_STATUS.get("state") or "unknown",
        "started_at": BOT_STATUS.get("started_at") or "starting",
        "restart_count": int(BOT_STATUS.get("restart_count") or 0),
        "last_error": BOT_STATUS.get("last_error") or "",
        "loop_heartbeat_age_seconds": round(heartbeat_age, 3),
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
        if self.path not in (
            "/",
            "/health",
            "/ping",
            "/live",
            "/ready",
            "/startup",
            "/metrics",
        ):
            self.send_response(404)
            self.end_headers()
            return
        snapshot = status_snapshot()
        if self.path == "/":
            body = render_html(snapshot).encode("utf-8")
            content_type = "text/html; charset=utf-8"
            status = 200
        else:
            architecture = snapshot.get("architecture") or {}
            if self.path in ("/ping", "/live"):
                payload = dict(architecture.get("liveness") or {"ok": True})
                if self.path == "/live":
                    heartbeat_limit = max(
                        60,
                        int(getattr(bot, "HARD_WATCHDOG_STALL_SECONDS", 420)),
                    )
                    main_running = snapshot.get("state") in {"starting", "running"}
                    heartbeat_fresh = (
                        float(snapshot.get("loop_heartbeat_age_seconds") or 0.0)
                        <= heartbeat_limit
                    )
                    payload["main_running"] = main_running
                    payload["heartbeat_fresh"] = heartbeat_fresh
                    payload["heartbeat_age_seconds"] = snapshot.get(
                        "loop_heartbeat_age_seconds", 0.0
                    )
                    payload["ok"] = bool(payload.get("ok") and main_running and heartbeat_fresh)
            elif self.path == "/ready":
                payload = architecture.get("readiness") or {"ok": False}
            elif self.path == "/startup":
                payload = architecture.get("startup") or {"ok": False}
            elif self.path == "/metrics":
                payload = architecture.get("metrics") or snapshot.get("metrics") or {}
            else:
                payload = snapshot
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            content_type = "application/json; charset=utf-8"
            status = 200 if self.path in ("/ping", "/metrics") or bool(payload.get("ok")) else 503
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.send_response(405)
        self.end_headers()

    def log_message(self, fmt, *args):
        return


class BoundedStatusServer(ThreadingHTTPServer):
    """Small bounded status server that cannot create unbounded request threads."""

    daemon_threads = True
    request_queue_size = 16

    def __init__(self, server_address, handler_class, max_workers=4):
        self._request_slots = threading.BoundedSemaphore(max(1, int(max_workers)))
        super().__init__(server_address, handler_class)

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


def main():
    port = int(os.getenv("PORT", "7860"))
    bind_host = os.getenv("ROYELLS_STATUS_BIND", "0.0.0.0").strip() or "0.0.0.0"
    max_workers = max(1, min(16, int(os.getenv("ROYELLS_STATUS_MAX_WORKERS", "4"))))
    server = BoundedStatusServer((bind_host, port), RoyellsStatusHandler, max_workers=max_workers)
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
    try:
        bot.validate_build_manifest_before_startup()
    except bot.BuildManifestError as exc:
        BOT_STATUS["state"] = "build manifest failed"
        BOT_STATUS["last_error"] = str(exc)[:1200]
        print(str(exc), flush=True)
        raise
    status_thread = threading.Thread(target=main, name="royells-status-http", daemon=True)
    status_thread.start()
    bot.bootstrap_log("app.py wrapper reached; running bot in main thread")
    try:
        BOT_STATUS["state"] = "running"
        BOT_STATUS["last_error"] = ""
        bot.app.run(bot.main())
        exit_code = bot.shutdown_exit_code()
        lifecycle_requested = bool(
            bot.V20_SERVICES
            and bot.V20_SERVICES.lifecycle.requested()
        )
        if lifecycle_requested:
            BOT_STATUS["state"] = (
                "controlled restart" if exit_code else "stopped"
            )
            BOT_STATUS["last_error"] = ""
            if exit_code:
                raise SystemExit(exit_code)
            raise SystemExit(0)
        BOT_STATUS["state"] = "returned unexpectedly"
        BOT_STATUS["last_error"] = "Royells main loop returned without a shutdown request."
        print("Royells main loop returned unexpectedly.", flush=True)
        # The durable checkpoint/delivery-intent ledger owns crash recovery.  Exit
        # non-zero so Docker's restart policy can recover a dead main loop instead
        # of leaving only the status thread alive indefinitely.
        raise SystemExit(75)
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
        print(
            "Main loop failed; exiting with code 75 for durable Docker recovery.",
            flush=True,
        )
        raise SystemExit(75) from exc
