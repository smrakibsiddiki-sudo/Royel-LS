import ast
import asyncio
import contextlib
import os
import re
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
BOT_PATH = ROOT / "royells_media_bot_ready.py"
BOT = BOT_PATH.read_text(encoding="utf-8")
BOT_TREE = ast.parse(BOT, filename=str(BOT_PATH))


def load_bot_function(name, namespace=None):
    """Compile one production helper without importing Telegram clients."""

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


def test_exact_telegram_media_invalid_spelling_is_classified():
    classify, _ = load_bot_function("is_invalid_media_upload_error")

    assert classify(
        RuntimeError(
            '[400 MEDIA_INVALID] - The media is invalid '
            '(caused by "messages.SendMultiMedia")'
        )
    )
    assert classify(RuntimeError("MEDIA_EMPTY"))
    assert not classify(RuntimeError("[420 FLOOD_WAIT_X] retry later"))


def test_source_video_metadata_is_sanitized_and_used_by_group_and_single_send():
    metadata, _ = load_bot_function("sanitized_source_video_upload_metadata")

    class CapturedVideo:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class CapturedPhoto:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    message = SimpleNamespace(
        photo=None,
        video=SimpleNamespace(
            width="1920",
            height=1080,
            duration=61,
            supports_streaming=False,
        ),
    )
    assert metadata(message) == {
        "width": 1920,
        "height": 1080,
        "duration": 61,
        "supports_streaming": False,
    }
    assert metadata(
        SimpleNamespace(
            video=SimpleNamespace(
                width=True,
                height=-1,
                duration="unknown",
                supports_streaming="yes",
            )
        )
    ) == {}

    build_group, _ = load_bot_function(
        "build_source_media_group",
        {
            "sanitized_source_video_upload_metadata": metadata,
            "InputMediaVideo": CapturedVideo,
            "InputMediaPhoto": CapturedPhoto,
        },
    )
    group = asyncio.run(build_group([(message, "source.mp4")]))
    assert group[0].kwargs == {
        "media": "source.mp4",
        "supports_streaming": False,
        "width": 1920,
        "height": 1080,
        "duration": 61,
    }

    captured = {}

    async def target_send_video(label, path, **kwargs):
        captured.update({"label": label, "path": path, **kwargs})
        return "sent"

    async def ensure_fingerprints(_state, _files):
        return None

    class UploadRecovery(Exception):
        pass

    class RefreshRequired(Exception):
        pass

    send_single, _ = load_bot_function(
        "send_prepared_source_single",
        {
            "sanitized_source_video_upload_metadata": metadata,
            "target_send_video": target_send_video,
            "target_send_photo": None,
            "ensure_upload_file_fingerprints": ensure_fingerprints,
            "is_file_part_missing_error": lambda _exc: False,
            "metric_increment": lambda *_args, **_kwargs: None,
            "recover_upload_session_state": None,
            "UploadSessionRecoveryError": UploadRecovery,
            "MediaArtifactRefreshRequired": RefreshRequired,
        },
    )
    assert asyncio.run(send_single(message, "source.mp4")) == "sent"
    assert captured["width"] == 1920
    assert captured["height"] == 1080
    assert captured["duration"] == 61
    assert captured["supports_streaming"] is False


class FakeFloodWait(Exception):
    pass


def _album_function(group_error):
    classify, _ = load_bot_function("is_invalid_media_upload_error")
    media_empty, _ = load_bot_function("is_media_empty_upload_error")
    flood_wait, _ = load_bot_function(
        "is_flood_wait_error",
        {"FloodWait": FakeFloodWait},
    )
    calls = {"group": 0, "individual": 0}

    async def send_group(*_args, **_kwargs):
        calls["group"] += 1
        raise group_error

    async def send_individual(items, _worker_id, _channel, job=None):
        calls["individual"] += 1
        return [(message, f"sent-{index}") for index, (message, _path) in enumerate(items)]

    async def no_confirmation(_items):
        return []

    class UploadRecovery(Exception):
        pass

    class RefreshRequired(Exception):
        pass

    function, _ = load_bot_function(
        "send_source_album_chunk_resilient",
        {
            "asyncio": asyncio,
            "PRESERVE_ALBUMS": True,
            "SOURCE_ALBUM_SEND_ATTEMPTS": 2,
            "SOURCE_ALBUM_INDIVIDUAL_FALLBACK": True,
            "SOURCE_ALBUM_GROUP_FAILURE_INDIVIDUAL_FALLBACK": True,
            "UploadSessionRecoveryError": UploadRecovery,
            "MediaArtifactRefreshRequired": RefreshRequired,
            "send_media_group_with_file_part_recovery": send_group,
            "send_source_items_individually": send_individual,
            "send_prepared_source_single": None,
            "is_flood_wait_error": flood_wait,
            "is_media_empty_upload_error": media_empty,
            "is_invalid_media_upload_error": classify,
            "is_temporary_network_error": lambda _exc: False,
            "confirm_recent_target_album": no_confirmation,
            "remove_processing_keys": lambda *_args: None,
            "log_event": lambda *_args, **_kwargs: None,
            "log_event_rate_limited": lambda *_args, **_kwargs: None,
        },
    )
    return function, calls


