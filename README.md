---
title: Royells Telegram Mirror Bot
sdk: docker
app_port: 7860
pinned: false
---

# Royells Telegram Mirror Bot v19.4.3-ui

Production-ready Hugging Face Docker Space package for the main Royells Telegram media mirror bot.

This is a main-bot-only build. Never run the same `ROYELLS_USER_SESSION_STRING` in another Space or process.

## Required Secrets

- `ROYELLS_API_ID`
- `ROYELLS_API_HASH`
- `ROYELLS_BOT_TOKEN`
- `ROYELLS_OWNER_ID`
- `ROYELLS_TARGET_CHAT_ID`
- `ROYELLS_USER_SESSION_STRING`

All non-secret production defaults are already in `Dockerfile`.

## Deploy

1. Replace the Space repository root with these five files:
   - `README.md`
   - `Dockerfile`
   - `app.py`
   - `requirements.txt`
   - `royells_media_bot_ready.py`
2. Keep the existing required Secrets.
3. Use **Factory Rebuild** once after replacing the files.
4. Do not delete JSON state files. They contain source status, queue recovery data, and duplicate indexes.

The bot automatically validates the active SQLite database. It preserves WAL/SHM files with a corrupt database for recovery, accepts only integrity-checked backups, salvages every readable row before rebuilding when all backups are damaged, bounds recovery artifacts, and creates verified compact SQLite snapshots.

## Fixed Behavior

- FairJobQueue checkpoint names are initialized safely, fixing repeated download worker, Source Guard, and historical backfill errors with `'FairJobQueue' object has no attribute 'name'`.
- External keepalive URLs ending in `/health` now fall back to `/ping` if the public route returns 404.
- Albums are preserved as Telegram media groups by default.
- Restricted sources are downloaded, validated, rebuilt, and uploaded as Telegram media groups.
- Individual album fallback is used only after Telegram rejects both protected and plain group delivery as deterministic invalid media.
- Successful fallback items are committed immediately; one invalid member no longer destroys or retries the healthy remainder.
- A one-item album remainder is uploaded as a single only because Telegram cannot create a one-item media group.
- Albums over 10 items are split into valid 2-10 item groups without a one-item tail.
- Each successful album chunk is committed immediately, preventing duplicate chunks after a later chunk failure.
- Ambiguous timeout results are checked against recent target-channel media before retry.
- Singles retain the fast `copy_message` path.
- Protected/restricted sources fall back to grouped download/upload.
- Active sources in `channel_manager.json` are reconciled into SQLite at startup.
- Removed, expired, system, and resolved-alias sources are never resurrected.
- A fresh or rebuilt database triggers a target-channel duplicate-index rebuild before queue recovery or source admission.
- Queue recovery is staged and pressure-limited instead of loading every crashed job at once.
- Every unfinished queue media UID/message is reserved before Telegram clients start, so deferred recovery jobs cannot be admitted again by Hot-500, adaptive intake, historical backfill, or realtime handlers.
- Recovery repeats in bounded 50-job batches until every persisted job is live or terminal; 1,000 jobs are never loaded into RAM at once.
- Already-posted recovered jobs are marked duplicate before Telegram fetch/admission.
- Hot-500 progress is durable inside the existing `sync_source_manager.json`.
- Completed Hot-500 sources are skipped after deploy/restart, the interrupted source is resumed, removed sources are ignored, and newly added sources are appended to the current pass.
- Legacy v19.1/v19.2 queue records are used to infer the already-completed sequential Hot-500 prefix before recovery changes their status.
- Zero-byte downloads retry the affected item up to three times, then quarantine only that item.
- Startup/historical prefetch pauses at 100 local media jobs instead of building an hours-long scan backlog.
- Queue healer ignores jobs that are actually queued, retry-scheduled, or owned by a live worker.
- SQLite writes use one DB executor, serialized transactions, explicit rollback, and JSON ledger fallback.
- Owner dashboard is live by default and refreshes every 5 seconds.
- Tools now includes Access management for extra admin IDs and target channel changes.
- Subscribers opens directly to a name-based dashboard; each subscriber has details, remove, and ban actions.
- Sources opens as a list of channel-name buttons; each source has a details page with open and remove actions.

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

Deploying v19.4.1 keeps the existing JSON, SQLite, duplicate, source, subscription, and queue state files. It also adds versioned, checksummed runtime checkpoints for live in-memory state:

