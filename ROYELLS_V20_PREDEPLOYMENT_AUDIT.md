# Royells Bot v20 - Final Pre-Deployment Audit

Audit date: 2026-07-31  
Audit root: `outputs/royells_fixed`  
Scope: every production Python module, test module, SQL migration, Docker/Space file, dependency file, release artifact, and retained audit/archive artifact.

## Executive Verdict

Royells v20 is **not ready for an unconditional production deployment yet**.

The default SQLite/JSON compatibility runtime is substantially hardened, compiles, and passes all offline tests. However, deployment should be blocked until the missing user-session fail-fast validation is fixed and the nine Ruff findings are resolved. The public health surface should also be restricted before exposing a production Space.

The current implementation is a compatibility architecture, not a completed PostgreSQL/Redis cutover. SQLite and JSON remain authoritative by design. PostgreSQL and Redis are disabled infrastructure.

Validation performed:

- Python compile: PASS
- Offline test suite: `63 passed, 2 skipped`
- Skipped tests: live PostgreSQL and live Redis integration tests requiring credentials
- Ruff `F,E9`: FAIL, 9 findings
- Top-level duplicate function/class scan: no duplicates found
- AST unreachable-statement scan: no obvious unreachable statements found
- TODO/FIXME/placeholder scan: no real production TODO, FIXME, HACK, XXX, or `NotImplementedError`
- Unsafe primitive scan: no `eval`, unsafe pickle, `os.system`, or `shell=True`
- Backup restore review: ZIP traversal, symlink, expansion-size, compression-ratio, JSON-size, and SQLite-integrity protections exist

## Blocking Findings

| Severity | Finding | Evidence | Production impact | Required fix | Backward compatible |
|---|---|---|---|---|---|
| Critical | Required user session is not included in startup fail-fast validation | `royells_media_bot_ready.py:387`, `:1432-1439`, `:18262-18274` | A missing `ROYELLS_USER_SESSION_STRING` can reach userbot startup after local/restored session cleanup. Source access, protected download, history scan, and recovery can fail or pause instead of terminating with a clear configuration error. | Add `ROYELLS_USER_SESSION_STRING` to the required-variable check for the Hugging Face production profile, before clients and workers start. | Yes |
| High | Public health/status endpoints disclose operational internals and crash text | `app.py:25-45`, `:95-133`, `:144-146`; `royells_v20_core/health.py:115-164` | Anyone who can reach the Space can inspect queue sizes, worker state, paths, architecture flags, metrics, recovery status, and `last_error`. Exception text may reveal sensitive operational context. | Make public `/ping` minimal; protect `/health`, `/ready`, `/startup`, and `/metrics` with a secret or private Space policy; never return raw crash text publicly. | Yes |
| High | Source Guard and Auto Sync are present but disabled in the shipped Docker profile | `Dockerfile:59-62`; `royells_media_bot_ready.py:18557-18576` | A deployment expecting these named workflows to run will not execute them. Adaptive intake/historical backfill are active, but they are not a source-code-equivalent proof that Source Guard and Auto Sync are active. | Document this as intentional replacement, or enable/test the required workflows. Correct the owner startup text at `:18579`, which says source guard is active even when disabled. | Yes |
| High | Static production quality gate fails | `royells_media_bot_ready.py:1040`, `:1317`, `:3852`, `:10898`, `:14822`, `:15811`, `:17007-17009`, `:17267` | Unused variables reduce clarity. Three exception variables are captured by lambdas and flagged undefined by Ruff; current immediate-awaited paths usually keep the exception scope alive, but delayed factory execution could make the error-reporting path fail. | Capture error text into a normal local string before creating each lambda; remove unused exception/name bindings. Re-run Ruff. | Yes |
| High | No live Telegram/Hugging Face restart or provider integration test was executed | Tests cover adapters offline; `tests/postgres/test_live_integration.py` and `tests/redis/test_live_integration.py` skipped | Offline tests cannot prove Telegram delivery boundary behavior, HF SIGTERM/rebuild recovery, real persistent-volume SQLite behavior, FloodWait handling, or provider TLS/reconnect behavior. | Run the production checklist and crash/recovery matrix on a staging Space with copied non-production state. | Yes |
| High | Hugging Face free hardware cannot guarantee uninterrupted 24/7 execution | Official HF documentation verified 2026-07-31 | Free hardware can sleep when unused. Ephemeral disk is lost on restart. `/data` is durable only when persistent storage is attached. | Use attached persistent storage and a non-sleeping paid hardware arrangement, or deploy to a platform intended for continuously running workers. | N/A |

## Important Non-Blocking Findings

| Severity | Finding | Evidence | Impact / fix |
|---|---|---|---|
| Medium | Several migration flags are parsed but are not independent production switches | `royells_v20_core/configuration/config.py:50-173`, `validation.py:18-91`, `container.py:330-463` | `USE_SQLITE_QUEUE`, `USE_SQLITE_CHECKPOINT`, and `ROYELLS_V20_*` are descriptive/reserved. The production container always wires the legacy queue/checkpoint and V20 supervisor. Document this accurately or make the flags operational in a later approved migration. |
| Medium | Optional PostgreSQL/Redis settings are only partially wired by the production container | `royells_v20_core/container.py:279-325` vs provider modules | Production composition builds minimal settings and bypasses provider pool/retry/SSL/TTL/namespace tuning. Optional dependencies are not passed to `LegacyHealthService`, so enabled backend health is absent. Use the dedicated providers and inject dependencies when shadow validation is approved. |
| Medium | Worker refactor is a wrapper around legacy monolithic loops | `royells_media_bot_ready.py:18367-18576`; `royells_v20_core/workers.py` | DI and supervision exist, but workers still execute functions and globals from the monolith. This is compatible, but not stateless-worker completion. Do not claim full Book 0-19 migration/cutover. |
| Medium | `/data` is created mode `0777` | `Dockerfile:153-156` | The container currently has one application user, limiting immediate exposure, but world-writable state is unnecessarily permissive. Prefer ownership by UID 1000 and mode `0750`/`0700`. |
| Medium | Broad exception suppression can hide partial cleanup/recovery failures | Pass/suppression table below | Most cases are intentional best-effort behavior; add rate-limited diagnostics to state-write rotation, row salvage fallback, file cleanup, metadata probing, and message refresh failures. |
| Medium | Dependency ranges are broad, not reproducible pins | `requirements.txt` | A future rebuild can install behavior-changing versions. Produce a tested lock/constraints file after staging validation. |
| Low | Docker contains three ignored legacy variables | `Dockerfile:12`, `:20-21`; literals at `royells_media_bot_ready.py:321`, `:410`, `:414` | Remove `ROYELLS_TRY_COPY_MESSAGE`, `ROYELLS_SOURCE_FAST_COPY`, and `ROYELLS_CONTENT_FILTER_ENABLED` from Docker or restore actual parsing. |
| Low | Old ZIPs, `__pycache__`, and historical reports remain in repository context | repository tree | They are not copied by Docker, but increase confusion and artifact size. Move them outside the deploy root. |

# Part 1 - Environment Variables

## Final Secret Table

These are the only secrets required for the default SQLite/JSON deployment.

| Variable | Required | Default | Recommended production value | Used by | Missing behavior | Add to HF Secrets | Replaces old variable |
|---|---:|---|---|---|---|---:|---|
| `ROYELLS_API_ID` | Yes | `0` | Telegram numeric API ID | `royells_media_bot_ready.py:235`, startup `:18262-18274` | Startup fails | Yes | No |
| `ROYELLS_API_HASH` | Yes | empty | Telegram API hash | `:236`, startup | Startup fails | Yes | No |
| `ROYELLS_BOT_TOKEN` | Yes | empty | BotFather token | `:237`, bot client `:1420-1429` | Startup fails | Yes | No |
| `ROYELLS_OWNER_ID` | Yes | `0` | Numeric owner user ID | `:238`, handlers/startup | Startup fails | Yes | No |
| `ROYELLS_TARGET_CHAT_ID` | Yes | `0` | `-100...` target channel ID | `:239`, upload/index paths | Startup fails | Yes | No |
| `ROYELLS_USER_SESSION_STRING` | Yes for HF | empty | Unique Pyrogram user session string used nowhere else | `:387`, userbot `:1432-1439` | Currently not fail-fast; userbot can become unusable | Yes | Replaces dependence on a local `ROYELLS_USER_SESSION` session file |