def test_album_media_invalid_enters_item_isolation_on_first_group_rejection():
    error = RuntimeError(
        '[400 MEDIA_INVALID] - The media is invalid '
        '(caused by "messages.SendMultiMedia")'
    )
    send_album, calls = _album_function(error)
    items = [
        (SimpleNamespace(chat=SimpleNamespace(id=-1001)), "one.mp4"),
        (SimpleNamespace(chat=SimpleNamespace(id=-1001)), "two.mp4"),
    ]

    result = asyncio.run(send_album(items, 1, "source"))

    assert len(result) == 2
    assert calls == {"group": 1, "individual": 1}


def test_album_flood_wait_is_propagated_without_plain_or_item_retry():
    error = FakeFloodWait("[420 FLOOD_WAIT_X] wait of 19 seconds")
    send_album, calls = _album_function(error)
    items = [
        (SimpleNamespace(chat=SimpleNamespace(id=-1001)), "one.mp4"),
        (SimpleNamespace(chat=SimpleNamespace(id=-1001)), "two.mp4"),
    ]

    try:
        asyncio.run(send_album(items, 1, "source"))
    except FakeFloodWait as caught:
        assert caught is error
    else:
        raise AssertionError("FloodWait was aggregated instead of propagated")
    assert calls == {"group": 1, "individual": 0}


def test_bot_gateway_transport_error_never_schedules_userbot_reconnect():
    role, _ = load_bot_function("telegram_client_role", {"re": re})
    scheduled = []

    class AsyncSemaphore:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    async def wait_noop(*_args, **_kwargs):
        return None

    def failing_factory():
        async def fail():
            raise ConnectionError("connection lost")

        return fail()

    gateway, _ = load_bot_function(
        "telegram_gateway_await",
        {
            "asyncio": asyncio,
            "time": time,
            "os": os,
            "FloodWait": FakeFloodWait,
            "TELEGRAM_CALL_RETRIES": 1,
            "DOWNLOAD_MEDIA_TIMEOUT_SECONDS": 30,
            "UPLOAD_SEND_TIMEOUT_SECONDS": 30,
            "TELEGRAM_CALL_TIMEOUT_SECONDS": 30,
            "telegram_client_role": role,
            "wait_for_telegram_client": wait_noop,
            "begin_telegram_inflight": lambda *_args, **_kwargs: "token",
            "end_telegram_inflight": lambda *_args, **_kwargs: None,
            "wait_global_flood_gate": wait_noop,
            "telegram_control_semaphore": AsyncSemaphore(),
            "metric_increment": lambda *_args, **_kwargs: None,
            "log_event_rate_limited": lambda *_args, **_kwargs: None,
            "note_global_flood_wait": lambda *_args, **_kwargs: 1,
            "should_wait_for_internal_flood_retry": lambda *_args: False,
            "is_session_auth_error": lambda _exc: False,
            "should_reconnect_telegram_error": lambda _exc: True,
            "schedule_userbot_reconnect": scheduled.append,
            "is_temporary_network_error": lambda _exc: False,
        },
    )

    with contextlib.suppress(ConnectionError):
        asyncio.run(
            gateway(
                "app.send_message",
                failing_factory,
                retries=1,
                client_role="bot",
            )
        )
    assert scheduled == []

    with contextlib.suppress(ConnectionError):
        asyncio.run(
            gateway(
                "userbot.get_messages",
                failing_factory,
                retries=1,
                client_role="userbot",
            )
        )
    assert len(scheduled) == 1


