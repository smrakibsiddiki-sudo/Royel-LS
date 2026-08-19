import ast
import asyncio
import contextlib
import hashlib
import random
import re
import threading
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from royells_v20_core.downloads import HybridDownloadError


ROOT = Path(__file__).resolve().parents[1]
BOT = (ROOT / "royells_media_bot_ready.py").read_text(encoding="utf-8")
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")


def load_bot_function(name, namespace=None):
    """Compile one pure/runtime helper without importing the production bot."""

    tree = ast.parse(BOT)
    node = next(
        item
        for item in tree.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == name
    )
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    scope = dict(namespace or {})
    exec(compile(module, str(ROOT / "royells_media_bot_ready.py"), "exec"), scope)
    return scope[name], scope


def test_process_restart_disabled_but_transport_recovery_enabled():
    assert 'TELEGRAM_TRANSPORT_RECOVERY_ENABLED = env_bool("ROYELLS_TELEGRAM_TRANSPORT_RECOVERY", True)' in BOT
    assert "AUTOMATIC_USERBOT_RECONNECT_ENABLED = TELEGRAM_TRANSPORT_RECOVERY_ENABLED" in BOT
    assert "Telegram transport recovery scheduled" in BOT
    assert "telegram transport recovery: {err_log[:180]}" in BOT
    assert "telegram transport recovery: {err_msg[:180]}" in BOT
    assert "process restart remains disabled" in BOT
    assert "ROYELLS_TELEGRAM_TRANSPORT_RECOVERY=1" in DOCKERFILE
    assert "Triggering userbot reconnect" not in BOT


def test_media_queues_are_bounded_while_descriptors_remain_durable():
    assert "MAIN_LOCAL_QUEUE_SOFT_LIMIT = 48" in BOT
    assert "MAIN_LOCAL_QUEUE_HARD_LIMIT = 64" in BOT
    assert "min(64, _download_queue_requested" in BOT
    assert "min(8, _upload_queue_requested" in BOT
    assert "maxsize=IN_MEMORY_QUEUE_MAX" in BOT
    assert "maxsize=UPLOAD_READY_QUEUE_MAX" in BOT
    assert "ROYELLS_IN_MEMORY_QUEUE_MAX=64" in DOCKERFILE
    assert "ROYELLS_UPLOAD_READY_QUEUE_MAX=8" in DOCKERFILE


def test_queue_recovery_owner_status_is_durable_coalesced_and_editable():
    """A queue-healer loop must edit one owner status, including after restart."""

    saved = []
    state = {"runtime_config": {"queue_recovery_notification": {}}}
    clock = SimpleNamespace(now=100.0)
    namespace = {
        "STATE": state,
        "state_mutex": threading.RLock(),
        "QUEUE_RECOVERY_NOTIFICATION_MAX_TRACKED_JOBS": 200,
        "QUEUE_RECOVERY_NOTIFICATION_REUSE_SECONDS": 86400,
        "QUEUE_RECOVERY_NOTIFICATION_EDIT_MIN_INTERVAL_SECONDS": 300,
        "time": SimpleNamespace(time=lambda: clock.now),
        "hashlib": hashlib,
        "now_iso": lambda: "2026-08-13T01:00:00Z",
        "save_state": saved.append,
        "OWNER_ID": 999,
    }
    event_key, namespace = load_bot_function(
        "queue_recovery_notification_event_key", namespace
    )
    state_locked, namespace = load_bot_function(
        "queue_recovery_notification_state_locked", namespace
    )
    formatter, namespace = load_bot_function(
        "format_queue_recovery_notification", namespace
    )
    prepare, namespace = load_bot_function(
        "prepare_queue_recovery_notification", namespace
    )
    finalize, namespace = load_bot_function(
        "finalize_queue_recovery_notification", namespace
    )

    first = prepare(["same-job"], 1, 0, 0, 1, now_epoch=clock.now)
    assert first["action"] == "send"
    assert "Queue recovery status" in first["text"]
    assert "Recovered 1 unfinished bot jobs after restart" not in first["text"]
    assert finalize(first, "sent", message_id=77) is True

    # Simulate a clean restart by retaining only the durable runtime-config map.
    state["runtime_config"] = deepcopy(state["runtime_config"])
    clock.now += 1
    repeated = prepare(["same-job"], 1, 0, 0, 1, now_epoch=clock.now)
    assert repeated["action"] == "dedupe"
    cursor = state["runtime_config"]["queue_recovery_notification"]
    assert cursor["message_id"] == 77
    assert cursor["coalesced_count"] == 1

    # A distinct recovered job is never suppressed: it edits the one status.
    clock.now += 1
    distinct = prepare(["different-job"], 1, 0, 0, 1, now_epoch=clock.now)
    assert distinct["action"] == "edit"
    assert distinct["message_chat_id"] == 999
    assert distinct["message_id"] == 77
    assert saved.count("runtime_config") >= 4
    assert event_key(["same-job"]) == event_key(["same-job"])
    assert state_locked()["tracked_job_ids"] == ["same-job", "different-job"]
    assert "Repeated recovery signals" in formatter(
        {"recovered": 1, "deferred": 0, "pending": 1}, 2, 1
    )


def test_queue_recovery_notification_keeps_ambiguous_send_intent_but_retries_definitive_rejection():
    failure_policy, _ = load_bot_function(
        "queue_recovery_notification_send_failure_is_definitive"
    )

    assert failure_policy(TimeoutError("gateway timed out")) is False
    assert failure_policy(RuntimeError("Forbidden: bot was blocked by the user")) is True
    assert "pending_send_token" in BOT
    assert "clear_pending=queue_recovery_notification_send_failure_is_definitive" in BOT
    assert "prevent a duplicate after restart" in BOT
    assert "ROYELLS_QUEUE_RECOVERY_NOTIFICATION_EDIT_MIN_INTERVAL_SECONDS=300" in DOCKERFILE
    assert "ROYELLS_QUEUE_RECOVERY_NOTIFICATION_REUSE_SECONDS=86400" in DOCKERFILE


def test_repeated_startup_and_healer_passes_dispatch_one_status_then_edit():
    plans = iter(
        (
            {"action": "send", "text": "initial"},
            {"action": "dedupe", "text": "same job"},
            {"action": "edit", "text": "new job"},
        )
    )
    delivered = []
    logs = []

    async def durable(_plan):
        return True

    async def deliver(plan):
        delivered.append(plan["action"])
        return "sent" if plan["action"] == "send" else "edited"

    notify, _ = load_bot_function(
        "notify_queue_recovery_summary",
        {
            "QUEUE_RECOVERY_NOTIFICATION_ENABLED": True,
            "OWNER_ID": 999,
            "queue_recovery_notification_lock": asyncio.Lock(),
            "prepare_queue_recovery_notification": lambda *args, **kwargs: next(plans),
            "queue_recovery_notification_plan_is_durable": durable,
            "deliver_queue_recovery_notification_plan": deliver,
            "log_event_rate_limited": lambda *args, **kwargs: logs.append(args),
            "QUEUE_RECOVERY_NOTIFICATION_EDIT_MIN_INTERVAL_SECONDS": 300,
        },
    )

    async def scenario():
        assert await notify(1, 0, 0, 1, ["job-1"]) is True  # startup
        assert await notify(1, 0, 0, 1, ["job-1"]) is False  # healer repeat
        assert await notify(1, 0, 0, 1, ["job-2"]) is True  # real new recovery

    asyncio.run(scenario())
    assert delivered == ["send", "edit"]
    assert logs and "coalesced" in logs[0][0]


def test_recovery_work_is_not_coupled_to_owner_notification_delivery():
    recovery = BOT.split("async def recover_queue_state", 1)[1].split(
        "async def channel_download_worker_loop", 1
    )[0]

    assert "recovered_job_ids.append(job_id)" in recovery
    assert "await notify_queue_recovery_summary(" in recovery
    assert "with contextlib.suppress(Exception):" in recovery
    assert 'f"Recovered {recovered} unfinished bot jobs after restart.' not in recovery
    assert "queue_limit=unlimited" not in recovery
    assert "queue_limits=download:{channel_download_queue.maxsize}" in recovery
    assert "unlimited media queue admission" not in BOT
    assert "It never\n    alters the recovered descriptor" in BOT


def test_source_scan_backpressure_is_enabled_before_bounded_queues_fill():
    enabled, _ = load_bot_function(
        "local_queue_limits_enabled",
        {
            "IN_MEMORY_QUEUE_MAX": 64,
            "UPLOAD_READY_QUEUE_MAX": 8,
        },
    )

    assert enabled() is True
    assert "ROYELLS_SCAN_PREFETCH_PRESSURE_TARGET=48" in DOCKERFILE
    assert "ROYELLS_MEDIA_ADMISSION_BACKPRESSURE_RETRY_SECONDS=10" in DOCKERFILE
    assert "SCAN_PREFETCH_PRESSURE_TARGET = max(" in BOT
    assert "return IN_MEMORY_QUEUE_MAX > 0 and UPLOAD_READY_QUEUE_MAX > 0" in BOT