Never store the six values above in `Dockerfile`, README, logs, or repository files.

## Platform/Build Variables

| Variable | Required | Default / shipped value | Recommended | Module / result | HF Secret |
|---|---:|---|---|---|---:|
| `PORT` | Platform | `7860` | Leave platform-managed | `app.py:144`; public HTTP listener | No |
| `ROYELLS_HUGGINGFACE_SPACE` | No | Docker/app force `1` | `1` on HF | Selects HF defaults | No |
| `SPACE_ID`, `SPACE_HOST` | No | HF-provided | Do not set | Alternative HF detection | No |
| `CPU_CORES` | No | empty | Platform-provided if available | Diagnostics only | No |
| `TZ` | No | `Asia/Dhaka` through `setdefault` | Set only if display timezone must change | Display/log time | No |
| `PYTHONUNBUFFERED` | No | `1` | `1` | Immediate logs | No |
| `PYTHONDONTWRITEBYTECODE` | No | `1` | `1` | Avoid runtime `.pyc` writes | No |
| `PYTHONFAULTHANDLER` | No | `1` | `1` | Native crash diagnostics | No |
| `HOME`, `PATH` | Platform/Docker | `/home/user`, local bin prepended | Keep Docker values | Python/pip runtime | No |

## V20 Authority and Migration Variables

All are optional. Missing values use the listed safe default. Do not add them as HF Secrets; use Space Variables only if deliberately overriding.

| Variable | Default | Production recommendation | Used by / missing result |
|---|---:|---|---|
| `USE_SQLITE` | `1` | `1` | Core config; false is rejected with legacy authority |
| `USE_JSON_STATE` | `1` | `1` | Core config; false is rejected with legacy runtime |
| `USE_SQLITE_QUEUE` | `1` | `1` | Parsed, but production registry remains legacy composite |
| `USE_SQLITE_CHECKPOINT` | `1` | `1` | Parsed, but production checkpoint remains legacy JSON |
| `USE_POSTGRES` | `0` | `0` | True is explicitly rejected before approved cutover |
| `USE_REDIS_STATE` | `0` | `0` | True is rejected |
| `USE_REDIS_QUEUE` | `0` | `0` | True is rejected |
| `USE_REDIS_CHECKPOINT` | `0` | `0` | True is rejected |
| `ENABLE_POSTGRES_ADAPTER` | `0` | `0` | Optional infrastructure registration only |
| `ENABLE_REDIS_ADAPTER` | `0` | `0` | Optional projection infrastructure only |
| `ENABLE_DUAL_WRITE` | `0` | `0` | True is rejected/reserved |
| `ENABLE_DUAL_READ` | `0` | `0` | True is rejected/reserved |
| `ENABLE_MIGRATION_LOG` | `0` | `0` | Enables migration journal support only |
| `ENABLE_ROLLBACK` | `0` | `0` | Policy metadata; no active cutover |
| `ROYELLS_MIGRATION_EPOCH` | `legacy-v1` | `legacy-v1` | Authority metadata |
| `ROYELLS_QUEUE_AUTHORITY` | empty | empty | Only legacy is valid in current production |
| `ROYELLS_CHECKPOINT_AUTHORITY` | empty | empty | Only legacy is valid in current production |
| `ROYELLS_V20_ENABLED` | `1` | `1` | Reported setting; production still builds V20 container |
| `ROYELLS_V20_DEPENDENCY_INJECTION` | `1` | `1` | Reported setting; not an independent composition switch |
| `ROYELLS_V20_WORKER_REFACTOR` | `1` | `1` | Reported setting; not an independent worker switch |
| `ROYELLS_V20_READINESS` | `1` | `1` | Parsed; HTTP health service remains installed |

## PostgreSQL/Neon Variables

All are optional and must remain absent/disabled for the default deployment. `DATABASE_URL` takes precedence; `POSTGRES_URL` is its legacy alias. The split `POSTGRES_*` fields are an alternative supported only by the dedicated provider.

| Variable | Default | Recommended while disabled | Module / behavior |
|---|---:|---|---|
| `DATABASE_URL` | empty | unset | Core and PostgreSQL settings |
| `POSTGRES_URL` | empty | unset | Fallback alias for `DATABASE_URL` |
| `POSTGRES_HOST` | empty | unset | Split connection mode |
| `POSTGRES_PORT` | `5432` | `5432` | Split connection mode |
| `POSTGRES_DB` | empty | unset | Split connection mode |
| `POSTGRES_USER` | empty | unset | Split connection mode |
| `POSTGRES_PASSWORD` | empty | unset/Secret when enabled | Split connection mode |
| `POSTGRES_SSLMODE` | `require` | `verify-full` when supported, otherwise `require` | TLS mode |
| `POSTGRES_APPLICATION_NAME` | `royells-v20-book18` | `royells-v20` | Provider identity |
| `POSTGRES_CONNECT_TIMEOUT` | `15` | `15` | Connect timeout |
| `POSTGRES_STATEMENT_TIMEOUT_MS` | `120000` | `120000` | Statement cap |
| `POSTGRES_RETRY_ATTEMPTS` | `3` | `3` | Adapter retry |
| `POSTGRES_RETRY_BASE_SECONDS` | `0.5` | `0.5` | Retry backoff |
| `POSTGRES_RETRY_CAP_SECONDS` | `10` | `10` | Retry cap |
| `POSTGRES_BATCH_SIZE` | `500` | `500` | Batch writer |
| `POSTGRES_ALLOW_LOGICAL_RESTORE` | `0` | `0` | Restore safety gate |
| `POOL_MIN` | `1` | `1` | Dedicated provider pool |
| `POOL_MAX` | `5` | `5` | Dedicated provider pool |
| `POOL_TIMEOUT` | `30` | `30` | Pool acquisition |
| `POOL_MAX_IDLE` | `300` | `300` | Pool idle retirement |
| `POOL_MAX_LIFETIME` | `1800` | `1800` | Connection lifetime |

If enabled later, credentials belong in HF Secrets. Tuning values belong in Variables.

## Redis/Upstash Variables

All are optional and must remain absent/disabled for the default deployment. `REDIS_URL` is preferred for TCP. Upstash REST supports both modern and alias names.

| Variable | Default | Recommended while disabled | Module / behavior |
|---|---:|---|---|
| `REDIS_URL` | empty | unset | Preferred TCP URL |
| `REDIS_HOST` | empty | unset | Split TCP mode |
| `REDIS_PORT` | `6379` | provider value | Split TCP mode |
| `REDIS_PASSWORD` | empty | unset/Secret when enabled | Split TCP credential |
| `REDIS_DB` | `0` | `0` | Logical DB |
| `REDIS_SSL` | `1` | `1` | TLS; Upstash requires `rediss://` |
| `UPSTASH_URL` | empty | unset | REST URL |
| `UPSTASH_TOKEN` | empty | unset/Secret when enabled | REST token |
| `UPSTASH_REDIS_REST_URL` | empty | unset | Alias for `UPSTASH_URL` |
| `UPSTASH_REDIS_REST_TOKEN` | empty | unset | Alias for `UPSTASH_TOKEN` |
| `KEY_PREFIX` | `royells` | unique deployment prefix | Namespace |
| `QUEUE_PREFIX` | `queue` | `queue` | Queue namespace |
| `REDIS_NAMESPACE_VERSION` | `v1` | `v1` | Schema version |
| `REDIS_MAX_CONNECTIONS` | `20` | `20` | TCP pool |
| `REDIS_SOCKET_TIMEOUT` | `5` | `5` | Socket timeout |
| `REDIS_CONNECT_TIMEOUT` | `5` | `5` | Connect timeout |
| `REDIS_POOL_WAIT_TIMEOUT` | `5` | `5` | Pool wait |
| `REDIS_HEALTH_CHECK_INTERVAL` | `30` | `30` | Connection health |
| `REDIS_RETRY_ATTEMPTS` | `3` | `3` | Retry |
| `REDIS_RETRY_BASE_SECONDS` | `0.25` | `0.25` | Retry base |
| `REDIS_RETRY_CAP_SECONDS` | `5` | `5` | Retry cap |
| `REDIS_MAX_PAYLOAD_BYTES` | `1048576` | `1048576` | Serialization limit |
| `REDIS_QUEUE_MAX_ATTEMPTS` | `8` | `8` | Future queue attempts |
| `REDIS_QUEUE_VISIBILITY_TIMEOUT` | `300` | `300` | Future reservation timeout |
| `REDIS_QUEUE_RECOVERY_BATCH` | `100` | `100` | Future recovery batch |
| `REDIS_QUEUE_DEDUPE_TTL_SECONDS` | `604800` | `604800` | Future dedupe TTL |
| `REDIS_LOCK_TTL_MS` | `30000` | `30000` | Lease TTL |
| `REDIS_LOCK_WAIT_TIMEOUT_MS` | `5000` | `5000` | Lock wait |
| `REDIS_HEARTBEAT_TTL_SECONDS` | `120` | `120` | Heartbeat TTL |
| `REDIS_PROCESSING_TTL_SECONDS` | `86400` | `86400` | Processing state TTL |
| `REDIS_CHECKPOINT_TTL_SECONDS` | `0` | `0` | Checkpoint persistence |
| `REDIS_METRICS_TTL_SECONDS` | `604800` | `604800` | Metrics retention |
| `ENABLE_REDIS_PUBSUB` | `0` | `0` | Optional events |

