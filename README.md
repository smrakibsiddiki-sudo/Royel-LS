---
title: Royells Telegram Mirror Bot
sdk: docker
app_port: 7860
pinned: false
---

# Royells Telegram Mirror Bot v21.0.0

Production-ready Docker package for the main Royells Telegram media mirror bot.

This is a main-bot-only build. Never run the same `ROYELLS_USER_SESSION_STRING` in another VM, Space, container, or process.

## Oracle VM profiles

The release defaults to the safe `oracle-e2-micro` profile: one downloader, one
ordered publisher, bounded queues, SQLite WAL, and low-memory backpressure. Use
`.env.oracle-e2-micro.example` with `docker-compose.oracle.yml`. When moving to an
Ampere A1 VM, stop E2 first and use `.env.oracle-a1.example`; A1 may scale downloads
to two, but upload/publish remains one. See `docs/ORACLE_VM_DEPLOYMENT.md` for the
command-by-command deployment, diagnostics, backup, and migration runbook.

The 4,092-line Oracle incident analysis and r11 change ledger are in
`ROYELLS_V21_ORACLE_E2_20260820_FORENSIC_AUDIT.md` and
`ROYELLS_V21_ORACLE_R11_RELEASE_NOTES.md`.

## v21.0.0 Micro Worker Engine

- Adds the in-process v21 micro-worker engine with immutable job contexts, an event bus, admission, scheduler/backpressure, download/upload dispatch events, media validation, duplicate observation, retry ownership, upload recovery, and cleanup ownership.
- Keeps the existing SQLite, JSON runtime checkpoint, delivery intent ledger, queue recovery, durable startup source scan, historical scan, and dashboard behavior backward-compatible.
- Validates downloaded media before upload staging using source-size coherence, bounded MP4/MOV container checks, photo decode, checksums, and advisory video metadata.
- Moves temp-file deletion behind the cleanup engine and removes all reconstructable downloaded media files on startup/restart while preserving the durable queue/checkpoint records needed to download them again from source.
- Routes `FILE_PART_X_MISSING` recovery through the upload recovery engine while preserving the existing byte-zero upload-session rebuild.
- Exposes v21 event/cleanup metrics in `/health` and runtime diagnostics.
- Generates `build_manifest.json` during Docker build and validates it before Telegram clients, workers, or the public status server start. A missing or mismatched production manifest aborts startup safely; runtime never mints a replacement identity.
- Refreshes `health_manifest.json` every 60 seconds in the runtime folder and appends production incidents to `incident_manifest.json` with root-cause classification and recovery metadata.

## 2026-08-12 Final Delivery and Recovery Profile

- The production shape is one ordered target publisher and one base downloader. The E2 profile remains at one downloader; the A1 profile may add one adaptive second downloader. Uploading two independent jobs at once is intentionally prohibited because it breaks target ordering and amplifies Telegram `FILE_PART_X_MISSING` upload-session failures.
- Active in-memory windows are profile-bounded: E2 uses 24 download / 4 upload-ready descriptors and A1 uses 48 / 8. When capacity is pressured, a complete descriptor is persisted as `retry_later` before its processing reservation can be released; source cursors cannot skip media just because the local queue is full.
- `FILE_PART_X_MISSING`, local missing/temp artifacts, `MEDIA_EMPTY`, and `CHAT_FORWARDS_RESTRICTED` are separate failure domains. They never create a permanent dead-media record merely from an upload response.
- A protected/forward-restricted source is capability-cached and routed to fresh source download, validation, and ordered re-upload. Telegram's server-side no-forward rule cannot be bypassed by adding workers.
- After the normal fresh-download retry budget, every source-owned job (not only a manual link) retains a durable file-free descriptor and renews recovery at 10, 30, then 90-minute capped intervals. A source scan can accelerate recovery but is never required to prevent a media miss.
- Legacy target-upload dead records caused by old `0-byte`, `MEDIA_EMPTY`, `FILE_PART`, or generic invalid-upload verdicts are released by an idempotent migration. Explicit source deletion/message-ID-invalid records remain terminal.
- See `ROYELLS_V21_AUG11_174801_FULL_AUDIT.md` for the complete forensic ledger of the most recent supplied 407-line startup log.

