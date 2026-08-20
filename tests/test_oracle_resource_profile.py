import ast
import asyncio
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOT_PATH = ROOT / "royells_media_bot_ready.py"
BOT = BOT_PATH.read_text(encoding="utf-8")
BOT_TREE = ast.parse(BOT, filename=str(BOT_PATH))
DOCKER = (ROOT / "Dockerfile").read_text(encoding="utf-8")
APP = (ROOT / "app.py").read_text(encoding="utf-8")
REQUIREMENTS = (ROOT / "requirements.txt").read_text(encoding="utf-8")


def load_bot_function(name, namespace=None):
    node = next(
        item
        for item in BOT_TREE.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == name
    )
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    scope = dict(namespace or {})
    exec(compile(module, str(BOT_PATH), "exec"), scope)
    return scope[name], scope


def test_oracle_e2_profile_is_hard_bounded_and_a1_keeps_one_publisher():
    assert 'RUNTIME_PROFILE = "oracle-e2-micro"' in BOT
    assert 'RUNTIME_PROFILE = "oracle-a1"' in BOT
    assert 'env_bool("ROYELLS_ORACLE_PROFILE_LOCK", False)' in BOT
    assert "and not IS_ORACLE_E2_MICRO" in BOT
    assert "1 if IS_ORACLE_E2_MICRO else 2" in BOT
    assert "ADAPTIVE_UPLOAD_WORKERS_MAX = 1" in BOT
    assert "UPLOAD_WORKERS = 1" in BOT
    assert "telegram_control_semaphore = asyncio.Semaphore(TELEGRAM_CONTROL_CONCURRENCY)" in BOT
    assert '1 if IS_ORACLE_E2_MICRO else 4' in BOT

    assert "ROYELLS_RUNTIME_PROFILE=oracle-e2-micro" in DOCKER
    assert "ROYELLS_ORACLE_PROFILE_LOCK=1" in DOCKER
    assert "ROYELLS_ADAPTIVE_MEDIA_WORKERS=0" in DOCKER
    assert "ROYELLS_TELEGRAM_DOWNLOAD_CONCURRENCY=1" in DOCKER
    assert "ROYELLS_TELEGRAM_MEDIA_CONCURRENCY=1" in DOCKER
    assert "ROYELLS_IN_MEMORY_QUEUE_MAX=24" in DOCKER
    assert "ROYELLS_UPLOAD_READY_QUEUE_MAX=4" in DOCKER
    assert "ROYELLS_TELEGRAM_CONTROL_CONCURRENCY=2" in DOCKER


def test_dashboard_stats_never_rescan_the_full_target_table():
    node = next(
        item for item in BOT_TREE.body
        if isinstance(item, ast.FunctionDef) and item.name == "get_db_stats"
    )
    source = ast.get_source_segment(BOT, node)
    assert "SELECT COUNT(*) FROM target_media_full_index" not in source
    assert "len(target_media_full_index)" in source
    assert "DB_STATS_REFRESH_INTERVAL_SECONDS" in BOT
    assert "await asyncio.sleep(DB_STATS_REFRESH_INTERVAL_SECONDS)" in BOT


def test_host_pause_suppresses_false_worker_cancellation_then_expires():
    now = time.monotonic()
    item = {
        "worker": "UP W1",
        "job_id": "album-safe",
        "started_monotonic": now - 5000,
        "last_progress_monotonic": now - 5000,
    }
    namespace = {
        "time": time,
        "MEDIA_RPC_HARD_TIMEOUT_SECONDS": 900,
    }
    active, namespace = load_bot_function(
        "worker_has_active_bounded_media_rpc", namespace
    )
    progress, namespace = load_bot_function("worker_effective_progress_at", namespace)
    namespace.update(
        {
            "WORKER_STALL_SECONDS": 1800,
            "WORKER_STALL_RESUME_GRACE_SECONDS": 180,
            "HARD_WATCHDOG_HEARTBEAT_SECONDS": 15,
            "EVENT_LOOP_HEARTBEAT_TS": time.monotonic(),
            "GLOBAL_EVENT_LOOP_STALL_ACTIVE": True,
            "WORKER_STALL_SUPPRESS_UNTIL_MONOTONIC": 0.0,
            "active_worker_jobs_lock": threading.RLock(),
            "active_worker_jobs": {"UP W1": item},
            "worker_has_active_bounded_media_rpc": active,
            "worker_effective_progress_at": progress,
        }
    )
    stalled, namespace = load_bot_function("stalled_worker_jobs", namespace)

    assert stalled() == []
    namespace["GLOBAL_EVENT_LOOP_STALL_ACTIVE"] = False
    namespace["WORKER_STALL_SUPPRESS_UNTIL_MONOTONIC"] = time.monotonic() + 60
    assert stalled() == []
    namespace["WORKER_STALL_SUPPRESS_UNTIL_MONOTONIC"] = 0.0
    assert stalled()[0]["job_id"] == "album-safe"