- `runtime_checkpoint.json`
- `runtime_checkpoint.previous.json`
- `delivery_intents.json`
- `delivery_intents.previous.json`

The checkpoint captures pending download/upload/link/retry/admission queues, queue priorities, album assembly buffers, active worker ownership, processing cache, active temp-file metadata, partial download progress, FloodWait state, source/adaptive/history cursors, delivery intents, and Telegram in-flight diagnostics. On restart, the bot loads the latest valid checkpoint, falls back to the previous-good generation if corruption is detected, reconciles delivery intents against recent target-channel media, and then resumes unfinished queue records without clearing or rebuilding queues from scratch.

Controlled dashboard **Restart Apply**, SIGTERM, container rebuilds, and Hugging Face restarts force a shutdown checkpoint before workers are cancelled. SIGKILL/power-loss scenarios recover from the latest periodic checkpoint plus the authoritative JSON/SQLite queue ledgers.

Optional environment controls:

- `ROYELLS_RUNTIME_CHECKPOINT=1`
- `ROYELLS_RUNTIME_CHECKPOINT_INTERVAL_SECONDS=15`
- `ROYELLS_RUNTIME_CHECKPOINT_CONFIRM_HISTORY_LIMIT=1000`
- `ROYELLS_RUNTIME_CHECKPOINT_MAX_AGE_DAYS=30`

## Queue and Hot-500 Resume

Deploying v19.4.1 does not clear or rename `download_queue.json`, `upload_queue.json`, `sync_source_manager.json`, duplicate indexes, sources, subscriptions, SQLite data, or runtime checkpoints.

For the current production case with approximately 1,000 unfinished jobs and 24 of 39 Hot-500 sources already processed:

1. All unfinished media keys are reserved before source admission.
2. Existing download/upload jobs resume in 50-job pressure-safe batches.
3. Hot-500 waits behind the queue recovery barrier.
4. Legacy queue evidence treats the last touched source as the resumable boundary and restores every proven completed source before it.
5. Hot-500 continues with the remaining 15 sources instead of starting all 39 again.
6. A source is written to the durable completed list only after its scan returns successfully.

Do not delete the runtime queue JSON files or checkpoint files during deployment. If the legacy queue has no record from the currently touched boundary source, v19.4.1 may duplicate-safe recheck only that one uncertain boundary source; it will not restart all 39. A new Hot-500 pass is never created merely because the process restarted. To request a deliberate new pass later, set `ROYELLS_STARTUP_HOT_SCAN_NEW_PASS_TOKEN` to a new non-empty value once; the stored token prevents the same request from resetting progress on every restart.

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

- Download workers: 5
- Upload workers: 5
- Button workers: 5
- Link workers: 1
- Telegram API concurrency: 3
- Media transmission concurrency: 2
- Queue soft/hard limits: 1500/2500
- Scan prefetch pressure target: 100
- Queue recovery batch/pressure target: 50/100
- Queue recovery retry interval: 3 seconds
- SQLite: WAL with `FULL` synchronous durability
- SQLite DB executor workers: 1
- Adaptive source intake and historical backfill: enabled
- Target duplicate index: enabled
- Hard watchdog restart: disabled
- Worker-stall restart: disabled
- Automatic process restart: disabled

Explicit Docker environment values take precedence over stale saved runtime worker/queue values. This prevents an old `runtime_config.json` from silently changing the deployed 5/5 profile.

## Production Audit

The July 28, 2026 v19.1.0 production log showed one startup, no automatic process restart, 39 active sources, stable memory near 425-457 MB, repeated deterministic `MEDIA_EMPTY` album members, partial-album quarantine, zero-byte download events, large queue waits/backlog, stale queue records, and later SQLite `database disk image is malformed` degradation.

v19.2.0 addressed the observed album, zero-byte, queue-pressure, SQLite, and restart paths.

v19.3.0 adds backward-compatible durable Hot-500 continuation and a persisted-queue recovery barrier. Existing JSON filenames and existing keys remain valid; the optional `startup_hot_scan` object is added only inside `sync_source_manager.json`. Database tables, source management, subscriptions, duplicate indexes, dashboards, bulk source addition, backup/restore, and owner controls remain unchanged.

v19.4.1 adds production hot-resume checkpointing, partial-download reuse, loop-safe checkpoint wakeups, and delivery-intent reconciliation. The bot preserves live queue state across code updates, controlled restarts, Hugging Face rebuilds, network reconnects, and crash recovery while continuing to use existing queue ledgers as the authority for unfinished work.