## 2026-08-13 Recovery Status and Retry-Ownership Fix

- Queue recovery never sends one owner-chat message per healer pass or per retained job. It maintains one durable, editable `Queue recovery status` message, deduplicated across retries and process restarts. Notification delivery cannot block media recovery.
- A future retry remains visible in the durable priority queue while the retry scheduler waits. A newly due earlier retry wakes the scheduler instead of waiting behind an unrelated long delay; queue healing therefore cannot re-admit the same healthy delayed descriptor as stale.
- A source item that temporarily downloads as empty or missing keeps its job reservation, original source descriptor, and healthy partial-file checkpoints. Its persisted item-level retry uses the 10/30/90-minute capped cadence, survives restart, and prevents a source scan from repeatedly re-adding the same link early.
- See `ROYELLS_V21_AUG12_073312_RECOVERY_SPAM_AUDIT.md` for the complete supplied 9,674-line log and 155-record incident-manifest audit.

## Optional External CPU Helper (Not a Telegram Worker)

- The main bot already enforces the safe delivery shape: one base downloader with at most one adaptive second downloader, one ordered publisher, and durable queue/retry/delivery ledgers. Do not run a second Telegram uploader or reuse the main user session on another host.
- `royells_compute_helper.py`, `royells_compute_helper_service.py`, and `royells_compute_helper_client.py` provide a separately deployable, HMAC-authenticated CPU-only inspection helper. It contains no Telegram dependency, session, token, source/target access, runtime state, or shared volume.
- The helper is opt-in and advisory only. It may inspect a deliberately submitted artifact, but it can never decide dead media, alter retries, upload to Telegram, or block the main bot if it is unavailable.
- The main bot intentionally does not stream live in-flight artifacts to a remote helper: that would duplicate WAN bandwidth and risk a cleanup race. Any future full artifact offload requires an object-store/reference-count design rather than an extra Telegram worker.
- See `docs/EXTERNAL_COMPUTE_HELPER.md` for the threat model, Docker deployment, signed protocol, and safe smoke test.

## v20.0.4 Upload Storm Stability Hotfix (historical context)

- Fixes `RuntimeWarning: coroutine 'CopyMediaGroup.copy_media_group' was never awaited` by creating Telegram media coroutines only after the media gate is acquired.
- Treats Pyrogram missing `.temp` download races as retryable temporary download failures.
- The older two-uploader profile is superseded by the final one-publisher, download-1-to-2 profile above.
- Forces stale saved runtime worker/queue values onto the v20.0.4 profile by default so old `5/5/2` state does not keep causing upload-session storms.

## v20.0.3 Startup Session-DB Hotfix

- Pyrogram session SQLite files now live in `runtime/sessions` instead of the main runtime DB/checkpoint folder.
- Startup probes the Pyrogram session storage before connecting Telegram clients.
- If a bot/userbot session SQLite open fails with `unable to open database file`, the matching `.session*` bundle is cleaned and startup retries instead of crashing the app.
- This targets the crash seen after `Loaded target media index...` and before Telegram clients were started.
- Existing saved runtime worker/queue defaults are auto-upgraded to the v20.0.3 speed profile unless you already changed them manually from Settings.

## v20.0.2 Dashboard, Subscription, Source, and Speed Update

- Admin dashboard starts live refresh automatically and updates every 5 seconds by default.
- Tools now includes Admin Manager and Target Manager. Admins can be added by Telegram user ID, and target channels can be set with direct numeric IDs without `-100`, @username, invite links, or post links.
- Settings no longer enforces a UI maximum cap on worker or queue values. Docker environment values act as initial defaults, and dashboard-saved values apply after Restart Apply.
- Subs now opens directly to active subscriber names. Selecting a subscriber shows details plus Remove and Ban actions, and Add Member remains directly below the subscriber list.
- Sources now opens as a simple list of clickable channel names. Selecting a source shows join/add metadata, saved link, Telegram deep-link open button, and a clearly marked remove action.
- User private/support messages are copied to the support channel and scheduled for user-side deletion after 1 hour. Support-channel messages are preserved.
- Subscriber `/start` sends a structured profile card to the support channel with user ID, name, username, status, subscription state, language, and flags.
- Docker defaults now use a Telegram-stable source-fleet profile: sequential source intake, bounded startup scan, public self-ping disabled, and conservative worker/API concurrency while keeping album uploads serialized.