def test_backpressured_media_admission_is_durable_and_resumes_without_releasing_keys():
    calls = []
    logs = []
    scheduled = []

    def record_download(job, status, error=None, files=None):
        calls.append(("download", job["job_id"], status, error, files))

    def record_total(job, status, error=None):
        calls.append(("total", job["job_id"], status, error))

    def record_sync(job, status, error=None):
        calls.append(("sync", job["job_id"], status, error))

    def schedule(queue_name, job, delay, reason):
        scheduled.append((queue_name, job["job_id"], delay, reason))
        return True

    retain, _ = load_bot_function(
        "retain_media_job_for_admission_backpressure",
        {
            "record_download_job": record_download,
            "record_total_job": record_total,
            "record_sync_item": record_sync,
            "schedule_queue_retry": schedule,
            "source_brain_record_messages": lambda messages, event, amount, title: calls.append(
                ("brain", len(messages), event, amount, title)
            ),
            "log_event_rate_limited": lambda *args, **kwargs: logs.append(args),
            "log_event": lambda text: logs.append((text,)),
            "MEDIA_ADMISSION_BACKPRESSURE_RETRY_SECONDS": 10,
        },
    )

    job = {"job_id": "pressure-job", "type": "single"}
    media = [object()]
    assert retain(job, media, "source", "admission timeout") is True
    assert calls[0] == ("download", "pressure-job", "retry_later", "admission timeout", [])
    assert ("total", "pressure-job", "retry_later", "admission timeout") in calls
    assert ("sync", "pressure-job", "retry_later", "admission timeout") in calls
    assert scheduled == [("download", "pressure-job", 10, "admission timeout")]
    assert ("brain", 1, "queued", 1, "source") in calls
    assert "remove_processing_keys" not in BOT.split(
        "def retain_media_job_for_admission_backpressure", 1
    )[1].split("async def enqueue_media_job", 1)[0]
    assert 'record_download_job(job, "retry_later", reason_text, files=[])' in BOT
    assert 'return True' in BOT.split(
        "def retain_media_job_for_admission_backpressure", 1
    )[1].split("async def enqueue_media_job", 1)[0]


def test_live_dashboard_is_flood_safe_by_default():
    assert 'LIVE_DASHBOARD_ENABLED = env_bool("ROYELLS_LIVE_DASHBOARD", False)' in BOT
    assert "Live dashboard stopped" in BOT
    assert "ROYELLS_LIVE_DASHBOARD=0" in DOCKERFILE
    assert "ROYELLS_LIVE_DASHBOARD_INTERVAL_SECONDS=300" in DOCKERFILE


def test_production_concurrency_defaults_are_stable():
    assert 'DEFAULT_DOWNLOAD_WORKERS = "1"' in BOT
    assert 'DEFAULT_UPLOAD_WORKERS = "1"' in BOT
    assert 'DEFAULT_API_CONCURRENCY = "1" if IS_HUGGINGFACE_SPACE else "2"' in BOT
    assert "ROYELLS_DOWNLOAD_WORKERS=1" in DOCKERFILE
    assert "ROYELLS_UPLOAD_WORKERS=1" in DOCKERFILE
    assert "ROYELLS_TELEGRAM_API_CONCURRENCY=1" in DOCKERFILE
    assert "telegram_download_semaphore = asyncio.Semaphore(TELEGRAM_DOWNLOAD_CONCURRENCY)" in BOT
    assert "if telegram_call_uses_download_lane(label)" in BOT
    assert "ROYELLS_TELEGRAM_DOWNLOAD_CONCURRENCY=2" in DOCKERFILE
    assert "ROYELLS_TELEGRAM_MEDIA_CONCURRENCY=1" in DOCKERFILE
    assert "ROYELLS_UPLOAD_ALBUM_CONCURRENCY=1" in DOCKERFILE
    assert "ROYELLS_MAX_CONCURRENT_TRANSMISSIONS=2" in DOCKERFILE
    assert "DOWNLOAD_WORKERS = 1" in BOT
    assert "UPLOAD_WORKERS = 1" in BOT
    assert "ADAPTIVE_UPLOAD_WORKERS_MAX = 1" in BOT
    assert "TELEGRAM_API_CONCURRENCY = 1" in BOT
    assert "TELEGRAM_MEDIA_CONCURRENCY = 1" in BOT
    assert "MAX_CONCURRENT_TRANSMISSIONS = 2" in BOT


def test_download_lane_and_flood_wait_helpers_execute_behaviorally():
    uses_download_lane, _ = load_bot_function("telegram_call_uses_download_lane")
    should_wait, _ = load_bot_function("should_wait_for_internal_flood_retry")

    assert uses_download_lane("hybrid userbot download media") is True
    assert uses_download_lane("download media") is True
    assert uses_download_lane("source get_messages") is False
    assert should_wait(1, 2, "userbot") is True
    assert should_wait(2, 2, "userbot") is False
    assert should_wait(1, 5, "bot") is False
    assert "if not should_wait_for_internal_flood_retry(attempt, retries, client_role):" in BOT
    assert "delay = max(1.0, telegram_flood_gate_remaining_seconds(flood_role))" in BOT


def test_flood_wait_is_recorded_once_and_reuses_original_deadline():
    clock = SimpleNamespace(now=100.0)
    metric_calls = []
    dirty_calls = []
    namespace = {
        "TELEGRAM_FLOOD_UNTIL": 0.0,
        "TELEGRAM_FLOOD_UNTIL_BY_ROLE": {
            "bot": 0.0,
            "userbot": 0.0,
            "generic": 0.0,
        },
        "telegram_client_role": lambda label="", client_role=None: str(
            client_role or "generic"
        ),
        "time": SimpleNamespace(monotonic=lambda: clock.now),
        "re": re,
        "flood_wait_delay": lambda seconds: float(seconds) + 5.0,
        "metric_increment": metric_calls.append,
        "contextlib": contextlib,
        "mark_runtime_checkpoint_dirty": dirty_calls.append,
    }
    note_flood_wait, scope = load_bot_function("note_global_flood_wait", namespace)

    error = SimpleNamespace(value=10)
    assert note_flood_wait(error, "userbot") == 15.0
    original_deadline = error._royells_flood_deadline
    clock.now += 4.0
    assert note_flood_wait(error, "userbot") == 11.0

    assert error._royells_flood_recorded is True
    assert error._royells_client_role == "userbot"
    assert error._royells_flood_deadline == original_deadline
    assert metric_calls == ["flood_wait_events"]
    assert dirty_calls == ["telegram flood wait"]
    assert scope["TELEGRAM_FLOOD_UNTIL_BY_ROLE"]["userbot"] == original_deadline


def test_long_flood_wait_is_never_shortened_by_local_cap():
    delay, _ = load_bot_function(
        "flood_wait_delay",
        {
            "random": random,
            "FLOOD_WAIT_JITTER_SECONDS": 0.0,
            "FLOOD_WAIT_BACKOFF_MULTIPLIER": 1.5,
            "FLOOD_WAIT_BACKOFF_CAP_SECONDS": 900,
        },
    )

    assert delay(1200) == 1200
    assert delay(100) == 150


def test_diagnostic_log_bridge_ignores_retry_and_deferral_telemetry():
    actionable, _ = load_bot_function(
        "diagnostic_log_event_is_actionable",
        {"re": re},
    )

    assert actionable("[UP ERROR W1] unhandled upload failure") is True
    assert actionable("FATAL: canonical target index unavailable") is True
    assert actionable("[UP ERROR W1] Timeout; retrying source job") is False
    assert actionable("Invalid media detected (1/2); retrying source item") is False
    assert actionable("Source deferred after recoverable timeout") is False
    assert 'diagnostic_increment_error(str(text), context="log_event")' in BOT


def test_daily_report_records_actual_build_runtime_and_intake_identity():
    assert 'f"Build: {build_identity} | number {build_number} | manifest {manifest_status}"' in BOT
    assert 'f"Runtime: {runtime_identity}"' in BOT
    assert '"adaptive enabled: cursor-based, sequential, pressure-aware "' in BOT
    assert 'f"Pipeline phase: {str(PIPELINE_SCAN_STATUS.get(\'phase\') or \'boot\')}"' in BOT


def test_target_index_prunes_ui_ledger_only_after_a_verified_full_scan():
    target_index = BOT.split("async def build_target_media_index", 1)[1].split(
        "async def target_media_index_loop", 1
    )[0]
    duplicate_memory = BOT.split("def known_duplicate_uid_memory", 1)[1].split(
        "def load_dead_media_from_state", 1
    )[0]

    assert "history_limited = bool(max_history and scanned > max_history)" in target_index
    assert "await run_blocking(\"db\", merge_target_index_db, seen_uids)" in target_index
    assert "if not history_limited:" in target_index
    assert '"action": "full_target_index_reconcile"' in target_index
    assert "canonical target and duplicate ledgers were preserved" in target_index
    assert 'STATE["clean_duplicate"]' not in duplicate_memory


