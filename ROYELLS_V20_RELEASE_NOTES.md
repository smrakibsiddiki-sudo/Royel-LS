# Royells v21.0.0 Release Notes

## v21.0.0 Micro Worker Engine

- Added the v21 in-process micro-worker engine while preserving the v20 SQLite, JSON checkpoint, delivery-intent, recovery, durable startup source scan, historical scan, and dashboard authorities.
- Added immutable job contexts and event-bus boundaries for admission, scheduler/backpressure, download dispatch, upload dispatch, duplicate observation, retry, media validation, upload recovery, cleanup, and metrics.
- Added startup temp cleanup for reconstructable downloaded media files while preserving queue/checkpoint records so manual restarts redownload from source.
- Added pre-upload media validation after download/resume: zero-byte/incomplete rejection, photo decoder checks, video metadata checks, checksum capture, and corruption detection.
- Routed `FILE_PART_X_MISSING` through the upload recovery engine while keeping the existing fresh byte-zero reupload and quarantine behavior.
- Added a v21 event drainer worker and `/health` exposure for v21 event/cleanup snapshots.
- Removed media queue soft/hard admission caps and made v21 scheduler ignore bot-control/dashboard FloodWait for media download admission; Telegram calls still honor role-specific FloodWait gates.
- Improved retry diagnostics so bare `TimeoutError` retries are recorded with a readable reason instead of a blank retry reason.
- Added the production `build_manifest.json` system: generated during Docker build, regenerated if missing, validated before public status/Telegram startup, exposed in `/health`, and available from Tools -> Build Info.
- Added critical-file SHA256 validation, environment/dependency inventory, rollback metadata, feature flags, and SQLite schema-version compatibility checks.
- Added the v21 Production Intelligence sidecar: `health_manifest.json` refresh, append-only rotating `incident_manifest.json`, automatic incident classification, diagnostic snapshots, and dashboard pages for health, workers, queues, database, sources, performance, and incidents.

## v20.0.4 Upload Storm Stability Hotfix

- Deferred Telegram media coroutine creation inside `telegram_media_call` to remove un-awaited copy-media-group coroutine warnings during source-scan timeouts.
- Classified Pyrogram missing `.temp` download paths as temporary retryable download races.
- Changed production upload profile to upload workers `2`, media concurrency `1`, Pyrogram transmissions `1`, and 1800-second upload hard timeout.
- Forced stale runtime worker/queue state to the v20.0.4 profile by default, preventing old saved `5/5/2` values from surviving rebuilds.

## v20.0.3 Startup Session-DB Hotfix

- Moved Pyrogram `.session` SQLite storage to `runtime/sessions`.
- Added a startup probe for the Pyrogram session directory before bot/userbot start.
- Added bounded retry and matching `.session*` cleanup when Telegram client startup raises `unable to open database file`.
- Prevents the process from crashing immediately after runtime/target-index recovery when the restored Pyrogram session storage is stale or temporarily unopenable.
- Auto-upgrades stale saved runtime worker/queue defaults to the current speed profile unless manual Settings changes exist.

## v20.0.2 Dashboard, Subscription, Source, and Speed Update

- Live dashboard refresh defaults to 5 seconds.
- Tools menu now contains Admin Manager and Target Manager, with Commands removed from the visible Tools flow.
- Multiple runtime admins can be added or removed from the dashboard.
- Target channel can be changed from the dashboard using IDs pasted without `-100`, @username, invite links, or post links.
- Settings worker/queue controls no longer enforce a UI maximum cap.
- Subs opens directly to active subscriber names, then subscriber details with Remove and Ban actions.
- Source channels open as clickable channel names, then a details page with saved link, added date, Telegram deep-link open button, and remove action.
- User private/support messages are scheduled for user-side deletion after 1 hour while support-channel copies are preserved.
- `/start` from subscribers sends a structured profile card to the support channel.
- Docker speed profile increases intake capacity, queue depth, worker/API concurrency, and source channels per adaptive tick while preserving conservative album upload serialization.

## v20.0.1 Transfer Stability Hotfix

- Fixed the retry race that could delete Pyrogram `.temp` files while a timed-out download attempt was still unwinding.
- Each download retry now receives a unique destination path, so one failed attempt cannot corrupt the next attempt.
- Added deferred cleanup for abandoned download attempt files, preserving stability without leaking runtime storage.
- Increased Hugging Face upload/media-call timeout defaults to 900 seconds for large videos and media groups.
- Added `ROYELLS_UPLOAD_ALBUM_CONCURRENCY`, defaulting to one album upload at a time, to prevent `FILE_PART_X_MISSING` retry storms from concurrent media-group uploads.
- Reduced Docker-pinned Pyrogram `ROYELLS_MAX_CONCURRENT_TRANSMISSIONS` to 1 in v20.0.4 for stable real throughput under Telegram session upload limits.
- Added size-aware upload timeout selection for photos, videos, and media groups.