## 2026-08-10 Delivery Stability Permanent Fix

- Startup source scan is bounded by default (`40` media units / `200` history messages) instead of treating `0` as an unlimited every-boot scan.
- Restart/deploy no longer creates a new startup deep-scan pass unless `ROYELLS_STARTUP_HOT_SCAN_NEW_PASS_TOKEN` is explicitly changed.
- Startup source peer warmup skips per-source `get_chat` checks by default; source peers resolve lazily from the dialog cache or normal scan path.
- Source/historical album prefetch now uses a shared cache and minimum interval, preventing repeated `get_media_group` calls from stacking into FloodWait.
- Outbound keepalive self-pings only the local `/ping` route by default, and any explicit `429` keepalive target is backed off before retry.
- Adaptive media scaling holds base capacity during Telegram FloodWait and the Docker defaults cap adaptive download/upload workers at `2/1`.

## 2026-08-10 False Invalid/Zero-Byte Media Permanent Fix

- Video width/height from an optional local probe is now advisory. The no-FFmpeg image falls back to Telegram's source metadata, so a missing probe can no longer reject a valid video.
- Every completed download is compared with Telegram's authoritative `file_size`; MP4/MOV files also receive a dependency-free bounded ISO-BMFF box check (`ftyp`, `mdat`, and `moov`/`moof`).
- Non-empty validation failures and empty/incomplete downloads use mutually exclusive state-machine branches. A validation failure can no longer fall through and be mislabeled `0-byte`.
- Local missing, empty, or truncated artifacts are retryable and are never force-added to the permanent dead-media list.
- An idempotent validator-v2 migration releases legacy UIDs created by the old `0-byte media terminal after ...` bug, rebuilds dead-media counters, and reopens only affected source scans with bounded repair limits.

## v20.0.1 Transfer Stability Hotfix

- Download retries now use a fresh output path per attempt, preventing cancelled Pyrogram `.temp` files from being deleted while the previous transfer is still unwinding.
- Deferred temp cleanup protects active/cancelled download attempts for a short grace period, then reaps them automatically to avoid storage leaks.
- Upload media calls use a 900s production timeout by default, avoiding unnecessary ambiguous retries for large videos and albums.
- Album/media-group uploads are gated with `ROYELLS_UPLOAD_ALBUM_CONCURRENCY=1` by default to stop concurrent upload-session corruption and repeated `FILE_PART_X_MISSING` storms.
- Hugging Face Docker defaults keep download/API intake fast while reducing Pyrogram `max_concurrent_transmissions` from 4 to 2 for stable effective throughput.

## Royells v20 Architecture

Royells v20 keeps the hardened SQLite, JSON runtime state, persistent queues,
duplicate indexes, album recovery, and hot-resume behavior as the active
production authority. The bot now routes infrastructure through typed
interfaces, repositories, dependency injection, a service container, worker
wrappers, lifecycle coordination, and health services.

PostgreSQL/Neon and Redis/Upstash adapters are included as disabled
infrastructure. They do not receive production traffic and do not replace
SQLite or JSON unless a separately approved migration enables a future
cutover. No new environment variable is required for the default deployment.

## Required Secrets

- `ROYELLS_API_ID`
- `ROYELLS_API_HASH`
- `ROYELLS_BOT_TOKEN`
- `ROYELLS_OWNER_ID`
- `ROYELLS_TARGET_CHAT_ID`
- `ROYELLS_USER_SESSION_STRING`

All non-secret production defaults are already in `Dockerfile`.

## Deploy

1. Replace the Space repository root with these files and directories:
   - `README.md`
   - `Dockerfile`
   - `app.py`
   - `requirements.txt`
   - `royells_media_bot_ready.py`
   - `royells_v21_micro_workers.py`
   - `royells_build_manifest.py`
   - `royells_production_intelligence.py`
   - `build_manifest.json`
   - `ROYELLS_V21_MANIFEST.sha256`
   - `royells_v20_core/`
   - `royells_v20_postgres/`
   - `royells_v20_redis/`
   - `migrations/`