def test_validator_repair_cooldown_cannot_complete_the_source():
    policy, _ = load_bot_function("startup_cooldown_can_complete_source")

    assert policy(0) is False
    assert policy(None) is False
    assert policy(1) is False
    assert policy(31) is False


def test_download_route_cache_skips_repeated_bot_api_probe():
    assert "download_route_cache = {}" in BOT
    assert "def preferred_download_route(message):" in BOT
    assert "BOT_API_DOWNLOAD_ENABLED = False" in BOT
    assert 'strategy="userbot_only"' in BOT
    assert 'metadata={"bot_api_download": "disabled"}' in BOT
    assert "strategy=\"userbot_cached\"" in BOT
    assert "remember_download_route(message, \"userbot\")" in BOT


def test_restart_apply_does_not_shutdown_process():
    assert "Runtime settings saved; automatic restart remains disabled." in BOT
    assert 'request_graceful_shutdown(\n            "owner restart apply"' not in BOT


def test_stalled_upload_worker_is_cancelled_and_requeued():
    assert 'min(3600, int(os.getenv("ROYELLS_WORKER_STALL_SECONDS", "1800")))' in BOT
    assert "WORKER_STALL_SECONDS = max(" in BOT
    assert "1800," in BOT
    assert "ROYELLS_WORKER_STALL_SECONDS=1800" in DOCKERFILE
    assert 'elif worker_name.startswith("UP W"):' in BOT
    assert 'queue_name = "upload"' in BOT
    assert "upload worker job stalled for more than {WORKER_STALL_SECONDS}s" in BOT
    assert "Stalled upload cancelled safely. Job retained for automatic retry." in BOT
    assert "delivery intent recovery will verify target before retry" in BOT


def test_adaptive_media_worker_scaler_is_enabled():
    assert 'ADAPTIVE_MEDIA_WORKERS_ENABLED = env_bool("ROYELLS_ADAPTIVE_MEDIA_WORKERS", True)' in BOT
    assert "async def adaptive_media_worker_scaler_loop():" in BOT
    assert '"adaptive_media_worker_scaler"' in BOT
    assert "Adaptive media worker scaler started" in BOT
    assert "Adaptive media worker scaler stopped idle" in BOT
    assert "telegram_flood_gate_remaining_seconds()" in BOT
    assert "holding base capacity while Telegram FloodWait gate is active" in BOT
    assert "ROYELLS_ADAPTIVE_MEDIA_WORKERS=1" in DOCKERFILE
    assert "ROYELLS_ADAPTIVE_DOWNLOAD_WORKERS_MAX=2" in DOCKERFILE
    assert "ROYELLS_ADAPTIVE_UPLOAD_WORKERS_MAX=1" in DOCKERFILE
    assert "ROYELLS_ADAPTIVE_DOWNLOAD_QUEUE_PER_WORKER=4" in DOCKERFILE
    assert "ROYELLS_ADAPTIVE_WORKER_LOW_WATERMARK=2" in DOCKERFILE


def test_saved_runtime_settings_cannot_reenable_unsafe_parallelism():
    limits, scope = load_bot_function("runtime_worker_limits")
    clamp, _ = load_bot_function("clamp_runtime_worker_value", {"runtime_worker_limits": limits})

    assert limits() == {"download": 1, "upload": 1, "link": 1, "button": 2}
    assert clamp("download", 99) == 1
    assert clamp("upload", 3) == 1
    assert clamp("button", 99) == 2
    assert "media_queue_limits=download:{IN_MEMORY_QUEUE_MAX}/upload:{UPLOAD_READY_QUEUE_MAX}" in BOT


def test_startup_deep_scan_and_boot_download_purge_are_enabled():
    assert 'env_int("ROYELLS_STARTUP_HOT_SCAN_LIMIT", "40" if IS_HUGGINGFACE_SPACE else "100")' in BOT
    assert '"ROYELLS_STARTUP_HOT_SCAN_HISTORY_LIMIT"' in BOT
    assert 'STARTUP_DEEP_SCAN_EVERY_BOOT = env_bool("ROYELLS_STARTUP_DEEP_SCAN_EVERY_BOOT", False)' in BOT
    assert "or (BOOT_ID if STARTUP_DEEP_SCAN_EVERY_BOOT else \"\")" in BOT
    assert 'ADAPTIVE_INTAKE_TIMEOUT_SECONDS = max(120, env_int("ROYELLS_ADAPTIVE_INTAKE_TIMEOUT_SECONDS", "900"))' in BOT
    assert 'PURGE_DOWNLOADS_ON_BOOT = env_bool("ROYELLS_PURGE_DOWNLOADS_ON_BOOT", True)' in BOT
    assert "def purge_download_root_on_boot(protected_paths=()):" in BOT
    assert "Startup download purge removed" in BOT
    assert "recovery_paths = v21_existing_recovery_file_paths(checkpoint_payload)" in BOT
    assert "protected_paths=tuple(important_runtime_paths()) + tuple(recovery_paths)" in BOT
    assert '"startup_deep_scan"' in BOT
    assert "ROYELLS_STARTUP_DEEP_SCAN_EVERY_BOOT=0" in DOCKERFILE
    assert "ROYELLS_STARTUP_HOT_SCAN_LIMIT=40" in DOCKERFILE
    assert "ROYELLS_STARTUP_HOT_SCAN_HISTORY_LIMIT=200" in DOCKERFILE
    assert "ROYELLS_ADAPTIVE_INTAKE_TIMEOUT_SECONDS=900" in DOCKERFILE
    assert "ROYELLS_PURGE_DOWNLOADS_ON_BOOT=1" in DOCKERFILE


def test_dashboard_hides_target_mode_live_profile_and_hot_scan_label():
    assert 'if line.startswith(("Live ", "Mode ", "Target ", "Profile ")):' in BOT
    assert 'f"DeepScan {PIPELINE_SCAN_STATUS.get(' in BOT
    assert 'f"Archive  {PIPELINE_SCAN_STATUS.get(' in BOT
    assert 'line = line.replace(" live |", " active |")' in BOT
    assert '"Main bot only"' in BOT
    assert ("Hot" + "500") not in BOT
    assert '"Mode    main bot only"' not in BOT
    assert '"Mode    main"' not in BOT
    assert 'f"Target  {TARGET_CHAT_ID or ' not in BOT


def test_caption_preserving_fast_copy_falls_back_without_deferring_source_scan():
    assert 'copyMessage preserved caption; using captionless upload fallback' in BOT
    assert 'copy_media_group preserved a caption; using captionless upload fallback' in BOT
    assert 'RuntimeError(\n                "copy_media_group preserved a caption; using captionless upload fallback"' not in BOT
    assert 'raise RuntimeError("copyMessage preserved caption; falling back to captionless upload")' not in BOT
    assert 'raise RuntimeError("copyMessage preserved caption in album item")' not in BOT
    assert 'return False\n        await update_delivery_intent(\n            intent_id,\n            status="accepted",' in BOT
    assert "auto_count += len(copied)" in BOT


def test_cleanup_protects_queued_upload_files_and_rehydrates_missing_uploads():
    assert "def queued_jobs(self):" in BOT
    assert "def protected_download_file_paths():" in BOT
    assert "for job in queue_obj.queued_jobs():" in BOT
    assert "for entry in list(retry_admission_queue._queue):" in BOT
    assert "if str(resolved) in protected_paths:" in BOT
    assert "if path in protected_download_file_paths():" in BOT
    assert "def is_upload_source_file_missing_error(exc):" in BOT
    assert 'record_upload_job(job, "retry_download", e)' in BOT
    assert 'schedule_queue_retry(\n                    "download",' in BOT
    assert "Fresh download retry scheduled" in BOT


def test_file_part_missing_rehydrates_download_instead_of_upload_loop():
    assert "UPLOAD_FILE_PART_REUPLOADS = 0" in BOT
    assert "Reuploading complete album from byte zero" not in BOT
    assert "Upload session invalidated; fresh download retry required" in BOT
    assert 'except UploadSessionRecoveryError as e:' in BOT
    branch = BOT.split('except UploadSessionRecoveryError as e:', 1)[1].split('except Exception as e:', 1)[0]
    assert "FILE_PART_X_MISSING; fresh download required" in BOT
    assert 'record_upload_job(job, "retry_download", e)' in branch
    assert 'record_total_job(retry_job, "queued_download", e)' in branch
    assert "schedule_queue_retry(" in branch
    assert '"download"' in branch
    assert 'f"Delayed fresh upload scheduled in {delay}s."' not in branch
    assert 'schedule_queue_retry("upload", job, delay, e)' not in branch