## Architecture

Royells v20 introduces technology-neutral interfaces, repository contracts,
dependency injection, a service container, supervised worker wrappers,
lifecycle coordination, health endpoints, PostgreSQL/Neon infrastructure, and
Redis/Upstash infrastructure.

SQLite, JSON runtime state, the legacy persistent queues, duplicate indexes,
delivery intents, and hot-resume checkpoints remain the production authority.
PostgreSQL and Redis are disabled by default and cannot become authoritative
through Book 18/19 feature flags.

## Compatibility

- Existing SQLite and JSON files are preserved.
- Existing pending download, upload, link, retry, and recovery work is not
  cleared during deployment.
- Existing startup source-scan and historical cursors continue from durable state.
- Automatic full-process restart remains permanently disabled.
- Multiple source addition and all v19 production behavior remain available.
- No new environment variable is required for the default deployment.

## Hardening Completed

- Hybrid download routing now uses Bot API first, Userbot fallback second, and
  the retry queue only after both transports fail.
- Bot API access gaps return structured `NOT_SUPPORTED` results and never mark
  a source unhealthy.
- Album downloads preserve source ordering, captions, media-group identity,
  metadata, and duplicate protection across transport fallback.
- `FILE_PART_X_MISSING` recovery invalidates every cached upload handle and
  session, reopens and verifies each original file, and performs at most one
  complete byte-zero reupload before delayed queue recovery.
- Upload inputs are rejected when missing, zero-byte, size-mismatched, or
  checksum-mismatched.
- Bot API and Userbot FloodWait gates are independent. A FloodWait pauses only
  the affected Telegram client and does not trip source-health circuits.
- Protected-channel copy failures switch directly to grouped download/upload
  without repeating impossible copy operations.
- Stalled download jobs are cancelled independently without restarting healthy
  workers or the process.
- Adaptive source intake applies per-source timeouts and independent
  cancellation while preserving startup source-scan fairness and progress.
- Source circuit activation is restricted to permanent peer/access failures;
  timeouts, slow API calls, FloodWait, and temporary network failures recover
  without disabling sources.
- Valid and invalid peer caches prevent repeated Telegram peer resolution.
- Fair download/upload queues, retry admission, and delivery intents now
  deduplicate stable job identities.
- Permanent source retries are bounded and persist their counters through
  queue/checkpoint recovery.
- SQLite startup and recovery validate integrity, indexes, and foreign keys;
  the job-state writer yields to critical DB writes to prevent starvation.
- Production metrics cover download/upload latency and transport outcomes,
  upload-session recovery, FloodWait, worker recovery, queue/checkpoint
  recovery, circuit activation, zero-byte media, DB latency, and deduplication.
- Persistent media downloads moved from `/data` to ephemeral `/tmp`.
- Shared persistent-I/O circuit breaker with exponential backoff and verified recovery streak.
- SQLite maintenance, incremental vacuum, atomic validated compaction, and legacy auto-vacuum migration.
- Legacy duplicate UID table removal and bounded 100,000-row posted compatibility history.
- Bounded terminal DB/JSON histories without pruning unfinished jobs.
- Diagnostic SQLite integrity work moved off the asyncio event loop.
- Atomic checkpoint generation and delivery-intent persistence race fixes.
- SQLite implicit commit and rollback compatibility.
- Nested SQLite transactions with savepoints and serialized access.
- Non-blocking SQLite queue operations.
- PostgreSQL migration dirty markers that survive failed DDL.
- PostgreSQL logical-restore identity sequence repair.
- PostgreSQL and Redis production-authority feature-flag guards.
- Redis immediate batch enqueue timing consistency.
- DI container, lifecycle, health, worker-supervisor, adapter, repository,
  migration, queue, runtime, lock, metrics, and failure-path tests.

## Validation

- Python compilation: passed.
- Correctness lint: passed.
- Unit and integration tests: 81 passed, 2 skipped.
- Hybrid download and production persistence regression tests: passed.
- No unresolved maintenance marker, broken import, or syntax marker
  remains in production code.
- Live PostgreSQL test: skipped unless `ROYELLS_TEST_POSTGRES_URL` is supplied.
- Live Redis test: skipped unless `ROYELLS_TEST_REDIS_URL` is supplied.
- Legacy production recovery validator: passed.
- Automatic process restart smoke test: suppressed as required.
- Docker release contract: passed static validation.

The local Windows environment did not provide a Docker executable, so an
actual image build was not run here. The Dockerfile copies every required v20
package and migration and performs its own Python compile check during build.
