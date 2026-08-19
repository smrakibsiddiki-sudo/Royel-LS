# Royells v20 Final Production Stability Report

Date: 2026-08-03

## v20.0.3 Startup Session-DB Addendum

This package fixes the startup crash observed after target-index recovery:
`OperationalError('unable to open database file')`.

Implemented changes:

- Pyrogram bot/userbot session SQLite files are isolated under
  `runtime/sessions`.
- Telegram client startup probes session storage before `client.start()`.
- SQLite open failures for Pyrogram sessions trigger matching `.session*`
  cleanup and bounded retry instead of killing the main process.
- Userbot session-file reset is only automatic when
  `ROYELLS_USER_SESSION_STRING` is configured, so source access can be rebuilt
  safely from the environment secret.

Local validation for this addendum: Python compile checks passed for the full
source tree.

## v20.0.2 Dashboard/Support/Throughput Addendum

This package adds the requested operational dashboard changes on top of the
v20.0.1 transfer-stability fixes.

Implemented changes:

- Dashboard live refresh defaults to 5 seconds.
- Tools menu now exposes Admin Manager and Target Manager, and the visible
  Commands button is removed.
- Runtime admins and runtime target channel are saved in `runtime_config`.
- Settings no longer applies a UI max cap to worker or queue values.
- Subs opens directly to subscriber names, with detail, remove, ban, and add
  member flows.
- Source channels open as clickable names and then detailed source cards with
  saved link and Telegram deep-link open buttons.
- Subscriber/private messages are scheduled for user-side deletion after 1
  hour, while support-channel messages are preserved.
- Subscriber `/start` sends a structured user profile card to the support
  channel.
- Docker defaults are tuned for higher source intake and queue depth while
  keeping album upload serialization to avoid Telegram upload-session
  corruption.

Local validation for this addendum: Python compile checks passed for the full
source tree. Live Telegram throughput is still governed by Telegram account,
FloodWait, file size, data-center, and channel permission limits.

## v20.0.1 Transfer Stability Addendum

This package includes a targeted hotfix for the production log failures seen on
2026-08-02: slow download queue buildup, repeated Pyrogram `.temp`
`FileNotFoundError`, 300-second upload timeouts, and recurring
`FILE_PART_X_MISSING` album retries.

Implemented changes:

- Per-attempt download destination paths for timeout/retry isolation.
- Deferred download temp cleanup with automatic grace-period reaping.
- Conservative router cleanup so a fresh failed transport temp file is not
  removed while Pyrogram may still be cancelling it.
- 900-second Hugging Face upload/media-call timeout defaults.
- Size-aware upload timeout calculation.
- Serialized album/media-group upload gate with
  `ROYELLS_UPLOAD_ALBUM_CONCURRENCY=1`.
- Docker-pinned Pyrogram transfer concurrency reduced from 4 to 2 to avoid
  upload-session corruption while keeping download/API workers active.

Local validation for this hotfix: Python compile checks passed for the full
source tree. Live Telegram transfer testing was not run in this local workspace.

## Completed Scope

Royells v20 now includes the complete production-stability scope from Parts
1-17 while preserving SQLite, JSON runtime state, existing queues, source
configuration, duplicate indexes, Hot-500 progress, historical cursors, and
all backward-compatible bot behavior.

Implemented production controls include:

- Bot API -> Userbot -> retry hybrid download routing.
- Structured Bot API access-gap fallback without source-health penalties.
- Album-safe download ordering and metadata preservation.
- Fresh-session `FILE_PART_X_MISSING` recovery with file size and SHA256
  validation.
- Independent Bot API and Userbot FloodWait gates.
- Protected-channel copy-to-download/upload fallback.
- Per-job download stall cancellation without automatic process restart.
- Per-source adaptive-intake timeout and cancellation.
- Permanent-only source circuit activation.
- Positive and negative Telegram peer caches.
- Missing, zero-byte, incomplete, and changed-file upload rejection.
- Stable queue and delivery-intent deduplication.
- Bounded, checkpointed permanent retry counters.
- SQLite integrity, index, and foreign-key validation.
- Critical DB writer priority over non-critical job-state persistence.
- Queue, checkpoint, worker, Telegram, upload, download, and database metrics.

## Compatibility

- Existing `royells.db` remains supported.
- Existing JSON state and runtime checkpoints remain supported.
- Existing pending and in-flight jobs are restored instead of cleared.
- Existing source channels do not need to be added again.
- PostgreSQL and Redis remain disabled infrastructure unless separately
  enabled through an approved migration.
- No new required environment variable was introduced for the default
  Hugging Face SQLite/JSON deployment.
- Automatic full-process restart remains disabled.

## Final Validation

- Python compilation: passed.
- Ruff critical correctness checks (`E9`, `F63`, `F7`, `F82`): passed.
- Automated test suite: 81 passed, 2 skipped.
- Hybrid download regression tests: passed.
- Upload-session, zero-byte, checksum, FloodWait, queue deduplication,
  delivery-intent, retry-bound, peer-cache, SQLite integrity, and worker-stall
  regression tests: passed.
- Production marker scan: no unresolved TODO, FIXME, placeholder, or
  `NotImplementedError`.

The two skipped tests are live PostgreSQL and live Redis integration tests.
They require `ROYELLS_TEST_POSTGRES_URL` and `ROYELLS_TEST_REDIS_URL` and do
not affect the default SQLite/JSON production mode.

## Known Deployment Boundary

No local Docker executable was available for an actual image build. The
Dockerfile and Python source compile cleanly, and the automated release
contract tests pass. Hugging Face performs the authoritative Docker build
during Factory Rebuild.

## Verdict

The current package is ready for Hugging Face deployment in the existing
SQLite/JSON production mode. Back up persistent storage before deployment,
replace the Space repository files with the release bundle, keep the existing
Secrets and `/data/royells_media_bot` storage, then perform one Factory
Rebuild. Do not delete runtime JSON/checkpoint files or the validated active
database.