## Active Runtime Variables

Every item in the following tables is optional, defaults when missing, belongs in HF Variables rather than Secrets, and replaces no old variable unless specifically noted.

### Workers, Queues, and Executors

| Variable | Source default | Shipped/recommended HF value |
|---|---:|---:|
| `ROYELLS_BOT_HANDLER_WORKERS` | `2` on HF | `2` |
| `ROYELLS_DOWNLOAD_WORKERS` | `5` | `5` |
| `ROYELLS_UPLOAD_WORKERS` | `5` | `5` |
| `ROYELLS_LINK_WORKERS` | `1` | `1` |
| `ROYELLS_BUTTON_WORKERS` | `3` on HF | `5` |
| `ROYELLS_ALLOW_AGGRESSIVE_MAIN_WORKERS` | `1` | `1` |
| `ROYELLS_MAIN_LOCAL_QUEUE_SOFT_LIMIT` | `1500` | `1500` |
| `ROYELLS_MAIN_LOCAL_QUEUE_HARD_LIMIT` | `2500` | `2500` |
| `ROYELLS_DOWNLOAD_QUEUE_LIMIT` | hard limit | `2500` |
| `ROYELLS_UPLOAD_QUEUE_LIMIT` | hard limit | `2500` |
| `ROYELLS_BUTTON_QUEUE_LIMIT` | `200` | `200` |
| `ROYELLS_LINK_QUEUE_LIMIT` | `100` | `100` |
| `ROYELLS_RETRY_QUEUE_LIMIT` | `5000` | `5000` |
| `ROYELLS_JOB_STATE_DB_QUEUE_LIMIT` | `5000` | `5000` |
| `ROYELLS_CRITICAL_DB_WRITE_QUEUE_LIMIT` | `5000` | `5000` |
| `ROYELLS_DB_WRITE_BATCH_SIZE` | `100` | `100` |
| `ROYELLS_QUEUE_ADMISSION_MAX_WAIT_SECONDS` | `20` | `20` |
| `ROYELLS_DB_EXECUTOR_WORKERS` | `1` | `1` |
| `ROYELLS_PERSISTENCE_EXECUTOR_WORKERS` | `2` | `2` |
| `ROYELLS_MEDIA_EXECUTOR_WORKERS` | `2` | `2` |
| `ROYELLS_CONTROL_EXECUTOR_WORKERS` | `2` | `2` |
| `ROYELLS_CPU_EXECUTOR_WORKERS` | `1` | `1` |
| `ROYELLS_CACHE_LIMIT` | `5000` | `5000` |
| `ROYELLS_PROCESSING_CACHE_TTL_SECONDS` | `86400` | `86400` |

### Telegram, Retry, Album, and Media

| Variable | Source default | Shipped/recommended HF value |
|---|---:|---:|
| `ROYELLS_POST_DELAY` | `1` on HF | `0` only after rate-limit testing |
| `ROYELLS_ALBUM_WAIT` | `5` | `5` |
| `ROYELLS_MAX_RETRIES` | `3` | `3` |
| `ROYELLS_SOURCE_JOB_MAX_RETRIES` | `8` on HF | Docker `5`; recommend `5-8` |
| `ROYELLS_SOURCE_PERMANENT_RETRY` | `1` | Docker `0` |
| `ROYELLS_SOURCE_FAILED_RETRY_DELAY_SECONDS` | `600` | `600` |
| `ROYELLS_TELEGRAM_API_CONCURRENCY` | `1` | `4` after staging confirmation |
| `ROYELLS_TELEGRAM_MEDIA_CONCURRENCY` | `2` | `2` |
| `ROYELLS_MAX_CONCURRENT_TRANSMISSIONS` | `1` on HF | Docker `4`; monitor FloodWait/memory |
| `ROYELLS_TELEGRAM_CALL_RETRIES` | `5` | `5` |
| `ROYELLS_TELEGRAM_CALL_TIMEOUT_SECONDS` | `90` on HF | `90` |
| `ROYELLS_TELEGRAM_CONTROL_TIMEOUT_SECONDS` | `15` | `15` |
| `ROYELLS_TELEGRAM_CONTROL_RETRIES` | `2` | `2` |
| `ROYELLS_TELEGRAM_CONTROL_CONCURRENCY` | `4` | `4` |
| `ROYELLS_TELEGRAM_MEDIA_CALL_HARD_TIMEOUT_SECONDS` | `300` on HF | `300` |
| `ROYELLS_TELEGRAM_RECONNECT_COOLDOWN_SECONDS` | `300` on HF | `300` |
| `ROYELLS_TELEGRAM_RECONNECT_DEFER_ON_PIPELINE` | `0` | `0` |
| `ROYELLS_TELEGRAM_RECONNECT_DEFER_MAX_SECONDS` | `300` | `300` |
| `ROYELLS_FLOOD_WAIT_BACKOFF_MULTIPLIER` | `1.5` | `1.5` |
| `ROYELLS_FLOOD_WAIT_JITTER_SECONDS` | `3` | `3` |
| `ROYELLS_FLOOD_WAIT_BACKOFF_CAP_SECONDS` | `900` | `900` |
| `ROYELLS_DOWNLOAD_MEDIA_TIMEOUT_SECONDS` | `420` on HF | `420` |
| `ROYELLS_DOWNLOAD_REFRESH_BEFORE_EACH_ITEM` | `0` | `0` |
| `ROYELLS_DOWNLOAD_ITEM_API_RETRIES` | `2` | `2` |
| `ROYELLS_UPLOAD_PREPARE_TIMEOUT_SECONDS` | `180` on HF | `180` |
| `ROYELLS_UPLOAD_SEND_TIMEOUT_SECONDS` | `300` on HF | `300` |
| `ROYELLS_ZERO_BYTE_RETRY_SECONDS` | `8` | `8` |
| `ROYELLS_ZERO_BYTE_ITEM_MAX_RETRIES` | `3` | `3` |
| `ROYELLS_PRESERVE_ALBUMS` | `1` | `1` |
| `ROYELLS_TRY_COPY_MEDIA_GROUP` | `1` | `1` |
| `ROYELLS_TRY_COPY_ALBUM_ITEMS` | `0` | `0` |
| `ROYELLS_SOURCE_ALBUM_INDIVIDUAL_FALLBACK` | `1` | `1` |
| `ROYELLS_SOURCE_ALBUM_GROUP_FAILURE_INDIVIDUAL_FALLBACK` | `1` | `1` |
| `ROYELLS_SOURCE_ALBUM_SEND_ATTEMPTS` | `2` | `2` |
| `ROYELLS_COPY_MESSAGE_ITEM_DELAY` | `0.15` | Docker `0.05` |
| `ROYELLS_COPY_RESTRICTED_CACHE_TTL_SECONDS` | `21600` | `21600` |
| `ROYELLS_TARGET_MEDIA_WITH_BOT` | `1` | `1` |
| `ROYELLS_TARGET_MEDIA_WITH_USERBOT` | `1` | `1` |
| `ROYELLS_TELEGRAM_LINK_FORCE_UPLOAD` | `1` | `1` |
| `ROYELLS_TELEGRAM_LINK_FORCE_UPLOAD_FOR_ALL` | `0` | `0` |
| `ROYELLS_SESSION_AUTH_PAUSE_SECONDS` | `300` | `300` |
| `ROYELLS_BOT_SESSION` | `royells_bot` | keep |
| `ROYELLS_USER_SESSION` | `royells_user` | keep; name only when string Secret is used |