def test_inflight_roles_allow_reconnect_to_wait_only_for_userbot_transfers():
    inflight = {}
    lock = threading.RLock()
    role, _ = load_bot_function("telegram_client_role", {"re": re})
    begin, scope = load_bot_function(
        "begin_telegram_inflight",
        {
            "uuid": uuid,
            "time": time,
            "telegram_client_role": role,
            "TELEGRAM_INFLIGHT": inflight,
            "TELEGRAM_INFLIGHT_LOCK": lock,
            "now_iso": lambda: "2026-08-20T00:00:00+06:00",
            "mark_runtime_checkpoint_dirty": lambda *_args: None,
        },
    )
    count, _ = load_bot_function(
        "active_userbot_media_rpc_count",
        {
            "TELEGRAM_INFLIGHT": inflight,
            "TELEGRAM_INFLIGHT_LOCK": lock,
        },
    )

    user_token = begin("send_video via userbot", "media", client_role="userbot")
    begin("send_video via bot", "media", client_role="bot")
    begin("userbot.get_me", "api", client_role="userbot")
    assert count() == 1
    inflight.pop(user_token)
    assert count() == 0
    assert scope["TELEGRAM_INFLIGHT"] is inflight


def test_grouped_media_invalid_retry_migration_is_narrow_and_idempotent():
    poison_error = (
        'grouped album upload failed; album kept intact: [400 MEDIA_INVALID] '
        '- The media is invalid (caused by "messages.SendMultiMedia")'
    )
    messages = [{"chat_id": -1001, "message_id": 10, "media_uid": "uid-1"}]
    state = {
        "runtime_config": {"events": []},
        "download_queue": {
            "items": {
                "poison": {
                    "status": "retry_later",
                    "type": "album",
                    "message_count": 6,
                    "messages": deepcopy(messages),
                    "permanent_retry_count": 24,
                    "last_error": "worker retry after grouped failure",
                    "files": ["partial.mp4"],
                },
                "true-absence": {
                    "status": "retry_later",
                    "type": "album",
                    "message_count": 2,
                    "messages": deepcopy(messages),
                    "permanent_retry_count": 24,
                    "last_error": poison_error + " source media unavailable",
                },
                "unrelated": {
                    "status": "retry_later",
                    "type": "album",
                    "message_count": 2,
                    "messages": deepcopy(messages),
                    "permanent_retry_count": 3,
                    "last_error": (
                        "grouped album upload failed after FLOOD_WAIT "
                        '(caused by "messages.SendMultiMedia")'
                    ),
                },
                "terminal": {
                    "status": "failed",
                    "type": "album",
                    "message_count": 2,
                    "permanent_retry_count": 24,
                    "last_error": poison_error,
                },
            }
        },
        "upload_queue": {
            "items": {
                "poison": {
                    "status": "uploading",
                    "type": "album",
                    "message_count": 6,
                    "messages": deepcopy(messages),
                    "permanent_retry_count": 24,
                    "fresh_download_recovery_count": 2,
                    "last_error": poison_error,
                    "files": ["bad.mp4"],
                    "partial_download_files": ["bad.part"],
                }
            }
        },
    }
    saved = []
    migration_id = "grouped-media-invalid-test-v1"

    def default_state(name):
        return {"items": {}} if name.endswith("queue") else {"events": []}

    migrate, _ = load_bot_function(
        "migrate_legacy_grouped_media_invalid_retries",
        {
            "STATE": state,
            "state_mutex": threading.RLock(),
            "default_state": default_state,
            "defaultdict": __import__("collections").defaultdict,
            "QUEUE_STUCK_STATUSES": {
                "queued",
                "downloading",
                "uploading",
                "retry_later",
            },
            "LEGACY_GROUPED_MEDIA_INVALID_RETRY_MIGRATION_ID": migration_id,
            "now_iso": lambda: "2026-08-20T01:00:00+06:00",
            "save_state": saved.append,
        },
    )

    before_absence = deepcopy(state["download_queue"]["items"]["true-absence"])
    before_unrelated = deepcopy(state["download_queue"]["items"]["unrelated"])
    before_terminal = deepcopy(state["download_queue"]["items"]["terminal"])
    assert migrate() == 1

    for queue_name in ("download_queue", "upload_queue"):
        repaired = state[queue_name]["items"]["poison"]
        assert repaired["status"] == "retry_later"
        assert repaired["permanent_retry_count"] == 0
        assert repaired["files"] == []
        assert repaired["messages"] == messages
        assert repaired["legacy_retry_migration"] == migration_id
    assert state["download_queue"]["items"]["true-absence"] == before_absence
    assert state["download_queue"]["items"]["unrelated"] == before_unrelated
    assert state["download_queue"]["items"]["terminal"] == before_terminal
    assert state["runtime_config"]["migrations"][migration_id]["repaired_jobs"] == 1
    assert set(saved) == {"download_queue", "upload_queue", "runtime_config"}

    checkpoint_descriptor, _ = load_bot_function(
        "checkpoint_job_descriptor",
        {
            "STATE": state,
            "state_mutex": threading.RLock(),
            "deepcopy": deepcopy,
            "LEGACY_GROUPED_MEDIA_INVALID_RETRY_MIGRATION_ID": migration_id,
        },
    )
    stale_checkpoint = {
        "job": {
            "job_id": "poison",
            "permanent_retry_count": 24,
            "files": ["rejected.mp4"],
            "partial_download_files": ["stale.part"],
            "partial_download_messages": deepcopy(messages),
        }
    }
    sanitized = checkpoint_descriptor(stale_checkpoint)
    assert sanitized["permanent_retry_count"] == 0
    assert sanitized["files"] == []
    assert sanitized["partial_download_files"] == []
    assert stale_checkpoint["job"]["permanent_retry_count"] == 24

    snapshot = deepcopy(state)
    assert migrate() == 0
    assert state == snapshot