2. Keep the existing required Secrets.
3. Use **Factory Rebuild** once after replacing the files.
4. Do not delete JSON state files. They contain source status, queue recovery data, and duplicate indexes.

The bot automatically validates the active SQLite database. It preserves WAL/SHM files with a corrupt database for recovery, accepts only integrity-checked backups, salvages every readable row before rebuilding when all backups are damaged, bounds recovery artifacts, retries transient open failures, and creates verified compact SQLite snapshots.

Database maintenance is automatic. New databases use incremental auto-vacuum;
legacy databases are migrated during an eligible idle maintenance cycle.
Terminal job payloads and diagnostic histories are bounded, the obsolete
duplicate UID table is emptied after migration, and `posted` is retained only
as a bounded compatibility history. Canonical target UIDs and unfinished jobs
are never pruned. See `ROYELLS_V20_SQLITE_FORENSIC_FINAL.md` for measured
100k/500k/1M database sizes and the full forensic report.

v20.0.1 retains the v19.5 online SQLite repair path. A corrupt live DB is quarantined, restored or rebuilt, then rehydrated from the JSON source/subscription/target state and the checksummed `media_delivery_ledger.jsonl`. Queue workers remain in the same process and continue after DB readiness returns.

## Fixed Behavior

- Albums are preserved as Telegram media groups by default.
- Restricted sources are capability-routed to fresh download, validation, and ordered re-upload as Telegram media groups.
- `MEDIA_EMPTY` first triggers a whole-album fresh download. Individual fallback is considered only after the refreshed artifact still receives an explicit deterministic rejection; it never force-deads the source UID.
- Successful fallback items are committed immediately; one invalid member no longer destroys or retries the healthy remainder.
- A one-item album remainder is uploaded as a single only because Telegram cannot create a one-item media group.
- Albums over 10 items are split into valid 2-10 item groups without a one-item tail.
- Each successful album chunk is committed immediately, preventing duplicate chunks after a later chunk failure.
- Ambiguous timeout results are checked against recent target-channel media before retry.
- Singles retain the fast `copy_message` path.
- Unrestricted albums use `copy_media_group` before any local download, preserving the group while removing the largest avoidable transfer bottleneck.
- Protected/restricted sources fall back to grouped download/upload.
- Downloads no longer refresh every source message before every item; refresh is reserved for retries and zero-byte/file-reference recovery.
- Active sources in `channel_manager.json` are reconciled into SQLite at startup.
- Removed, expired, system, and resolved-alias sources are never resurrected.
- A fresh or rebuilt database triggers a target-channel duplicate-index rebuild before queue recovery or source admission.
- Queue recovery is staged in durable batches and admitted through the bounded D64/U8 active windows; overflow remains durable rather than becoming unbounded live work.
- Every unfinished queue media UID/message is reserved before Telegram clients start, so deferred recovery jobs cannot be admitted again by startup source scan, adaptive intake, historical backfill, or realtime handlers.
- Recovery repeats in durable batches until every persisted job is live or terminal.
- Already-posted recovered jobs are marked duplicate before Telegram fetch/admission.
- Startup source-scan progress is durable inside the existing `sync_source_manager.json`.
- Completed startup scan sources are skipped after deploy/restart, the interrupted source is resumed, removed sources are ignored, and newly added sources are appended to the current pass.
- Legacy v19.1/v19.2 queue records are used to infer the already-completed sequential startup-scan prefix before recovery changes their status.
- Empty/incomplete downloads retry the affected item using the configured bounded retry policy, then remain recoverable for a fresh source download; they are never force-dead solely because a local artifact is empty.
- Startup/historical prefetch pauses at the configured bounded pressure threshold while durable queue recovery continues; this prevents a source cursor from outrunning local capacity.
- Queue healer ignores jobs that are actually queued, retry-scheduled, or owned by a live worker.
- SQLite writes use one DB executor, serialized transactions, explicit rollback, and JSON ledger fallback.
- SQLite startup now has a read-write readiness gate, open retries, stale-sidecar startup repair, and serialized backup snapshots so `unable to open database file` cannot release workers into a broken DB mount.
- Large 88k+ duplicate/target JSON indexes are debounced while each accepted upload is fsynced immediately to the compact append-only media ledger.
- Button callbacks are answered immediately, duplicate clicks are coalesced, and control edits use bounded timeouts outside the media-transfer bottleneck.
- Only the main Dashboard auto-refreshes. Console logs and all other views remain static.