### Intake, Scan, Source Guard, and Auto Sync

| Variable | Source default | Shipped/recommended HF value |
|---|---:|---:|
| `ROYELLS_ADAPTIVE_INTAKE` | `1` | `1` |
| `ROYELLS_ADAPTIVE_INTAKE_START_DELAY_SECONDS` | `45` | `15` |
| `ROYELLS_ADAPTIVE_INTAKE_INTERVAL_SECONDS` | `20` | `5` |
| `ROYELLS_ADAPTIVE_INTAKE_TIMEOUT_SECONDS` | `120` | `90` |
| `ROYELLS_STARTUP_HOT_SCAN_LIMIT` | `500` | `500` |
| `ROYELLS_STARTUP_HOT_SCAN_HISTORY_LIMIT` | `2500` | `2500` |
| `ROYELLS_STARTUP_HOT_SCAN_NEW_PASS_TOKEN` | empty | leave empty unless deliberately starting a new pass |
| `ROYELLS_HISTORICAL_BACKFILL` | `1` | `1` |
| `ROYELLS_HISTORICAL_BACKFILL_BATCH_IDS` | `100` | `100` |
| `ROYELLS_HISTORICAL_BACKFILL_PRESSURE_TARGET` | `1200` | Docker `100` |
| `ROYELLS_HISTORICAL_BACKFILL_PAUSE_SECONDS` | `5` | `5` |
| `ROYELLS_SCAN_PREFETCH_PRESSURE_TARGET` | `100` | `100` |
| `ROYELLS_STARTUP_CATCHUP` | `1` | Docker `0` |
| `ROYELLS_STARTUP_CATCHUP_MEDIA_LIMIT` | `200` | `200` |
| `ROYELLS_STARTUP_CATCHUP_HISTORY_LIMIT` | dynamic, min `1200` | keep default |
| `ROYELLS_STARTUP_CATCHUP_CHANNEL_TIMEOUT_SECONDS` | `180` | `180` |
| `ROYELLS_STARTUP_CATCHUP_DELAY_SECONDS` | `120` on HF | `120` |
| `ROYELLS_IMMEDIATE_RESCUE_ENABLED` | `0` | `0` |
| `ROYELLS_IMMEDIATE_RESCUE_SCAN_LIMIT` | `200` | `200` |
| `ROYELLS_IMMEDIATE_RESCUE_HISTORY_LIMIT` | dynamic, min `1200` | keep default |
| `ROYELLS_IMMEDIATE_RESCUE_SCAN_DELAY_SECONDS` | `20` | `20` |
| `ROYELLS_AUTO_SYNC` | `1` | Docker `0`; enable only if adaptive intake is disabled |
| `ROYELLS_AUTO_SYNC_LIMIT` | `100` on HF | `100` |
| `ROYELLS_AUTO_SYNC_INTERVAL_SECONDS` | `1800` | `1800` |
| `ROYELLS_AUTO_SYNC_START_DELAY_SECONDS` | `900` | `900` |
| `ROYELLS_AUTO_SYNC_MAX_QUEUE` | `10` on HF | `10` |
| `ROYELLS_AUTO_SYNC_CHANNEL_TIMEOUT_SECONDS` | `120` | `120` |
| `ROYELLS_AUTO_SYNC_CHANNEL_CONCURRENCY` | `1` | `1` |
| `ROYELLS_AUTO_SYNC_CHANNELS_PER_RUN` | `4` on HF | `4` |
| `ROYELLS_AUTO_SYNC_HEALTH_CHECK` | `0` on HF | `0` |
| `ROYELLS_SOURCE_GUARD` | `1` | Docker `0`; enable only if adaptive intake is disabled |
| `ROYELLS_SOURCE_GUARD_INTERVAL_SECONDS` | `20` on HF | `20` |
| `ROYELLS_SOURCE_GUARD_START_DELAY_SECONDS` | `60` | `60` |
| `ROYELLS_SOURCE_GUARD_LOOKBACK` | `20` on HF | `20` |
| `ROYELLS_SOURCE_GUARD_CATCHUP_LIMIT` | `500` | `500` |
| `ROYELLS_SOURCE_GUARD_CHANNEL_TIMEOUT_SECONDS` | `120` on HF | `120` |
| `ROYELLS_SOURCE_GUARD_CHANNELS_PER_TICK` | `1` | `1` |
| `ROYELLS_SOURCE_GUARD_BACKOFF_MAX_SECONDS` | `300` | `300` |
| `ROYELLS_SOURCE_GUARD_ACCESS_BACKOFF_SECONDS` | `3600` | `3600` |
| `ROYELLS_SOURCE_GUARD_TIMEOUT_RECONNECT` | `0` | `0` |
| `ROYELLS_SOURCE_GUARD_PAUSE_ON_LOCAL_PRESSURE` | `1` | `1` |
| `ROYELLS_SOURCE_GUARD_PRESSURE_SLEEP_SECONDS` | `20` on HF | `20` |
| `ROYELLS_SOURCE_PRESSURE_OFFLOAD` | `1` | `1` |
| `ROYELLS_STARTUP_SCAN_LIMIT` | `300` on HF | `300` |
| `ROYELLS_GUARD_STALE_SECONDS` | `900` | `900` |
| `ROYELLS_WATCHDOG_RESCUE_SYNC_SECONDS` | `900` | `900` |

### Source Health, Links, Subscription, and Owner Controls

| Variable | Default | Recommended |
|---|---:|---:|
| `ROYELLS_SOURCE_LINK_REPORT_CHAT_ID` | project-specific `-100...` | set to your own report channel |
| `ROYELLS_SUPPORT_CHAT_ID` | report chat | set to your own support channel |
| `ROYELLS_JOIN_LINK` | empty | target join URL if required |
| `ROYELLS_SUPPORT_COOLDOWN_SECONDS` | `1800` | `1800` |
| `ROYELLS_SUPPORT_TICKET_TTL_SECONDS` | `86400` | `86400` |
| `ROYELLS_SUPPORT_DELETE_INCOMING` | `1` | `1` |
| `ROYELLS_OWNER_STATE_TTL_SECONDS` | `300` | `300` |
| `ROYELLS_ADD_CHANNEL_RESOLVE_TIMEOUT_SECONDS` | `90` | `90` |
| `ROYELLS_MAX_BULK_SOURCE_CHANNELS` | `100` | `100` |
| `ROYELLS_BULK_SOURCE_RESOLVE_TIMEOUT_SECONDS` | `45` | `45` |
| `ROYELLS_CHANNEL_HEALTH_TIMEOUT_SECONDS` | `35` | `35` |
| `ROYELLS_CHANNEL_HEALTH_START_DELAY_SECONDS` | `600` | `600` |
| `ROYELLS_CHANNEL_AUTO_RETIRE_DELETED_SOURCES` | `1` | `1` |
| `ROYELLS_CHANNEL_HARD_DELETE_CONFIRMATIONS` | `2` | `2` |
| `ROYELLS_CHANNEL_SOFT_DELETE_CONFIRMATIONS` | `12` | `12` |
| `ROYELLS_CHANNEL_RETIRE_ON_SOFT_MISSING` | `0` | `0` |
| `ROYELLS_CHANNEL_RETIRE_PEER_INVALID_AFTER` | `2` | `2` |
| `ROYELLS_CHANNEL_RETIRE_ON_PEER_INVALID` | `0` | `0` |
| `ROYELLS_CHANNEL_PEER_INVALID_BACKOFF_SECONDS` | `21600` | `21600` |
| `ROYELLS_RETIRE_ACCESS_FAILURES` | `0` | `0` |
| `ROYELLS_SOURCE_MAX_FAILURES_BEFORE_RETIRE` | `8` | `8` |
| `ROYELLS_SOURCE_CIRCUIT_BREAKER` | `1` | `1` |
| `ROYELLS_SOURCE_CIRCUIT_FAILURE_THRESHOLD` | `3` | `3` |
| `ROYELLS_SOURCE_CIRCUIT_COOLDOWN_SECONDS` | `3600` | `3600` |
| `ROYELLS_SOURCE_CIRCUIT_MAX_COOLDOWN_SECONDS` | `21600` | `21600` |
| `ROYELLS_SOURCE_LINK_DISCOVERY` | `1` | `1` |
| `ROYELLS_SOURCE_LINK_LIVE_CHECK` | `1` | `1` |
| `ROYELLS_SOURCE_LINK_CHECK_CACHE_SECONDS` | `21600` | `21600` |
| `ROYELLS_SOURCE_PEER_RESOLVE_COOLDOWN_SECONDS` | `1800` | `1800` |
| `ROYELLS_SUB_PROFILE_CACHE_SECONDS` | `900` | `900` |
| `ROYELLS_SUB_PROFILE_REFRESH_LIMIT` | `60` | `60` |
| `ROYELLS_MEMBER_FETCH_TIMEOUT_SECONDS` | `45` | `45` |
| `ROYELLS_MEMBER_FETCH_LIMIT` | `500` | `500` |