def test_pressure_managed_source_retry_is_renewable_with_saturated_counter():
    renewable, _ = load_bot_function(
        "source_job_retry_forever",
        {
            "SOURCE_PERMANENT_RETRY_ENABLED": True,
            "pressure_managed_source": lambda source: source == "source_guard",
        },
    )
    assert renewable(
        {
            "source": "source_guard",
            "_permanent_retry_count": 24,
            "is_fallback": False,
        }
    )
    assert not renewable(
        {
            "source": "source_guard",
            "_permanent_retry_count": 24,
            "is_fallback": True,
        }
    )

    persisted = []
    scheduled = []

    def record(kind):
        return lambda job, status, error=None, **_kwargs: persisted.append(
            (kind, job["_permanent_retry_count"], status, str(error))
        )

    def schedule(queue_name, job, delay, reason):
        scheduled.append(
            (queue_name, job["_permanent_retry_count"], delay, str(reason))
        )
        return True

    retry_later, _ = load_bot_function(
        "schedule_source_job_retry_later",
        {
            "SOURCE_PERMANENT_RETRY_LIMIT": 24,
            "source_job_retry_forever": renewable,
            "source_retry_requires_clean_download": lambda _reason: False,
            "clear_partial_download_checkpoint": lambda *_args, **_kwargs: None,
            "source_job_retry_delay_seconds": lambda *_args: 600,
            "record_download_job": record("download"),
            "record_total_job": record("total"),
            "record_sync_item": record("sync"),
            "schedule_queue_retry": schedule,
            "is_retryable_hybrid_download_timeout": lambda _reason: False,
            "log_event_rate_limited": lambda *_args, **_kwargs: None,
        },
    )
    job = {
        "job_id": "renewable",
        "source": "source_guard",
        "_permanent_retry_count": 24,
        "is_fallback": False,
    }
    assert retry_later(job, RuntimeError("temporary upload rejection"))
    assert all(counter == 24 for _kind, counter, _status, _error in persisted)
    assert scheduled == [
        ("download", 24, 600, "temporary upload rejection")
    ]


def test_role_and_flood_safety_guards_cover_all_album_and_media_paths():
    validator = BOT.split("async def session_validator_loop", 1)[1].split(
        "async def telegram_gateway_await", 1
    )[0]
    media_call = BOT.split("async def telegram_media_call", 1)[1].split(
        "def target_media_clients", 1
    )[0]
    album = BOT.split("async def send_source_album_chunk_resilient", 1)[1].split(
        "def split_source_album_chunks", 1
    )[0]
    individual = BOT.split("async def send_source_items_individually", 1)[1].split(
        "# External platform cookie/session management removed.", 1
    )[0]

    assert "Bot session validator warning" in validator
    assert validator.count("try:") >= 2
    assert 'client_role == "userbot" and should_reconnect_telegram_error' in media_call
    assert media_call.index("await wait_for_telegram_client(label)") < media_call.index(
        "begin_telegram_inflight("
    )
    assert album.count("if is_flood_wait_error(") >= 4
    assert individual.count("if is_flood_wait_error(") >= 2
    assert 'env_bool("ROYELLS_TELEGRAM_RECONNECT_DEFER_ON_PIPELINE", True)' in BOT
    reconnect = BOT.split(
        "async def recover_userbot_connection", 1
    )[1].split("async def wait_for_telegram_client", 1)[0]
    assert "active_userbot_media_rpc_count()" in reconnect
    assert "int(MEDIA_RPC_HARD_TIMEOUT_SECONDS) + 30" in reconnect
    reconnect_gate = BOT.split(
        "async def wait_for_telegram_client", 1
    )[1].split("async def wait_while_session_invalid", 1)[0]
    assert "int(MEDIA_RPC_HARD_TIMEOUT_SECONDS) + 180" in reconnect_gate