def test_container_is_arm_buildable_and_python_receives_sigterm():
    assert "AS wheel-builder" in DOCKER
    assert "build-essential" in DOCKER
    assert "python -m pip wheel" in DOCKER
    assert "COPY --from=wheel-builder /wheels /wheels" in DOCKER
    assert "STOPSIGNAL SIGTERM" in DOCKER
    assert 'CMD ["python", "-u", "app.py"]' in DOCKER
    assert "HEALTHCHECK" in DOCKER
    assert 'os.environ["ROYELLS_HUGGINGFACE_SPACE"] = "1"' not in APP
    assert "raise SystemExit(75)" in APP
    assert 'self.path in ("/ping", "/metrics")' in APP
    assert 'main_running = snapshot.get("state") in {"starting", "running"}' in APP


def test_oracle_operational_files_do_not_contain_secrets():
    e2 = (ROOT / ".env.oracle-e2-micro.example").read_text(encoding="utf-8")
    a1 = (ROOT / ".env.oracle-a1.example").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.oracle.yml").read_text(encoding="utf-8")
    for content in (e2, a1):
        assert "ROYELLS_BOT_TOKEN=\n" in content
        assert "ROYELLS_USER_SESSION_STRING=\n" in content
        assert "ROYELLS_API_HASH=\n" in content
    assert "restart: unless-stopped" in compose
    assert 'ROYELLS_ORACLE_PROFILE_LOCK: "1"' in compose
    assert "stop_grace_period: 90s" in compose
    assert 'max-size: "20m"' in compose


def test_future_retry_ledger_does_not_block_healthy_runnable_pipeline():
    retry_queue = asyncio.PriorityQueue()
    now = time.monotonic()
    retry_queue.put_nowait((now + 3600, 1, "download:parked", 1, "download", {"job_id": "parked"}, "later"))
    namespace = {"time": time, "retry_admission_queue": retry_queue}
    retry_count, namespace = load_bot_function("runnable_retry_job_count", namespace)

    class EmptyQueue:
        @staticmethod
        def qsize():
            return 0

    namespace.update(
        {
            "channel_download_queue": EmptyQueue(),
            "upload_queue": EmptyQueue(),
            "link_process_queue": EmptyQueue(),
            "active_worker_jobs_lock": threading.RLock(),
            "active_worker_jobs": {},
            "runnable_retry_job_count": retry_count,
        }
    )
    runnable, _ = load_bot_function("runnable_pipeline_job_count", namespace)
    assert retry_count(now) == 0
    assert runnable() == 0
    retry_queue.put_nowait((now - 1, 2, "download:due", 1, "download", {"job_id": "due"}, "now"))
    assert retry_count(time.monotonic()) == 1
    assert runnable() == 1


def test_stale_executor_environment_cannot_oversubscribe_e2():
    requested = {"value": 32}
    workers, scope = load_bot_function(
        "bounded_executor_workers",
        {
            "IS_ORACLE_E2_MICRO": True,
            "env_int": lambda _name, _default: requested["value"],
        },
    )
    assert workers("ROYELLS_MEDIA_EXECUTOR_WORKERS", 1) == 1
    scope["IS_ORACLE_E2_MICRO"] = False
    assert workers("ROYELLS_MEDIA_EXECUTOR_WORKERS", 2) == 8
    assert workers("ROYELLS_CPU_EXECUTOR_WORKERS", 1, general_cap=4) == 4


def test_removed_content_filter_does_not_import_numpy_imagehash_on_e2():
    assert "import imagehash" not in BOT
    assert "ImageHash" not in REQUIREMENTS
    assert "imagehash = None" in BOT


class OracleResourceProfileTests(unittest.TestCase):
    def test_e2_and_a1_worker_contract(self):
        test_oracle_e2_profile_is_hard_bounded_and_a1_keeps_one_publisher()

    def test_constant_time_dashboard_stats(self):
        test_dashboard_stats_never_rescan_the_full_target_table()

    def test_host_pause_worker_safety(self):
        test_host_pause_suppresses_false_worker_cancellation_then_expires()

    def test_multi_arch_container_lifecycle(self):
        test_container_is_arm_buildable_and_python_receives_sigterm()

    def test_secret_free_oracle_examples(self):
        test_oracle_operational_files_do_not_contain_secrets()

    def test_parked_retry_pressure_isolation(self):
        test_future_retry_ledger_does_not_block_healthy_runnable_pipeline()

    def test_e2_executor_oversubscription_guard(self):
        test_stale_executor_environment_cannot_oversubscribe_e2()

    def test_removed_filter_dependency_is_not_loaded(self):
        test_removed_content_filter_does_not_import_numpy_imagehash_on_e2()