### SQLite, Persistence, Recovery, and Backup

| Variable | Source default | Shipped/recommended HF value |
|---|---:|---:|
| `ROYELLS_DATA_DIR` | `/data/royells_media_bot` | `/data/royells_media_bot` with attached persistence |
| `ROYELLS_RUNTIME_DIR` | data `/runtime` | `/data/royells_media_bot/runtime` |
| `ROYELLS_FOLDER_NAME` | `royells_media_bot` | keep |
| `ROYELLS_DB_TIMEOUT_SECONDS` | `30` | `30` |
| `ROYELLS_DB_JOURNAL_MODE` | `DELETE` on HF | `DELETE` |
| `ROYELLS_DB_SYNCHRONOUS` | `NORMAL` | `NORMAL`; `FULL` for stronger durability at lower throughput |
| `ROYELLS_DB_OPEN_RETRY_ATTEMPTS` | `6` | `8` |
| `ROYELLS_DB_OPEN_RETRY_BASE_SECONDS` | `0.25` | `0.35` |
| `ROYELLS_DB_STARTUP_READY_ATTEMPTS` | `10` | `12` |
| `ROYELLS_DB_STARTUP_READY_DELAY_SECONDS` | `1.5` | `1.5` |
| `ROYELLS_DB_OPEN_STARTUP_SIDECAR_REPAIR` | `1` | `1` |
| `ROYELLS_DB_HEALTH_CHECK_INTERVAL_SECONDS` | `120` | `120` |
| `ROYELLS_DB_ONLINE_RECOVERY_COOLDOWN_SECONDS` | `120` | `120` |
| `ROYELLS_DB_RECOVERY_ARTIFACT_KEEP` | `3` | `3` |
| `ROYELLS_JSON_FSYNC` | source `0` | Docker `1` |
| `ROYELLS_RUNTIME_CHECKPOINT` | `1` | `1` |
| `ROYELLS_RUNTIME_CHECKPOINT_INTERVAL_SECONDS` | `15` | `15` |
| `ROYELLS_RUNTIME_CHECKPOINT_CONFIRM_HISTORY_LIMIT` | `1000` | `1000` |
| `ROYELLS_RUNTIME_CHECKPOINT_MAX_AGE_DAYS` | `30` | `30` |
| `ROYELLS_DELIVERY_AMBIGUITY_GRACE_SECONDS` | `120` | `120` |
| `ROYELLS_QUEUE_RECOVERY_BATCH_LIMIT` | `50` | `50` |
| `ROYELLS_QUEUE_RECOVERY_PRESSURE_TARGET` | `100` | `100` |
| `ROYELLS_QUEUE_RECOVERY_ITEM_DELAY_SECONDS` | `0.1` | `0.1` |
| `ROYELLS_QUEUE_RECOVERY_RETRY_SECONDS` | `3` | `3` |
| `ROYELLS_QUEUE_HEALER` | `1` | `1` |
| `ROYELLS_QUEUE_HEALER_INTERVAL_SECONDS` | `300` | `300` |
| `ROYELLS_QUEUE_STALE_SECONDS` | `900` | `900` |
| `ROYELLS_AUTO_RECOVERY` | `1` | Docker `0`; queue healer remains active |
| `ROYELLS_AUTO_RECOVERY_INTERVAL_SECONDS` | `900` | `900` |
| `ROYELLS_AUTO_RECOVERY_START_DELAY_SECONDS` | `900` | `900` |
| `ROYELLS_AUTO_RECOVERY_LIMIT` | `150` | `150` |
| `ROYELLS_STATE_BACKUP_INTERVAL_SECONDS` | `900` | `900` |
| `ROYELLS_STATE_BACKUP_KEEP` | `3` | `3` |
| `ROYELLS_BACKUP_ZIP_COMPRESSLEVEL` | `9` | `9` |
| `ROYELLS_TELEGRAM_BACKUP` | `1` | set `0` unless owner wants automatic backup documents |
| `ROYELLS_TELEGRAM_BACKUP_INTERVAL_SECONDS` | `86400` | `86400` |
| `ROYELLS_RESTORE_RELOAD_TARGET_INDEX` | `1` | `1` |
| `ROYELLS_RESTORE_MAX_MEMBERS` | `500` | `500` |
| `ROYELLS_RESTORE_MAX_UNCOMPRESSED_MB` | `2048` | size to actual backup envelope |
| `ROYELLS_RESTORE_MAX_COMPRESSION_RATIO` | `200` | `200` |
| `ROYELLS_RESTORE_MAX_JSON_MB` | `256` | `256` |
| `ROYELLS_PURGE_RESTORED_SESSION_FILES` | `1` | `1` when env session is authoritative |
| `ROYELLS_PURGE_BOT_SESSION_FILES` | `0` | `0` |

### Storage, Memory, Watchdogs, Diagnostics, and Dashboard

| Variable | Source default | Shipped/recommended HF value |
|---|---:|---:|
| `ROYELLS_DOWNLOAD_STORAGE_LIMIT_MB` | `20480` on HF | lower than attached storage minus safety reserve |
| `ROYELLS_MIN_FREE_STORAGE_MB` | `2048` on HF | `2048` |
| `ROYELLS_STORAGE_MONITOR_INTERVAL_SECONDS` | `300` | `300` |
| `ROYELLS_MEMORY_GC_INTERVAL_SECONDS` | `300` | `300` |
| `ROYELLS_HARD_WATCHDOG` | `1` | `1` |
| `ROYELLS_HARD_WATCHDOG_FORCE_RESTART` | `0` | `0` |
| `ROYELLS_HARD_WATCHDOG_HEARTBEAT_SECONDS` | `15` | `15` |
| `ROYELLS_HARD_WATCHDOG_STALL_SECONDS` | `900` on HF | `900` |
| `ROYELLS_HARD_WATCHDOG_STARTUP_GRACE_SECONDS` | `600` | `600` |
| `ROYELLS_HARD_WATCHDOG_EXIT_CODE` | `75` | `75` |
| `ROYELLS_MEMORY_RESTART_LIMIT_MB` | `12000` on HF | Docker `0` |
| `ROYELLS_WORKER_STALL_SECONDS` | `900` on HF | `900` |
| `ROYELLS_WORKER_STALL_FORCE_RESTART` | `0` | `0` |
| `ROYELLS_AUTO_RESTART_MAX_PER_WINDOW` | `0` | `0` |
| `ROYELLS_AUTO_RESTART_WINDOW_SECONDS` | `21600` | `21600` |
| `ROYELLS_WATCHDOG_CHECK_SECONDS` | `300` | `300` |
| `ROYELLS_ASYNC_DIAGNOSTICS` | `0` | `0`; enable temporarily only |
| `ROYELLS_SLOW_CALLBACK_SECONDS` | `1` | `1` |
| `ROYELLS_EVENT_LOOP_LAG_LOG_SECONDS` | `1` | `1` |
| `ROYELLS_SLOW_DB_SECONDS` | `0.5` | `0.5` |
| `ROYELLS_SLOW_API_SECONDS` | `5` | `5` |
| `ROYELLS_SLOW_MEDIA_SECONDS` | `15` | `15` |
| `ROYELLS_SLOW_QUEUE_WAIT_SECONDS` | `5` | `5` |
| `ROYELLS_SLOW_LOCK_SECONDS` | `0.25` | `0.25` |
| `ROYELLS_SLOW_LOG_RATE_LIMIT_SECONDS` | `60` | `60` |
| `ROYELLS_DIAGNOSTIC_REPORT_ENABLED` | `1` | `1` |
| `ROYELLS_DIAGNOSTIC_REPORT_INTERVAL_SECONDS` | `86400` | `86400` |
| `ROYELLS_LOG_MAX_MB` | `20` | `20` |
| `ROYELLS_LOG_BACKUP_COUNT` | `5` | `5` |
| `ROYELLS_LARGE_STATE_SAVE_MIN_INTERVAL_SECONDS` | `90` | `90` |
| `ROYELLS_LIVE_DASHBOARD` | `1` | `1` |
| `ROYELLS_LIVE_DASHBOARD_INTERVAL_SECONDS` | `10` | Docker `15` |
| `ROYELLS_KEEPALIVE_HTTP` | app forces `0` | `0`; `app.py` owns public port |
| `ROYELLS_FORCE_INTERNAL_HTTP` | empty/off | off |
| `ROYELLS_KEEPALIVE_PORT` | `7860` on HF | unused while internal HTTP off |
| `ROYELLS_KEEPALIVE_PING_URLS` | empty | optional comma-separated outbound pings |
| `ROYELLS_KEEPALIVE_URLS` | empty | legacy alias |
| `ROYELLS_KEEPALIVE_PING_INTERVAL_SECONDS` | `240` | `240` |
| `ROYELLS_DIALOG_REFRESH_INTERVAL_SECONDS` | `900` | `900` |