def test_boot_cleanup_preserves_only_recoverable_checkpoint_spool_files(tmp_path):
    """A restart must not turn a staged upload into a missing-local-file retry."""

    spool = tmp_path / "downloads"
    spool.mkdir()
    keep = spool / "album" / "live.mp4"
    keep.parent.mkdir()
    keep.write_bytes(b"live media")
    kept_temp = spool / "album" / "live.mp4.temp"
    kept_temp.write_bytes(b"active transfer")
    stale = spool / "orphan.temp"
    stale.write_bytes(b"stale")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")

    records = {
        "upload_queue": {
            "items": {
                "active": {"status": "queued_upload", "files": [str(keep)]},
                "done": {"status": "uploaded", "files": [str(stale)]},
            }
        },
        "download_queue": {
            "items": {
                "partial": {
                    "status": "retry",
                    "_partial_download_files": [str(keep)],
                }
            }
        },
    }
    recovery_paths, _ = load_bot_function(
        "v21_existing_recovery_file_paths",
        {
            "contextlib": contextlib,
            "state_mutex": contextlib.nullcontext(),
            "deepcopy": deepcopy,
            "STATE": records,
        },
    )
    protected = recovery_paths(
        {"temp_files": [{"path": str(kept_temp)}, {"path": str(outside)}]}
    )
    assert str(keep) in protected
    assert str(kept_temp) in protected
    assert str(outside) in protected
    assert str(stale) not in protected

    purge, _ = load_bot_function(
        "purge_download_root_on_boot",
        {
            "Path": Path,
            "contextlib": contextlib,
            "DOWNLOAD_DIR": spool,
            "DATA_DIR": tmp_path / "data",
            "RUNTIME_DIR": tmp_path / "runtime",
            "STATE_DIR": tmp_path / "state",
            "SESSION_DIR": tmp_path / "sessions",
            "log_event": lambda _message: None,
            "format_exception_for_log": lambda exc: str(exc),
        },
    )
    result = purge(protected_paths=protected)
    assert keep.exists()
    assert kept_temp.exists()
    assert not stale.exists()
    assert outside.exists()
    assert result["protected"] == 2  # the external checkpoint path is ignored safely


def test_startup_cleanup_passes_recovery_paths_to_both_cleanup_stages():
    calls = []

    class CleanupEngine:
        def startup_sweep(self, *, protected_paths):
            calls.append(("sweep", tuple(protected_paths)))
            return {"deleted": 1, "freed": 2, "skipped": 3, "protected": 4}

    run_cleanup, _ = load_bot_function(
        "run_v21_startup_cleanup",
        {
            "PURGE_DOWNLOADS_ON_BOOT": True,
            "V21_ENGINE": SimpleNamespace(cleanup_engine=CleanupEngine()),
            "V21_CLEANUP_STALE_TEMP_ON_BOOT": True,
            "v21_existing_recovery_file_paths": lambda _payload: {"/spool/live.mp4"},
            "purge_download_root_on_boot": lambda *, protected_paths: calls.append(
                ("purge", tuple(protected_paths))
            )
            or {"deleted": 5, "freed": 6, "skipped": 7, "protected": 8},
            "important_runtime_paths": lambda: ("/runtime/state.json",),
        },
    )
    result = run_cleanup({"temp_files": []})
    assert ("purge", ("/spool/live.mp4",)) in calls
    assert ("sweep", ("/runtime/state.json", "/spool/live.mp4")) in calls
    assert result == {"deleted": 6, "freed": 8, "skipped": 10, "protected": 12}


def test_file_part_recovery_keeps_file_free_retry_when_local_artifact_is_gone():
    metrics = []
    logs = []

    async def run_blocking(_pool, func, *args):
        return func(*args)

    async def missing_fingerprint(*_args, **_kwargs):
        raise FileNotFoundError("upload source file missing: /spool/vanished.mp4")

    reset, _ = load_bot_function(
        "reset_upload_session_state",
        {
            "metric_increment": lambda name: metrics.append(name),
            "run_blocking": run_blocking,
            "_discard_upload_sidecars": lambda _files: 0,
            "ensure_upload_file_fingerprints": missing_fingerprint,
            "format_exception_for_log": lambda exc: f"{type(exc).__name__}: {exc}",
            "log_event": logs.append,
        },
    )
    job = {
        "_uploaded_media": object(),
        "_input_media": object(),
        "_upload_cache": {"old": True},
        "_upload_session": object(),
        "_upload_state": {"part": 12},
        "_upload_handles": [object()],
        "_upload_file_fingerprints": {"/spool/vanished.mp4": {"size": 1}},
    }
    asyncio.run(reset(job, ["/spool/vanished.mp4"], RuntimeError("FILE_PART_X_MISSING")))

    assert job["_upload_session_generation"] == 1
    assert "_upload_file_fingerprints" not in job
    assert "_upload_handles" not in job
    assert "vanished.mp4" in job["_upload_recovery_local_artifact_error"]
    assert "upload_recovery_local_artifact_unavailable" in metrics
    assert any("fresh download retry required" in message for message in logs)


def test_startup_deep_scan_respects_source_cooldown():
    assert "if not guard_next_check_allowed(channel_id):" in BOT
    assert "startup_deep_scan_cooldown:{channel_id}" in BOT
    assert "source peer is cooling down after a recoverable resolve error" in BOT
    assert "def startup_hot_source_retry_at(channel_id, minimum_delay=60):" in BOT
    assert '"deferred_sources"' in BOT
    assert "Startup source initial pass released adaptive intake" in BOT
    assert "success=startup_cooldown_can_complete_source(repair_count)" not in BOT


def test_peer_recovery_does_not_share_cache_and_candidate_cooldowns():
    assert "SOURCE_PEER_CANDIDATE_NEXT_AT = {}" in BOT
    assert "def source_peer_candidate_resolve_allowed(channel_id, force=False):" in BOT
    assert "def note_source_peer_candidate_resolve_attempt(channel_id):" in BOT
    assert "SOURCE_DIALOG_SCAN_ON_CACHE_MISS and source_peer_resolve_allowed(norm)" in BOT
    assert "can_try_candidate = source_peer_candidate_resolve_allowed(channel_id)" in BOT
    assert "note_source_peer_candidate_resolve_attempt(channel_id)" in BOT
    assert "ROYELLS_SOURCE_DIALOG_SCAN_ON_CACHE_MISS=1" in DOCKERFILE
    assert "ROYELLS_CHANNEL_PEER_INVALID_BACKOFF_SECONDS=300" in DOCKERFILE


def test_legacy_peer_cooldown_is_normalized_only_when_it_cannot_be_current():
    clock = SimpleNamespace(now=10_000.0)

    def parse_timestamp(value):
        if value in (None, ""):
            return None
        return SimpleNamespace(timestamp=lambda: float(value))

    needs_normalization, _ = load_bot_function(
        "legacy_peer_backoff_needs_normalization",
        {
            "time": SimpleNamespace(time=lambda: clock.now),
            "parse_iso_timestamp": parse_timestamp,
            "CHANNEL_PEER_INVALID_BACKOFF_MAX_ATTEMPTS": 6,
        },
    )

    # Counts above six were emitted by the old uncapped writer, even if the
    # process restarted recently. They must be repaired immediately.
    assert needs_normalization(
        {"peer_invalid_count": 916, "next_check_after": clock.now + 21_600}
    ) is True
    # A fresh timestamped cursor is treated as an intentional modern policy,
    # rather than being shortened just because its deadline is long.
    assert needs_normalization(
        {
            "peer_invalid_count": 3,
            "next_check_after": clock.now + 21_600,
            "peer_invalid_updated_at": str(clock.now - 30),
        }
    ) is False
    assert needs_normalization(
        {
            "peer_invalid_count": 3,
            "next_check_after": clock.now + 21_600,
            "peer_invalid_updated_at": str(clock.now - 3_601),
        }
    ) is True

    state = {
        "sync_source_manager": {
            "cursors": {
                "-1001": {
                    "peer_invalid_count": 916,
                    "next_check_after": clock.now + 21_600,
                }
            }
        }
    }
    allowed, _ = load_bot_function(
        "guard_next_check_allowed",
        {
            "guard_key": lambda value: str(value),
            "state_mutex": contextlib.nullcontext(),
            "STATE": state,
            "time": SimpleNamespace(time=lambda: clock.now),
            "CHANNEL_PEER_INVALID_BACKOFF_MAX_ATTEMPTS": 6,
            "legacy_peer_backoff_needs_normalization": needs_normalization,
            "save_state": lambda _name: None,
            "now_iso": lambda: "2026-08-12T00:00:00",
            "log_event_rate_limited": lambda *args, **kwargs: None,
        },
    )

    assert allowed(-1001) is False
    cursor = state["sync_source_manager"]["cursors"]["-1001"]
    assert cursor["peer_invalid_count"] == 6
    assert cursor["next_check_after"] == clock.now + 1_800
    clock.now += 1_801
    assert allowed(-1001) is True