## Restart Stability

Long-running download, upload, album, or FFmpeg jobs no longer force a full process restart.

- Worker-age watchdog checks are diagnostic by default.
- The event-loop watchdog is diagnostic-only by default.
- The unsafe same-process infinite restart loop was removed from `app.py`.
- Code-level automatic process restart is completely disabled.
- If the bot main loop has a fatal failure, the status server remains online and reports `restart circuit open` instead of reloading the queue.
- SQLite diagnostic writer corruption degrades to JSON-backed operation instead of starting a restart storm.

The owner-only **Restart Apply** dashboard action remains available for an intentional controlled restart.

## Zero-Loss Hot Resume

Deploying v20.0.1 keeps the existing JSON, SQLite, duplicate, source, subscription, and queue state files. It also keeps versioned, checksummed runtime checkpoints for live in-memory state:

- `runtime_checkpoint.json`
- `runtime_checkpoint.previous.json`
- `delivery_intents.json`
- `delivery_intents.previous.json`
- `media_delivery_ledger.jsonl`

The checkpoint captures pending download/upload/link/retry/admission queues, queue priorities, album assembly buffers, active worker ownership, pending critical upload-ledger DB commits, pending diagnostic job-state DB writes, processing cache, active temp-file metadata, partial download progress, FloodWait state, source/adaptive/history cursors, delivery intents, and Telegram in-flight diagnostics. On restart, the bot loads the latest valid checkpoint, falls back to the previous-good generation if corruption is detected, converts any accepted-but-uncommitted upload ledger item into a confirmable delivery intent, reconciles delivery intents against recent target-channel media, and then resumes unfinished queue records without clearing or rebuilding queues from scratch.

Controlled dashboard **Restart Apply**, SIGTERM, container rebuilds, and Hugging Face restarts force a shutdown checkpoint before workers are cancelled. SIGKILL/power-loss scenarios recover from the latest periodic checkpoint plus the authoritative JSON/SQLite queue ledgers.

Optional environment controls:

- `ROYELLS_RUNTIME_CHECKPOINT=1`
- `ROYELLS_RUNTIME_CHECKPOINT_INTERVAL_SECONDS=15`
- `ROYELLS_RUNTIME_CHECKPOINT_CONFIRM_HISTORY_LIMIT=1000`
- `ROYELLS_RUNTIME_CHECKPOINT_MAX_AGE_DAYS=30`
- `ROYELLS_DELIVERY_AMBIGUITY_GRACE_SECONDS=120`
- `ROYELLS_JSON_FSYNC=1`

Telegram does not expose a client idempotency key for media sends. Royells therefore persists each delivery intent before sending, records exact returned target message IDs when available, confirms ambiguous sends by target UID, and delays replay during a confirmation grace window. This is the strongest practical exactly-once protocol available with Telegram: jobs and queue state are durably preserved, while a send interrupted at Telegram's acceptance boundary is confirmed before replay.

## Queue and Startup Scan Resume

Deploying v20.0.1 does not clear or rename `download_queue.json`, `upload_queue.json`, `sync_source_manager.json`, duplicate indexes, sources, subscriptions, SQLite data, or runtime checkpoints.

For production cases with unfinished jobs and partially processed startup source scans:

1. All unfinished media keys are reserved before source admission.
2. Existing download/upload jobs resume in durable recovery batches through bounded media admission windows. A full window retains the job as a durable retry, never drops it.
3. Startup source scan waits behind the queue recovery barrier.
4. Legacy queue evidence treats the last touched source as the resumable boundary and restores every proven completed source before it.
5. Startup source scan continues with remaining sources instead of starting every source again.
6. A source is written to the durable completed list only after its scan returns successfully.