### Index, Filtering, Media Conversion, and Cleanup

| Variable | Default | Recommended |
|---|---:|---:|
| `ROYELLS_TARGET_MEDIA_INDEX` | `1` | `1` |
| `ROYELLS_TARGET_MEDIA_INDEX_START_DELAY_SECONDS` | `1800` on HF | Docker `7200` |
| `ROYELLS_TARGET_MEDIA_INDEX_INTERVAL_SECONDS` | `86400` | `86400` |
| `ROYELLS_TARGET_MEDIA_INDEX_PAGE_LIMIT` | deep-clean page limit | keep |
| `ROYELLS_TARGET_MEDIA_INDEX_MAX_HISTORY` | `0` unlimited | `0` if storage/API budget allows |
| `ROYELLS_TARGET_MEDIA_INDEX_DELETE_DUPLICATES` | `1` | `1` with owner approval |
| `ROYELLS_DEAD_MEDIA_FAILURE_THRESHOLD` | `2` | `2` |
| `ROYELLS_DEAD_MEDIA_SKIP_AFTER_ATTEMPTS` | `2` | `2` |
| `ROYELLS_DEEP_CLEAN_DELETE_BATCH` | `10` | `10` |
| `ROYELLS_DEEP_CLEAN_HISTORY_PAGE_LIMIT` | `50` | `50` |
| `ROYELLS_FORCE_SOURCE_VIDEO_FIX` | `0` on HF | `0` |
| `ROYELLS_VIDEO_FIX_PRESET` | `ultrafast` on HF | `ultrafast` |
| `ROYELLS_VIDEO_FIX_CRF` | `20` on HF | `20` |
| `ROYELLS_FFMPEG_THREADS` | `1` on HF | `1` |
| `ROYELLS_FFMPEG_CONVERSION_TIMEOUT_SECONDS` | `1800` | `1800` |
| `ROYELLS_VIDEO_META_PROBE_TIMEOUT_SECONDS` | `6` on HF | `6` |
| `ROYELLS_CONTENT_FILTER_THRESHOLD` | `12` | dormant while filter disabled |
| `ROYELLS_CONTENT_FILTER_VIDEO_FRAME_SECONDS` | `1` | dormant |
| `ROYELLS_CONTENT_FILTER_HASH_CACHE_SECONDS` | `30` | dormant |
| `ROYELLS_TELEGRAM_LINK_BYPASS_CONTENT_FILTER` | `1` | dormant |

## Ignored, Dormant, and Test-Only Variables

| Variable | Status |
|---|---|
| `ROYELLS_TRY_COPY_MESSAGE` | Docker-only ignored; code hardcodes `TRY_COPY_MESSAGE=True` |
| `ROYELLS_SOURCE_FAST_COPY` | Docker-only ignored; code hardcodes `SOURCE_FAST_COPY_ENABLED=True` |
| `ROYELLS_CONTENT_FILTER_ENABLED` | Docker-only ignored; code hardcodes `CONTENT_FILTER_ENABLED=False` |
| `ROYELLS_BRAIN_DISCOVERY` | Mentioned by dormant logging; no active env read |
| `ROYELLS_SOURCE_LINK_JOIN_PROBE` | Mentioned in UI text; feature hardcoded off |
| `ROYELLS_WORKER_HTTP_URLS` | Mentioned in old UI text; split-worker mode hardcoded off |
| `ROYELLS_TEST_POSTGRES_URL` | Test-only live integration credential |
| `ROYELLS_TEST_REDIS_URL` | Test-only live integration credential |

# Part 2 - Requirements

## Dependency Verification

All production third-party imports are represented:

- `pyrogram` -> `Pyrogram`
- `tgcrypto` runtime acceleration -> `TgCrypto`
- `requests` -> `requests`
- `PIL` -> `Pillow`
- `imagehash` -> `ImageHash`
- `psycopg`, `psycopg_pool` -> `psycopg[binary,pool]`
- `redis`, hiredis acceleration -> `redis[hiredis]`

No required production package is missing.

PostgreSQL and Redis dependencies are not used by the default authority, but shipped modules import them and Docker compiles those modules, so retaining them is correct for this artifact. Removing them would require excluding the optional adapter packages.

Development-only dependencies correctly live in `requirements-dev.txt`: pytest, fakeredis, Ruff.

## Final requirements.txt

```text
Pyrogram>=2.0.106,<3
TgCrypto>=1.2.5
requests>=2.31.0
Pillow>=10.0.0
ImageHash>=4.3.1
psycopg[binary,pool]>=3.2,<4
redis[hiredis]>=7.4,<9
```

Recommendation: keep this source file but generate a tested constraints/lock file for reproducible production rebuilds. Do not tighten versions without staging the Telegram/media pipeline.

# Part 3 - Database

## Active SQLite Schema

`init_db()` creates or upgrades:

- `posted`
- `channels`
- `subscriptions`
- `target_media`
- `target_media_full_index`
- `dead_media`
- `content_filter_hashes`
- `media_job_state`
- `runtime_health`
- `v20_audit_events` on first AuditRepository use

Indexes:

- `idx_channels_username`
- `idx_subscriptions_expire`
- `idx_subscriptions_username`
- `idx_dead_media_source`
- `idx_content_filter_type`
- `idx_media_job_state_status`
- `idx_media_job_state_source`

Schema evidence: `royells_media_bot_ready.py:10547-10670`; V20 audit table: `royells_v20_core/repositories/legacy.py:659`.

## Compatibility Result

- Old `royells.db` compatibility: **Yes**, provided integrity checks pass.
- Default migration required: **No**.
- Data loss expected: **No**, because tables use `IF NOT EXISTS` and missing legacy columns are added.
- Reuse old DB: **Yes**, with its JSON state and ledger files.
- Backup first: **Mandatory**.
- Delete old DB: **No**.
- Delete queue/checkpoint JSON: **No**.

Startup repair validates integrity, quarantines corruption, accepts only verified backups, attempts row salvage, and rehydrates from JSON/ledger. This materially reduces corruption impact but cannot make unsafe storage or abrupt filesystem loss impossible.

PostgreSQL migrations under `migrations/postgres/` are optional infrastructure. They must not be applied for the default SQLite deployment.

# Part 4 - File Structure

## Files Included in Docker

| Path | Purpose | Status |
|---|---|---|
| `README.md` | HF Space metadata and operations guide | Required |
| `Dockerfile` | Python 3.11 image, ffmpeg, non-root app user, defaults, startup | Required |
| `app.py` | Public status server and main-process wrapper | Required; health hardening needed |
| `requirements.txt` | Production dependencies | Required |
| `royells_media_bot_ready.py` | Legacy-compatible production bot and active runtime | Required |
| `royells_v20_core/` | Interfaces, configuration, DI, repositories, health, worker supervisor | Required |
| `royells_v20_postgres/` | Disabled PostgreSQL infrastructure | Required only while shipping Book 18 |
| `royells_v20_redis/` | Disabled Redis infrastructure | Required only while shipping Book 19 |
| `migrations/postgres/` | Disabled future PostgreSQL schema | Required only for adapter validation |