def test_source_add_resolve_is_retry_friendly_and_longer_lived():
    assert 'ADD_CHANNEL_RESOLVE_TIMEOUT_SECONDS = max(60, int(os.getenv("ROYELLS_ADD_CHANNEL_RESOLVE_TIMEOUT_SECONDS", "180")))' in BOT
    assert 'env_int("ROYELLS_BULK_SOURCE_RESOLVE_TIMEOUT_SECONDS", "180")' in BOT
    assert "Send the same channel link/username again here; Add Channel action is still open." in BOT
    assert "Source add saved unresolved identifier" in BOT
    assert "ROYELLS_ADD_CHANNEL_RESOLVE_TIMEOUT_SECONDS=180" in DOCKERFILE
    assert "ROYELLS_BULK_SOURCE_RESOLVE_TIMEOUT_SECONDS=180" in DOCKERFILE


def test_ffmpeg_conversion_and_dependency_are_removed():
    assert '"ffmpeg"' not in BOT
    assert "FFMPEG" not in BOT
    assert '"ffprobe"' not in BOT
    assert "subprocess.run(" not in BOT
    assert "apt-get install -y --no-install-recommends ca-certificates" in DOCKERFILE
    assert "ffmpeg" not in DOCKERFILE.lower()
    assert "system_libraries\": [\"ca-certificates\"]" not in BOT


def test_huggingface_sleep_prevention_self_ping_enabled():
    assert 'if env_bool("ROYELLS_KEEPALIVE_SELF_PING", True):' in BOT
    assert 'KEEPALIVE_PUBLIC_SELF_PING = env_bool("ROYELLS_KEEPALIVE_PUBLIC_SELF_PING", False)' in BOT
    assert "Keepalive ping {url}: 429; backing off" in BOT
    assert "SPACE_HOST" in BOT
    assert "SPACE_ID" in BOT
    assert "ROYELLS_KEEPALIVE_SELF_PING=1" in DOCKERFILE
    assert "ROYELLS_KEEPALIVE_PUBLIC_SELF_PING=0" in DOCKERFILE


def test_scan_album_prefetch_is_cached_and_throttled():
    assert "async def fetch_scan_media_group" in BOT
    assert "MEDIA_GROUP_FETCH_CACHE" in BOT
    assert "SCAN_ALBUM_PREFETCH_MIN_INTERVAL_SECONDS" in BOT
    assert "fetch_scan_media_group(\"sync get_media_group\", msg)" in BOT
    assert "fetch_scan_media_group(\"guard get_media_group\", msg)" in BOT
    assert "fetch_scan_media_group(\"historical get_media_group\", msg, retries=2)" in BOT


def test_video_metadata_is_advisory_and_validation_cannot_fall_through_to_zero_byte():
    workers = (ROOT / "royells_v21_micro_workers.py").read_text(encoding="utf-8")
    assert 'metadata["dimension_source"] = "telegram_source"' in workers
    assert 'metadata["dimension_source"] = "unavailable"' in workers
    assert '"ok_metadata_unknown"' in workers
    assert "downloaded file size mismatch" in workers
    assert 'result_code = "corrupt_container"' not in workers
    assert "video_metadata_loader=None" in BOT
    assert "video metadata missing dimensions" not in workers
    assert "never turn it into a delivery veto" in workers
    assert "A non-empty validation failure is not an empty download" in BOT
    assert "Media validation failed (" in BOT
    assert "Media download unavailable (" in BOT
    assert "0-byte media quarantined after bounded item retries" not in BOT
    assert "0-byte media detected (" not in BOT


def test_legacy_false_dead_media_is_released_and_affected_sources_are_rescanned():
    assert "def release_legacy_false_zero_byte_quarantine():" in BOT
    assert 'reason.startswith("0-byte media terminal after")' in BOT
    assert "LEGACY_FALSE_ZERO_BYTE_MIGRATION_ID" in BOT
    assert 'dead_state.setdefault("migrations", {})' in BOT
    assert 'scan_state.setdefault("validator_repair_sources", {})' in BOT
    assert "def complete_validator_repair_source(channel_id):" in BOT
    assert "Validator repair rescan active" in BOT
    assert "affected startup source scans were reopened" in BOT
    # The unavailable-item path owns a durable retry now.  Keep the guard
    # scoped to that terminal item decision rather than falling through to a
    # later, unrelated force-dead branch in the worker.
    assert "Media item deferred for future source rescan after bounded retries" not in BOT
    terminal_block = BOT.split(
        "Media item retained for durable unavailable-source retry:", 1
    )[0].rsplit("terminal_error = RuntimeError", 1)[1]
    assert "force_dead=True" not in terminal_block
    assert "remember_dead_media([m], terminal_error" not in terminal_block


def test_transport_and_validation_outcomes_cannot_poison_dead_uids():
    permanent = BOT.split("def is_permanent_dead_media_error(exc):", 1)[1].split(
        "def should_skip_dead_media_now", 1
    )[0]
    assert "Only explicit source-side absence" in permanent
    assert "return bool(exc.permanent)" not in permanent
    assert "HybridDownloadError) and exc.permanent" not in BOT.split(
        "def should_skip_dead_media_now", 1
    )[1].split("def is_session_auth_error", 1)[0]


def test_small_photo_threshold_and_validation_fingerprint_are_shared_end_to_end():
    workers = (ROOT / "royells_v21_micro_workers.py").read_text(encoding="utf-8")
    assert "def minimum_complete_size(media_kind: str)" in workers
    assert "def minimum_complete_media_size(message=None, media_kind=\"\")" in BOT
    assert "media_path_is_complete_enough(message, path)" in BOT
    assert 'job.setdefault("_upload_file_fingerprints", {})[str(path)]' in BOT
    assert "if observed_size > 1024:" not in BOT


def test_local_media_failures_never_force_dead_and_partial_album_is_not_published():
    preparation = BOT.split("async def prepare_source_upload_items", 1)[1].split(
        "async def build_source_media_group", 1
    )[0]
    fallback = BOT.split("async def send_source_items_individually", 1)[1].split(
        "# External platform cookie/session management removed.", 1
    )[0]
    assert "force_dead=True" not in preparation
    assert "force_dead=True" not in fallback
    assert "individual album item needs fresh download" in fallback
    assert "Media job retained for durable unavailable-source retry" in BOT
    assert "never publish a partial album" in BOT
    assert "ROYELLS_SOURCE_PERMANENT_RETRY=1" in DOCKERFILE


def test_disk_and_manifest_observability_fail_closed():
    assert "sane_disk_snapshot(DATA_DIR)" in BOT
    assert "virtual filesystem capacity is untrusted" in BOT
    assert "repair_missing=False" in BOT
    assert '"build_identity": {' in BOT
    assert "Restore 90%: rebuilding target index before source admission resumes." in BOT


def test_active_media_rpc_heartbeat_protects_only_bounded_transfer_window():
    has_active_rpc, _ = load_bot_function(
        "worker_has_active_bounded_media_rpc",
        {
            "time": SimpleNamespace(time=lambda: 1_000.0),
            "MEDIA_RPC_HARD_TIMEOUT_SECONDS": 900,
            "WORKER_MEDIA_TIMEOUT_MARGIN_SECONDS": 300,
        },
    )
    effective_progress, _ = load_bot_function(
        "worker_effective_progress_at",
        {
            "time": SimpleNamespace(time=lambda: 1_000.0),
            "MEDIA_RPC_HARD_TIMEOUT_SECONDS": 900,
        },
    )

    item = {
        "active_media_rpc": "download media item 1/1",
        "active_media_rpc_started_at": 100.0,
        "active_media_rpc_deadline_at": 1_000.0,
        "last_progress_at": 999.0,
    }
    assert has_active_rpc(item, now_ts=1_000.0) is True
    assert has_active_rpc(item, now_ts=1_001.0) is False
    assert effective_progress(item, now_ts=999.0) == 999.0
    assert effective_progress(item, now_ts=1_001.0) == 100.0
    assert has_active_rpc({"active_media_rpc": "upload"}, now_ts=1_200.0) is False
    assert "async with worker_media_rpc_heartbeat(" in BOT
    assert "worker_has_active_bounded_media_rpc(item, now_ts)" in BOT
    assert "deadline = time.monotonic() + MEDIA_RPC_HARD_TIMEOUT_SECONDS" in BOT
    assert "worker_effective_progress_at(item, now_ts)" in BOT