Do not delete the runtime queue JSON files or checkpoint files during deployment. If the legacy queue has no record from the currently touched boundary source, Royells may duplicate-safe recheck only that one uncertain boundary source; it will not restart every source. A new startup source-scan pass is never created merely because the process restarted. To request a deliberate new pass later, set `ROYELLS_STARTUP_HOT_SCAN_NEW_PASS_TOKEN` to a new non-empty value once; the stored token prevents the same request from resetting progress on every restart.

## Add Multiple Sources

Dashboard:

`/start` -> `Sources` -> `Add Multiple`

Command:

```text
/addsources
```

Then send one source per line. Comma-separated input is also accepted:

```text
https://t.me/source_one
@source_two
-1001234567890
https://t.me/+invite_hash
```

You can also include values directly after the command:

```text
/addsources @source_one, @source_two, -1001234567890
```

Each item is resolved independently. Existing/reserved sources are skipped, individual failures do not stop the batch, and the result reports added, existing, and failed counts. The default limit is 100 sources per batch.

## Runtime Profile

- Download workers: 1
- Upload workers: 1
- Button workers: 2
- Link workers: 1
- Telegram API concurrency: 1
- Download transfer concurrency: 2 (shared bounded lane)
- Target publish/media concurrency: 1
- Pyrogram concurrent transmissions: 2
- Queue active windows: download 64 | upload-ready 8; durable retry is unlimited on disk, not in RAM
- Scan prefetch pressure target: 48 combined active jobs
- Queue recovery batch/pressure target: 50/100
- Queue recovery retry interval: 3 seconds
- SQLite: DELETE journal mode with `NORMAL` synchronous mode plus fsynced media/JSON journals for Hugging Face Persistent Storage
- SQLite DB executor workers: 1
- Adaptive source intake: enabled, one source per tick
- Historical backfill: disabled by default; enable only for deliberate archival backfill windows
- Target duplicate index: enabled
- Hard watchdog restart: disabled
- Worker-stall restart: disabled
- Automatic process restart: disabled

The production code clamps stale saved runtime values as well as Docker values. This prevents an old `runtime_config.json` from silently changing the deployed stability profile.

## Production Audit

The July 28, 2026 v19.1.0 production log showed one startup, no automatic process restart, 39 active sources, stable memory near 425-457 MB, repeated deterministic `MEDIA_EMPTY` album members, partial-album quarantine, zero-byte download events, large queue waits/backlog, stale queue records, and later SQLite `database disk image is malformed` degradation.

v19.2.0 addressed the observed album, zero-byte, queue-pressure, SQLite, and restart paths.

v19.3.0 adds backward-compatible durable startup source-scan continuation and a persisted-queue recovery barrier. Existing JSON filenames and existing keys remain valid; the optional `startup_hot_scan` object is added only inside `sync_source_manager.json`. Database tables, source management, subscriptions, duplicate indexes, dashboards, bulk source addition, backup/restore, and owner controls remain unchanged.

v19.4.1 adds production hot-resume checkpointing, partial-download reuse, loop-safe checkpoint wakeups, and delivery-intent reconciliation. The bot preserves live queue state across code updates, controlled restarts, Hugging Face rebuilds, network reconnects, and crash recovery while continuing to use existing queue ledgers as the authority for unfinished work.

The July 28, 2026 19:30 startup log showed DB backup restoration and `integrity=ok`, followed by repeated `OperationalError: unable to open database file` from button workers, queue recovery, `/start`, and backup checkpointing. v19.4.2 fixes that path with SQLite directory/sidecar probes, connection retry, startup read-write readiness gating, queue-recovery DB readiness waiting, serialized compact backup creation, and HF-safe `DELETE` journal mode.

The July 29, 2026 production log showed one process startup, but severe internal starvation: a single media-send semaphore serialized five upload workers, unconditional pre-download refresh calls consumed hours, full duplicate-index snapshots stalled the event loop, button callbacks waited behind repeated work, and live SQLite corruption caused 25 failed compact backups. v19.5.0 removes those internal bottlenecks, adds online DB recovery and the append-only delivery ledger, rate-limits repetitive latency diagnostics, enables two concurrent target media sends, and keeps automatic full-process restart disabled.