## Core Package Map

- `interfaces.py`: backend-neutral contracts.
- `models.py`: authority, queue, recovery, worker, and health models.
- `configuration/`: typed environment parsing and migration-safety validation.
- `container.py`: production composition root and service container.
- `authority.py`: immutable authority policy.
- `lifecycle.py`: shutdown/restart request state.
- `health.py`: liveness/readiness/startup snapshots.
- `workers.py`, `worker_contracts.py`: injected wrappers and supervision.
- `repositories/contracts.py`, `repositories/legacy.py`: repository interfaces and compatibility implementations.
- `adapters/legacy_bot.py`: bridge to current SQLite/JSON/queues/recovery.
- `adapters/sqlite_database.py`, `sqlite_queue.py`, `json_runtime.py`: isolated Book 17 adapters/tests.
- `adapters/local_storage.py`: path-constrained local temporary storage.
- `adapters/pyrogram_telegram.py`: Telegram interface wrapper.
- `migration.py`: journal and disabled dual-write coordinator.

## Non-Deploy Clutter

The Dockerfile does not copy:

- `tests/`
- `requirements-dev.txt`
- old `*.zip` release files
- old `PHASE17_*` and `PRODUCTION_*` reports
- `__pycache__/`

These should still be removed from the Space repository/deployment context or moved to release storage. No production file was renamed by the current Docker build.

# Part 5 - Configuration

## Active Production Profile

- SQLite authority: enabled.
- JSON runtime/checkpoint: enabled.
- PostgreSQL: disabled.
- Redis: disabled.
- Dual read/write: disabled.
- Download/upload/button/link workers: `5/5/5/1`.
- Telegram API/media concurrency: `4/2`.
- Pyrogram max concurrent transmissions: `4`.
- Queue soft/hard: `1500/2500`.
- Adaptive intake, Hot-500, historical backfill: enabled.
- Startup catchup, immediate rescue, Auto Sync, Auto Recovery, Source Guard: disabled.
- Queue healer and DB guards: enabled.
- Process auto-restart: permanently suppressed in code.
- Worker coroutine restart: enabled through `TaskSupervisor`.
- Dashboard refresh: enabled, 15 seconds.
- SQLite: `DELETE`, `NORMAL`, JSON fsync enabled.

## Incorrect or Misleading Configuration

1. `ROYELLS_USER_SESSION_STRING` is documented required but not fail-fast validated.
2. Source Guard and Auto Sync are disabled while startup owner text says source guard is active.
3. Three legacy Docker variables are ignored.
4. V20 boolean flags imply optionality that the production composition root does not implement.
5. Optional backend tuning providers are bypassed by production composition.

# Part 6 - Hugging Face Deployment

## Required Root

Upload exactly the Docker-included files/directories listed in Part 4. Exclude old ZIPs, caches, local session files, logs, and local databases.

## Space Settings

1. SDK: Docker.
2. App port: 7860.
3. Attach persistent storage before first production start.
4. Confirm `/data` is mounted and writable.
5. Add the six required Secrets.
6. Keep PostgreSQL/Redis Secrets absent.
7. Use a private Space or protect non-minimal health endpoints.
8. Use Factory Rebuild for the first v20 image replacement.

## Persistence Layout

```text
/data/royells_media_bot/
  backups/
  runtime/
    royells.db
    state/
    downloads/
    runtime_checkpoint.json
    runtime_checkpoint.previous.json
    delivery_intents.json
    delivery_intents.previous.json
    media_delivery_ledger.jsonl
```

Actual state locations are source-defined and may include additional JSON files. Preserve the entire data root.

## Build and Startup

- Build: Docker installs ffmpeg and `requirements.txt`, then runs `compileall`.
- Start command: `python -u app.py`.
- Public listener: `0.0.0.0:7860`.
- Main bot runs in the main process; HTTP server runs in a daemon thread.
- Code-level automatic restart is disabled.
- Intentional owner restart exits with lifecycle status so the platform can restart.
- A fatal main-loop error leaves the HTTP server alive in `restart circuit open`; manual investigation/restart is required.

HF free hardware is not a guaranteed 24/7 worker tier. Free Spaces can sleep. Ephemeral disk is not durable. Attached persistent storage is mandatory for this stateful design.

# Part 7 - Startup Validation

## Expected Log Sequence

Expected high-signal lines:

1. `[ROYELLS DOCKER] command reached; starting app.py`
2. `[ROYELLS BOOT ...] python process started`
3. Third-party imports and runtime folders ready
4. Telegram client objects created
5. `app.py wrapper reached`
6. `[ROYELLS APP] status server ready on port 7860`
7. `Starting Royells Bot v20.0.0`
8. `Deploy target: Hugging Face Space`
9. Data/runtime paths under `/data/royells_media_bot`
10. `Workers: bot=2, button=5, download=5, upload=5, link=1, api_concurrency=4, media_concurrency=2`
11. `Royells v20 composition ready` with legacy SQLite/JSON authority
12. DB integrity/read-write readiness and DB counts
13. Runtime checkpoint/delivery intent load status
14. Queue reservation and Hot-500 durable progress
15. Loaded target/dead indexes
16. Userbot and bot start
17. Peer warmup result
18. `Bot and userbot started`
19. `Crash recovery is active`
20. Diagnostic-only hard watchdog
21. Supervised task starts
22. Queue recovery completion/barrier status
23. Owner startup ping

Expected supervised task count with shipped defaults is approximately 38, including 5 download, 5 upload, 5 button, 1 link, DB writers/guards, checkpoint, retry, recovery, cleanup, backup, monitoring, Hot-500, adaptive intake, and historical backfill tasks.

Initial queue values depend on recovered state; they are not required to be zero. Existing jobs should appear as reserved/recovered rather than being rebuilt from source scans.

Observed historical baseline in supplied logs was roughly 374-457 MB. Treat approximately 350-700 MB after warmup as a reasonable operational baseline, not a source-code guarantee. Investigate sustained unbounded growth, storage cleanup churn, or multi-gigabyte memory.

## Bad Startup Indicators

- Missing required environment variable
- Userbot waiting for interactive phone login
- `AUTH_KEY_DUPLICATED`
- `database disk image is malformed`
- repeated DB recovery cycles
- `unable to open database file`
- DB readiness gate failure
- target index unexpectedly empty after known production reuse
- Hot-500 starts a new pass without a new token
- queue recovered as zero when durable queue files contain jobs
- repeated worker restart messages
- readiness remains false after recovery
- source count differs materially from source JSON without reconciliation
- `/health` exposes raw credential-bearing exception text

# Part 8 - Production Checklist

- [ ] Fix session-string fail-fast.
- [ ] Fix all nine Ruff findings.
- [ ] Decide whether Source Guard/Auto Sync are intentionally replaced or must be enabled.
- [ ] Correct the misleading startup owner message.
- [ ] Protect detailed health endpoints.
- [ ] Build with a tested dependency lock/constraints file.
- [ ] Create a full backup of `/data/royells_media_bot`.
- [ ] Download an off-platform copy of the backup.
- [ ] Verify backup ZIP integrity and SQLite `PRAGMA integrity_check`.
- [ ] Verify the six required Secrets.
- [ ] Confirm the user session is unique and no old Space/process uses it.
- [ ] Confirm target/report/support channel IDs.
- [ ] Attach persistent storage and verify `/data` durability.
- [ ] Confirm enough free storage for DB, JSON, backups, and temporary media.
- [ ] Remove old ZIPs, caches, logs, and session files from deploy root.
- [ ] Keep PostgreSQL, Redis, dual-read, and dual-write disabled.
- [ ] Deploy to a staging Space first.
- [ ] Verify compile and all 56 offline tests in the exact image.
- [ ] Run real Telegram single/album/protected-source tests.
- [ ] Run restart, SIGTERM, abrupt-stop, and recovery tests.
- [ ] Verify no duplicate target delivery after ambiguous timeout/restart.
- [ ] Verify queue and Hot-500 resume from exact stored progress.
- [ ] Verify dashboard button latency under load.
- [ ] Verify DB backup/restore and corruption recovery on the mounted volume.
- [ ] Observe at least 12-24 hours of staging load.
- [ ] Record queue throughput, memory, DB latency, FloodWaits, retry rate, and album failures.
- [ ] Confirm public endpoint exposure is acceptable.
- [ ] Factory Rebuild production once.
- [ ] Do not delete SQLite, queue JSON, checkpoint JSON, source JSON, target index, or ledger.