def test_retryable_hybrid_download_timeout_uses_deterministic_exponential_jitter():
    timeout_classifier, _ = load_bot_function(
        "is_retryable_hybrid_download_timeout",
        {
            "HybridDownloadError": HybridDownloadError,
            "TimeoutError": TimeoutError,
            "asyncio": asyncio,
        },
    )
    backoff, _ = load_bot_function(
        "source_job_retry_delay_seconds",
        {
            "SOURCE_FAILED_RETRY_DELAY_SECONDS": 600,
            "SOURCE_FAILED_RETRY_MAX_DELAY_SECONDS": 7_200,
            "SOURCE_FAILED_RETRY_JITTER_SECONDS": 60,
            "is_retryable_hybrid_download_timeout": timeout_classifier,
            "hashlib": hashlib,
        },
    )

    timeout = HybridDownloadError(
        "hybrid userbot download media timed out after 420s",
        cause=TimeoutError("deadline exceeded"),
        retryable=True,
    )
    job = {"job_id": "job-1", "post_uid": "uid-1", "source": "startup_hot"}

    first = backoff(job, timeout, retry_count=1)
    second = backoff(job, timeout, retry_count=2)
    assert 600 <= first <= 660
    assert 1_200 <= second <= 1_260
    assert backoff(job, timeout, retry_count=2) == second
    assert backoff(job, RuntimeError("temporary non-timeout"), retry_count=4) == 600
    assert "Temporary hybrid download timeout retained for retry" in BOT


def test_size_aware_download_budget_covers_the_observed_slow_media_sizes():
    timeout_for_message, _ = load_bot_function(
        "download_timeout_for_message",
        {
            "DOWNLOAD_MEDIA_TIMEOUT_SECONDS": 900,
            "DOWNLOAD_MEDIA_SECONDS_PER_MB": 8,
        },
    )
    mib = 1024 * 1024

    def message(size_mb):
        return SimpleNamespace(video=SimpleNamespace(file_size=int(size_mb * mib)), photo=None)

    # The old curve emitted 205/280/338/420 seconds for these values. The
    # conservative final curve avoids retrying a merely slow valid transfer.
    assert timeout_for_message(message(21)) == 288
    assert timeout_for_message(message(40)) == 440
    assert timeout_for_message(message(54.5)) == 556
    assert timeout_for_message(message(75)) == 720
    assert timeout_for_message(message(100)) == 900
    assert "ROYELLS_DOWNLOAD_MEDIA_TIMEOUT_SECONDS=900" in DOCKERFILE
    assert "ROYELLS_DOWNLOAD_MEDIA_SECONDS_PER_MB=8" in DOCKERFILE


def test_manual_telegram_link_fresh_download_recovery_stays_durable_after_normal_budget():
    """A manual link cannot be released to a source scan that does not own it."""

    clock = SimpleNamespace(now=10_000.0)
    delays = (600, 1_800, 5_400)
    delay_for_count, _ = load_bot_function(
        "manual_fresh_download_deferred_delay_seconds",
        {"MANUAL_FRESH_DOWNLOAD_DEFERRED_RETRY_DELAYS_SECONDS": delays},
    )
    build_manual_retry, _ = load_bot_function(
        "build_manual_fresh_download_deferred_retry_job",
        {
            "MANUAL_FRESH_DOWNLOAD_DEFERRED_RETRY_DELAYS_SECONDS": delays,
            "manual_fresh_download_deferred_delay_seconds": delay_for_count,
            "now_iso": lambda: "2026-08-12T00:00:00",
            "time": SimpleNamespace(time=lambda: clock.now),
        },
    )
    manual_job = {
        "job_id": "manual-1",
        "source": "telegram_link",
        "files": ["stale.bin"],
        "_partial_download_files": ["partial.bin"],
        "_fresh_download_recovery_count": 2,
    }

    first = build_manual_retry(manual_job, "FILE_PART_X_MISSING")
    assert first["files"] == []
    assert first["_partial_download_files"] == []
    assert first["_manual_fresh_download_deferred_count"] == 1
    assert first["_manual_fresh_download_deferred_due_at"] == 10_600.0

    second = build_manual_retry(first, "FILE_PART_X_MISSING")
    third = build_manual_retry(second, "FILE_PART_X_MISSING")
    fourth = build_manual_retry(third, "FILE_PART_X_MISSING")
    assert second["_manual_fresh_download_deferred_due_at"] == 11_800.0
    assert third["_manual_fresh_download_deferred_due_at"] == 15_400.0
    assert fourth["_manual_fresh_download_deferred_count"] == 3
    assert fourth["_manual_fresh_download_deferred_due_at"] == 15_400.0
    assert build_manual_retry({"source": "startup_hot"}, "x") is None
    assert delay_for_count(1) == 600
    assert delay_for_count(2) == 1_800
    assert delay_for_count(99) == 5_400

    remaining_seconds, _ = load_bot_function(
        "manual_fresh_download_deferred_remaining_seconds",
        {
            "MANUAL_FRESH_DOWNLOAD_DEFERRED_RETRY_DELAYS_SECONDS": delays,
            "time": SimpleNamespace(time=lambda: clock.now),
        },
    )
    assert remaining_seconds(first) == 600
    assert remaining_seconds(
        {"source": "telegram_link", "_manual_fresh_download_deferred_due_at": 999_999}
    ) == 5_400
    assert remaining_seconds({"source": "startup_hot", "_manual_fresh_download_deferred_due_at": 10_600}) == 0

    plan_retry, _ = load_bot_function(
        "plan_fresh_download_retry",
        {
            "build_fresh_download_retry_job": lambda *_args: None,
            "build_manual_fresh_download_deferred_retry_job": build_manual_retry,
            "manual_fresh_download_deferred_delay_seconds": delay_for_count,
        },
    )
    planned, delay, is_manual_deferred = plan_retry(manual_job, "FILE_PART_X_MISSING", 5)
    assert planned["_manual_fresh_download_deferred_count"] == 1
    assert delay == 600
    assert is_manual_deferred is True

    assert "manual_fresh_download_deferred_count" in BOT
    assert "manual_fresh_download_deferred_due_at" in BOT
    assert "manual delayed fresh-download retry" in BOT
    assert BOT.count("plan_fresh_download_retry(") >= 3  # definition + both upload branches