# Part 9 - Stress and Functional Verification

## Mirroring and Downloader/Uploader

1. Add one unrestricted and one protected source.
2. Publish unique photo, video, document, and caption cases.
3. Confirm source message IDs and media UIDs enter queue/state.
4. Confirm temporary files are non-zero and removed after terminal upload.
5. Record download and upload latency/throughput for at least 100 mixed jobs.

## Albums

1. Test groups of 2, 9, 10, 11, and 20 items.
2. Test a protected album.
3. Include one intentionally invalid/deleted member.
4. Confirm 11/20 split into valid Telegram groups without a one-item group.
5. Confirm successful chunks are committed and not replayed after later failure.

## Duplicate Detection

1. Mirror a known item.
2. Re-submit through realtime, Hot-500, history, and link paths.
3. Restart between admission and retry.
4. Confirm one target delivery and a duplicate/terminal state for all replays.

## Queue and Scheduler

1. Build a queue above the pressure target.
2. Confirm staged recovery stays bounded.
3. Confirm fair sources can progress.
4. Trigger a retryable timeout and FloodWait.
5. Verify due-time ordering, attempt counters, backoff, and terminal/dead state.

## Source Guard and Auto Sync

They cannot be validated as active under the shipped Docker profile because both are disabled and suppressed when adaptive intake is enabled. Test them in a separate profile with adaptive intake disabled, or formally accept adaptive intake/historical backfill as their replacement.

## Subscription

Test add, expire, profile refresh, access denial, owner exemption, and restart persistence.

## Recovery

Run:

- graceful owner restart
- SIGTERM
- forced container stop
- kill during download
- kill immediately before send
- kill during ambiguous send
- kill after Telegram accepts but before DB commit
- SQLite read/write failure
- corrupt current checkpoint with valid previous checkpoint
- network disconnect/reconnect

For each, compare pre/post queue keys, pending intents, posted/target UIDs, source cursor, Hot-500 completed sources, temp files, and target message count.

# Part 10 - Bug, Placeholder, and Dead-Code Audit

## Ruff Findings

| Line | Finding |
|---:|---|
| 1040 | unused exception variable |
| 1317 | unused exception variable |
| 3852 | unused timeout exception variable |
| 10898 | exception variable captured by lambda, Ruff `F821` |
| 14822 | unused `title` |
| 15811 | unused timeout exception variable |
| 17007/17009 | exception variable captured by lambda, Ruff `F821` |
| 17267 | exception variable captured by lambda, Ruff `F821` |

## Pass Statements

| File:line | Assessment |
|---|---|
| `royells_media_bot_ready.py:1082` | intentional Windows fallback when `fcntl` is unavailable |
| `:1737` | suppresses previous-checkpoint rotation failure; should be logged |
| `:2449` | suppresses bulk salvage insert failure before per-row fallback |
| `:3654` | no running loop during reconnect scheduling; acceptable |
| `:3902` | expected `MessageNotModified` |
| `:3942` | best-effort temp cleanup; diagnostic would help |
| `:4023` | `/proc` memory read unsupported/failure fallback |
| `:8216` | normal periodic checkpoint timeout wakeup |
| `:9691` | best-effort file-extension rename |
| `:9726`, `:9751` | ffprobe metadata fallback |
| `:13260` | invite link lookup fallback to creating a new link |
| `:13766`, `:13809` | message refresh fallback; hidden error reduces diagnosis |
| `royells_v20_postgres/adapter.py:297` | preserves original transaction error if rollback also fails |

## Dormant/Declaration-Only Functions

AST/reference review found no top-level duplicate functions/classes, but the following are dormant, compatibility-only, or declaration-only in the active profile:

- `env_bool_any`
- `should_offload_link`
- `worker_queue_ping_loop`
- disabled Brain discovery/validation/pending functions
- `append_event`
- `add_uid_to_target_full_index`
- `all_messages_are_duplicates`
- `persisted_queue_job_is_recoverable`
- `delayed_source_retry`
- `fix_video_ext`
- `video_is_telegram_friendly`
- `add_uid_to_target`
- `is_live_dashboard`
- `live_logs_loop`
- `source_brain_rank`
- `startup_hot_scan_snapshot`
- `mask_secret_url`
- old `supervise_loop`
- old `run_once_supervised`

Decorated Pyrogram handlers were not classified as dead merely because they have no direct Python caller.

No genuine TODO/FIXME/placeholder implementation was found.

# Part 11 - Security Audit

## Positive Controls

- Secrets are environment-driven.
- Structured V20 logger excludes common secret field names.
- PostgreSQL/Redis URLs provide redacted representations.
- SQLite uses parameterized values in normal repositories.
- Dynamic SQLite table/column identifiers come from internal allowlisted schema data.
- Subprocess calls use argument arrays, not a shell.
- Restore ZIP validation blocks traversal and symlinks and limits expansion.
- Local storage adapter resolves and constrains paths to configured roots.
- JSON/checkpoint persistence uses atomic replacement, checksum/HMAC-style comparison, fsync options, and previous generation fallback.
- Container runs as non-root UID 1000.

## Security Findings

1. Public detailed health endpoints: High.
2. Raw `last_error` and optional dependency exception text in public responses: High.
3. `/data` mode `0777`: Medium.
4. Runtime/state/log/session files rely on process umask rather than explicit restrictive modes: Medium.
5. Space repository must never contain `.session`, DB, JSON state, logs, or backup ZIPs: operational requirement.
6. No authentication/rate limit/security headers on status server: Medium.
7. `ThreadingHTTPServer` is acceptable for tiny health traffic but has no request concurrency cap: Low.
8. Error logs may include Telegram RPC context and source/channel IDs; restrict log access: Low/Medium.

# Part 12 - Final Verdict

1. **Is Royells Bot v20 production-ready?**  
   Not yet. It is close for the legacy SQLite/JSON profile but blocked by the missing session fail-fast and failed static quality gate.

2. **Is it safe for 24/7 continuous operation?**  
   The code is designed for continuous operation and suppresses unsafe process restart loops, but 24/7 safety is not proven until staging crash/recovery and long-duration tests pass.

3. **Is it safe on Hugging Face Free Space?**  
   Not for guaranteed 24/7 service. Free hardware can sleep, and state is unsafe without attached persistent storage.

4. **Is any manual step required?**  
   Yes: backup, persistent storage, six Secrets, unique session, staging tests, endpoint hardening, and initial Factory Rebuild.

5. **Is any environment variable missing?**  
   The documented required set is complete, but the code fails to enforce `ROYELLS_USER_SESSION_STRING`.

6. **Is any dependency missing?**  
   No production dependency is missing. Live integration dependencies/services were not exercised.

7. **Is migration required?**  
   No for the default SQLite/JSON deployment. PostgreSQL/Redis cutover is not implemented or enabled.

8. **Is any configuration incorrect?**  
   Yes: disabled Source Guard/Auto Sync may conflict with expected features; startup messaging is misleading; three Docker variables are ignored; some V20 flags are non-operational.

9. **Known risks?**  
   Telegram acceptance-boundary ambiguity, mounted-storage SQLite behavior, public health leakage, provider tests skipped, broad dependency ranges, and remaining monolithic/global coupling.

10. **Remaining improvements?**  
    Fix blockers, lock dependencies, secure health endpoints, make configuration truthful, complete live chaos/recovery tests, and continue incremental migration only after validation.

## Deployment Readiness Score

**81 / 100**

Score basis:

- Architecture and compatibility: 16/20
- SQLite/JSON durability and recovery: 17/20
- Tests and code quality: 12/20
- Security: 13/20
- Deployment/operations: 13/20
- Documentation and maintainability: 10/20

Deployment recommendation: **HOLD**, fix the blocking findings, then rerun compile, tests, Ruff, staging recovery, and endpoint-security validation.