def test_channel_fresh_download_recovery_is_renewable_and_scheduler_safe_after_normal_budget():
    """FILE_PART/MEDIA_EMPTY channel jobs must outlive cursor/lookback windows."""

    clock = SimpleNamespace(now=10_000.0)
    delays = (600, 1_800, 5_400)
    delay_for_count, _ = load_bot_function(
        "source_fresh_download_deferred_delay_seconds",
        {"SOURCE_FRESH_DOWNLOAD_DEFERRED_RETRY_DELAYS_SECONDS": delays},
    )
    build_source_retry, _ = load_bot_function(
        "build_source_fresh_download_deferred_retry_job",
        {
            "SOURCE_FRESH_DOWNLOAD_DEFERRED_RETRY_DELAYS_SECONDS": delays,
            "source_fresh_download_deferred_delay_seconds": delay_for_count,
            "now_iso": lambda: "2026-08-12T00:00:00+06:00",
            "time": SimpleNamespace(time=lambda: clock.now),
        },
    )
    channel_job = {
        "job_id": "channel-1",
        "source": "startup_hot",
        "messages": [{"chat_id": -100123, "message_id": 77}],
        "files": ["stale.bin"],
        "_partial_download_files": ["partial.bin"],
        "_fresh_download_recovery_count": 2,
    }

    first = build_source_retry(channel_job, "FILE_PART_X_MISSING")
    assert first["job_id"] == "channel-1"
    assert first["messages"] == channel_job["messages"]
    assert first["files"] == []
    assert first["_partial_download_files"] == []
    assert first["_source_fresh_download_deferred_count"] == 1
    assert first["_source_fresh_download_deferred_due_at"] == 10_600.0

    second = build_source_retry(first, "MEDIA_EMPTY")
    third = build_source_retry(second, "MEDIA_EMPTY")
    fourth = build_source_retry(third, "MEDIA_EMPTY")
    assert second["_source_fresh_download_deferred_due_at"] == 11_800.0
    assert third["_source_fresh_download_deferred_due_at"] == 15_400.0
    assert fourth["_source_fresh_download_deferred_count"] == 3
    assert fourth["_source_fresh_download_deferred_due_at"] == 15_400.0
    assert build_source_retry({"source": "telegram_link"}, "MEDIA_EMPTY") is None
    assert delay_for_count(1) == 600
    assert delay_for_count(2) == 1_800
    assert delay_for_count(99) == 5_400

    remaining_seconds, _ = load_bot_function(
        "source_fresh_download_deferred_remaining_seconds",
        {
            "SOURCE_FRESH_DOWNLOAD_DEFERRED_RETRY_DELAYS_SECONDS": delays,
            "time": SimpleNamespace(time=lambda: clock.now),
        },
    )
    assert remaining_seconds(first) == 600
    assert remaining_seconds(
        {
            "source": "startup_hot",
            "_source_fresh_download_deferred_due_at": 999_999,
        }
    ) == 5_400
    assert remaining_seconds(
        {
            "source": "telegram_link",
            "_source_fresh_download_deferred_due_at": 10_600,
        }
    ) == 0

    serialize, _ = load_bot_function(
        "serialize_runtime_job", {"message_meta": lambda message: {}}
    )
    checkpoint_payload = serialize(
        {
            **first,
            "type": "single",
            "ch_name": "source channel",
            "messages": channel_job["messages"],
        }
    )
    assert checkpoint_payload["source_fresh_download_deferred_count"] == 1
    assert checkpoint_payload["source_fresh_download_deferred_due_at"] == 10_600.0

    restored_message = SimpleNamespace(
        chat=SimpleNamespace(id=-100123), id=77
    )

    async def fetch_checkpoint_messages(_metas):
        return [restored_message]

    hydrate, _ = load_bot_function(
        "hydrate_runtime_job_from_checkpoint",
        {
            "SOURCE_FRESH_DOWNLOAD_DEFERRED_RETRY_DELAYS_SECONDS": delays,
            "MANUAL_FRESH_DOWNLOAD_DEFERRED_RETRY_DELAYS_SECONDS": delays,
            "fetch_messages_from_meta": fetch_checkpoint_messages,
            "media_path_is_complete_enough": lambda *_args: False,
            "make_job_id": lambda *_args: "generated-job",
            "stamp_queue_job": lambda job, _queue_name: job,
        },
    )
    restored = asyncio.run(hydrate(checkpoint_payload, "download"))
    assert restored["_source_fresh_download_deferred_count"] == 1
    assert restored["_source_fresh_download_deferred_due_at"] == 10_600.0
    assert restored["_source_fresh_download_deferred_reason"] == "FILE_PART_X_MISSING"

    plan_retry, _ = load_bot_function(
        "plan_fresh_download_retry",
        {
            "build_fresh_download_retry_job": lambda *_args: None,
            "build_manual_fresh_download_deferred_retry_job": lambda *_args: None,
            "build_source_fresh_download_deferred_retry_job": build_source_retry,
            "source_fresh_download_deferred_delay_seconds": delay_for_count,
        },
    )
    planned, delay, is_deferred = plan_retry(channel_job, "MEDIA_EMPTY", 5)
    assert planned["_source_fresh_download_deferred_count"] == 1
    assert delay == 600
    assert is_deferred is True

    for incident_name in ("FILE_PART_X_MISSING", "MEDIA_EMPTY"):
        calls = []
        logs = []
        retain, _ = load_bot_function(
            "retain_fresh_download_retry_for_recovery",
            {
                "record_download_job": lambda job, status, error=None, files=None: calls.append(
                    ("download", job["job_id"], status, str(error), files)
                ),
                "record_total_job": lambda job, status, error=None: calls.append(
                    ("total", job["job_id"], status, str(error))
                ),
                "record_sync_item": lambda job, status, error=None: calls.append(
                    ("sync", job["job_id"], status, str(error))
                ),
                "log_event": logs.append,
            },
        )
        assert retain(planned, incident_name) is True
        assert calls[0] == (
            "download",
            "channel-1",
            "retry_later",
            incident_name,
            [],
        )
        assert ("total", "channel-1", "queued_download", incident_name) in calls
        assert ("sync", "channel-1", "retry_later", incident_name) in calls
        assert "retained for recovery" in logs[0]

    upload_worker = BOT.split("async def upload_worker_loop", 1)[1].split(
        "async def process_telegram_link", 1
    )[0]
    assert "source-owned delayed fresh-download retry" in upload_worker
    assert upload_worker.count("retain_fresh_download_retry_for_recovery(retry_job, e)") == 2
    assert "_source_fresh_download_deferred_due_at" in BOT
    assert "source_fresh_download_deferred_due_at" in BOT


def test_unavailable_source_item_is_durable_cooled_and_keeps_album_checkpoint():
    """An empty source item cannot be scan-requeued before its owned retry is due."""

    clock = SimpleNamespace(now=10_000.0)
    delays = (600, 1_800, 5_400)
    message = SimpleNamespace(chat=SimpleNamespace(id=-100123), id=77)
    healthy_message = SimpleNamespace(chat=SimpleNamespace(id=-100123), id=78)
    namespace = {
        "SOURCE_ITEM_UNAVAILABLE_RETRY_DELAYS_SECONDS": delays,
        "SOURCE_ITEM_UNAVAILABLE_RETRY_STATE_LIMIT": 64,
        "get_media_uid": lambda _message: "",
        "time": SimpleNamespace(time=lambda: clock.now),
        "now_iso": lambda: "2026-08-13T00:00:00Z",
    }
    key_for_message, namespace = load_bot_function(
        "source_item_unavailable_message_key", namespace
    )
    delay_for_count, namespace = load_bot_function(
        "source_item_unavailable_retry_delay_seconds", namespace
    )
    normalize, namespace = load_bot_function(
        "normalize_source_item_unavailable_retries", namespace
    )
    namespace.update(
        {
            "source_item_unavailable_message_key": key_for_message,
            "source_item_unavailable_retry_delay_seconds": delay_for_count,
            "normalize_source_item_unavailable_retries": normalize,
        }
    )
    retries_for_job, namespace = load_bot_function(
        "source_item_unavailable_retries_for_job", namespace
    )
    namespace["source_item_unavailable_retries_for_job"] = retries_for_job
    next_remaining, namespace = load_bot_function(
        "source_item_unavailable_next_remaining_seconds", namespace
    )
    namespace["source_item_unavailable_next_remaining_seconds"] = next_remaining
    remaining, namespace = load_bot_function(
        "source_item_unavailable_remaining_seconds", namespace
    )
    builder, _ = load_bot_function(
        "build_source_item_unavailable_retry_job", namespace
    )

    original = {
        "job_id": "album-empty-1",
        "source": "startup_hot",
        "type": "album",
        "messages": [message, healthy_message],
        "files": ["stale-untrusted.bin"],
        "_partial_download_files": ["healthy-part.mp4"],
        "_partial_download_message_metas": [
            {"chat_id": -100123, "message_id": 78}
        ],
    }
    retry_job, delay = builder(original, [message], "empty")
    item_key = key_for_message(message)

    assert delay == 600
    assert retry_job["messages"] == [message, healthy_message]
    assert retry_job["files"] == []
    assert retry_job["_partial_download_files"] == ["healthy-part.mp4"]
    assert retry_job["_source_item_unavailable_retries"][item_key]["count"] == 1
    assert retry_job["_source_item_unavailable_retries"][item_key]["due_at"] == 10_600.0
    assert remaining(retry_job, message) == 600
    assert remaining(retry_job, healthy_message) == 0

    # A restart serializes the same descriptor; a scan/recovery path must retain
    # it rather than release the failed source item to a fresh source scan.
    serialize, _ = load_bot_function(
        "serialize_runtime_job", {"message_meta": lambda message: {"chat_id": message.chat.id, "message_id": message.id}}
    )
    payload = serialize(retry_job)
    assert payload["source_item_unavailable_retries"][item_key]["due_at"] == 10_600.0
    assert "source_item_unavailable_retries=rec.get(" in BOT
    enqueue = BOT.split("async def enqueue_media_job", 1)[1].split(
        "async def try_copy_message_fast_path", 1
    )[0]
    assert "source_item_unavailable_next_remaining_seconds(job)" in enqueue
    download_worker = BOT.split("async def channel_download_worker_loop", 1)[1].split(
        "async def upload_worker_loop", 1
    )[0]
    assert "source_item_unavailable_remaining_seconds(job, m) > 0" in download_worker
    unavailable_branch = download_worker.split(
        "Media item retained for durable unavailable-source retry", 1
    )[1].split("if not downloaded_files:", 1)[0]
    assert "remove_processing_keys(m.chat.id, m)" not in unavailable_branch
    assert "record_download_job(" in unavailable_branch
    assert '"retry_later"' in unavailable_branch

    clock.now = 10_600.0
    second, second_delay = builder(retry_job, [message], "empty")
    assert second_delay == 1_800
    assert second["_source_item_unavailable_retries"][item_key]["count"] == 2
    assert second["_source_item_unavailable_retries"][item_key]["due_at"] == 12_400.0


def test_restricted_forward_capability_is_ttl_bounded_and_survives_restart():
    """CHAT_FORWARDS_RESTRICTED must bypass impossible copies after a restart."""

    clock = SimpleNamespace(now=10_000.0)
    saved = []
    metrics = []
    cache = {}
    state = {"sync_source_manager": {"copy_restricted_sources": {}}}
    namespace = {
        "STATE": state,
        "state_mutex": threading.RLock(),
        "time": SimpleNamespace(time=lambda: clock.now),
        "contextlib": contextlib,
        "copy_restricted_cache": cache,
        "COPY_RESTRICTED_CACHE_TTL_SECONDS": 600,
        "normalize_channel_id": lambda value: str(value),
        "now_iso": lambda: "2026-08-12T00:00:00+06:00",
        "save_state": saved.append,
        "metric_increment": metrics.append,
    }
    cache_key, _ = load_bot_function("copy_restricted_cache_key", namespace)
    source_state, _ = load_bot_function("copy_restricted_source_state", namespace)
    expiry, _ = load_bot_function(
        "copy_restricted_expiry", {"contextlib": contextlib}
    )
    namespace.update(
        {
            "copy_restricted_cache_key": cache_key,
            "copy_restricted_source_state": source_state,
            "copy_restricted_expiry": expiry,
        }
    )
    remember, _ = load_bot_function("remember_copy_restricted", namespace)
    cached, _ = load_bot_function("copy_restricted_cached", namespace)

    restriction = RuntimeError(
        "Telegram says: [400 CHAT_FORWARDS_RESTRICTED] - The chat restricts forwarding content"
    )
    assert remember("-100123", restriction) is True
    assert state["sync_source_manager"]["copy_restricted_sources"]["-100123"][
        "expires_at"
    ] == 10_600.0
    assert saved == ["sync_source_manager"]
    assert metrics == ["copy_restricted_fallbacks"]

    # A new process starts with no RAM cache, then loads the persisted TTL.
    cache.clear()
    assert cached("-100123") is True
    assert cache["-100123"] == 10_600.0
    clock.now = 10_601.0
    assert cached("-100123") is False


def test_media_empty_is_rehydrated_before_album_member_isolation_or_dead_listing():
    is_media_empty, _ = load_bot_function("is_media_empty_upload_error")
    permanent, _ = load_bot_function("is_permanent_dead_media_error")

    assert is_media_empty(RuntimeError("[400 MEDIA_EMPTY] invalid upload media")) is True
    assert permanent(RuntimeError("MEDIA_EMPTY source media unavailable")) is False
    assert permanent(RuntimeError("FILE_PART_X_MISSING source message deleted")) is False
    assert permanent(RuntimeError("source media no longer available")) is True

    album = BOT.split("async def send_source_album_chunk_resilient", 1)[1].split(
        "def split_source_album_chunks", 1
    )[0]
    individual = BOT.split("async def send_source_items_individually", 1)[1].split(
        "# External platform cookie/session management removed.", 1
    )[0]
    upload_worker = BOT.split("async def upload_worker_loop", 1)[1].split(
        "async def process_telegram_link", 1
    )[0]

    assert "MEDIA_EMPTY; complete album fresh download required before member isolation" in album
    assert "MediaArtifactRefreshRequired" in album
    assert "Deterministic invalid album member detected" not in album
    assert album.index("if is_media_empty_upload_error(exc):") < album.index(
        "is_invalid_media_upload_error(exc)"
    )
    assert "MEDIA_EMPTY; fresh download required before retrying album item" in individual
    assert "force_dead=True" not in album
    assert "force_dead=True" not in individual
    assert "isinstance(e, MediaArtifactRefreshRequired)" in upload_worker
    assert "remember_dead_media(messages, e, force_dead=True)" in upload_worker


def test_retry_scheduler_keeps_delayed_descriptor_visible_and_preempts_long_wait():
    """A future retry must remain healer-visible while a nearer retry can run."""

    class TargetQueue:
        def __init__(self):
            self.items = []
            self.admitted = asyncio.Event()

        def full(self):
            return False

        def put_nowait(self, job):
            self.items.append(job)
            self.admitted.set()

        def job_ids(self):
            return {
                str(job.get("job_id") or "")
                for job in self.items
                if job.get("job_id")
            }

    async def exercise():
        retry_queue = asyncio.PriorityQueue()
        wakeup = asyncio.Event()
        download_target = TargetQueue()
        upload_target = TargetQueue()
        link_target = TargetQueue()
        long_job = {"job_id": "long-delayed"}
        short_job = {"job_id": "short-due"}
        generations = {}
        namespace = {
            "asyncio": asyncio,
            "contextlib": contextlib,
            "time": time,
            "retry_admission_queue": retry_queue,
            "RETRY_SCHEDULER_WAKEUP": wakeup,
            "RETRY_GENERATIONS": generations,
            "RETRY_SEQUENCE": iter(range(2, 10_000)),
            "channel_download_queue": download_target,
            "upload_queue": upload_target,
            "link_process_queue": link_target,
            "stamp_queue_job": lambda job, queue_name: job.update(
                {"_queue_name": queue_name}
            ),
            "mark_runtime_checkpoint_dirty": lambda _reason: None,
            "log_event": lambda _message: None,
            "active_worker_jobs_lock": threading.RLock(),
            "active_worker_jobs": {},
        }
        wake, _ = load_bot_function("wake_retry_scheduler", namespace)
        peek, _ = load_bot_function("peek_retry_admission_entry", namespace)
        namespace.update(
            {
                "wake_retry_scheduler": wake,
                "peek_retry_admission_entry": peek,
            }
        )
        scheduler, _ = load_bot_function("retry_scheduler_loop", namespace)
        live_ids, _ = load_bot_function("live_pipeline_job_ids", namespace)

        long_entry = (
            time.monotonic() + 1.0,
            1,
            "download:long-delayed",
            1,
            "download",
            long_job,
            "long recovery delay",
        )
        generations["download:long-delayed"] = 1
        retry_queue.put_nowait(long_entry)
        wake()

        task = asyncio.create_task(scheduler())
        try:
            await asyncio.sleep(0.02)
            # The scheduler is waiting, but does not own/remove the future
            # entry; queue healing therefore sees it as live rather than stale.
            assert "long-delayed" in live_ids()
            assert retry_queue.qsize() == 1

            short_entry = (
                time.monotonic() + 0.03,
                2,
                "download:short-due",
                1,
                "download",
                short_job,
                "urgent retry",
            )
            generations["download:short-due"] = 1
            retry_queue.put_nowait(short_entry)
            wake()

            await asyncio.wait_for(download_target.admitted.wait(), timeout=0.5)
            assert download_target.items == [short_job]
            assert "download:short-due" not in generations
            assert "download:long-delayed" in generations
            assert retry_queue.qsize() == 1
            assert retry_queue._queue[0][2] == "download:long-delayed"
            assert "long-delayed" in live_ids()
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            # Cancellation occurs while the scheduler is waiting, not while it
            # owns the descriptor, so the durable retry remains recoverable.
            assert retry_queue.qsize() == 1
            assert retry_queue._queue[0][2] == "download:long-delayed"

    asyncio.run(exercise())


def test_retry_scheduler_deduplicates_each_live_job_generation():
    """Repeated recovery admission must retain one durable retry descriptor."""

    retry_queue = asyncio.PriorityQueue()
    wakeup = asyncio.Event()
    metrics = []
    namespace = {
        "asyncio": asyncio,
        "time": time,
        "retry_admission_queue": retry_queue,
        "RETRY_SCHEDULER_WAKEUP": wakeup,
        "RETRY_GENERATIONS": {},
        "RETRY_SEQUENCE": iter(range(1, 100)),
        "V21_ENGINE": None,
        "metric_increment": metrics.append,
        "mark_runtime_checkpoint_dirty": lambda _reason: None,
        "log_event": lambda _message: None,
    }
    retry_key_fn, _ = load_bot_function("retry_key", namespace)
    retry_reason_fn, _ = load_bot_function("retry_reason_text", namespace)
    wake_fn, _ = load_bot_function("wake_retry_scheduler", namespace)
    namespace.update(
        {
            "retry_key": retry_key_fn,
            "retry_reason_text": retry_reason_fn,
            "wake_retry_scheduler": wake_fn,
        }
    )
    schedule, _ = load_bot_function("schedule_queue_retry", namespace)

    job = {"job_id": "one-only"}
    assert schedule("download", job, 60, "temporary") is True
    assert schedule("download", job, 60, "duplicate admission") is True
    assert retry_queue.qsize() == 1
    assert namespace["RETRY_GENERATIONS"] == {"download:one-only": 1}
    assert wakeup.is_set() is True
    assert metrics == ["download_retries", "retry_deduplications"]


def test_optional_compute_helper_isolated_from_telegram_delivery_authority():
    """A future helper must not silently become a second Telegram worker."""

    inspector = (ROOT / "royells_compute_helper.py").read_text(encoding="utf-8").lower()
    service = (ROOT / "royells_compute_helper_service.py").read_text(encoding="utf-8").lower()
    helper_docker = (ROOT / "Dockerfile.compute-helper").read_text(encoding="utf-8")
    helper_docs = (ROOT / "docs" / "EXTERNAL_COMPUTE_HELPER.md").read_text(encoding="utf-8")

    assert "pyrogram" not in inspector
    assert "os.getenv" not in inspector
    assert "import pyrogram" not in service
    assert "from pyrogram" not in service
    assert "royells_user_session" not in service
    assert "royells_bot_token" not in service
    assert "royells_media_bot_ready.py" not in helper_docker
    assert "royells_v21_micro_workers.py" not in helper_docker
    assert "royells_compute_helper" not in BOT
    assert "never give the helper" in helper_docs.lower()
    assert "one ordered publisher" in helper_docs.lower()
