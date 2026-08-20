# Royells v21 Oracle r11 release notes

Release date: 2026-08-20  
Primary runtime: Oracle `VM.Standard.E2.1.Micro`  
Migration runtime: Oracle Ampere A1

## Architecture summary

The release keeps one durable Telegram session owner, one ordered publisher, and a
bounded persistent retry ledger. E2 is hard-limited to one download and one upload
worker; A1 may adapt only the source-download lane from one to two while target
publishing remains serialized.

## Oracle-log-correlated fixes

- Exact Telegram `[400 MEDIA_INVALID] - The media is invalid` responses from
  `messages.SendMultiMedia` now enter reversible per-item album fallback after the
  first group rejection instead of repeating four identical whole-album sends.
- Source video width, height, duration, and a real boolean `supports_streaming`
  value are propagated safely; streaming is never fabricated as `True`.
- `MEDIA_EMPTY`, `FILE_PART_X_MISSING`, local missing/temp artifacts, and truncated
  downloads stay retryable and never become a source dead-media verdict.
- A one-time narrow migration reopens legacy nonterminal grouped-`MEDIA_INVALID`
  jobs without reopening confirmed deleted-source records.
- Source-owned retries remain renewable after the diagnostic count reaches 24.
  The counter saturates, but durable ownership does not expire.
- Future-due retry descriptors no longer count as runnable pipeline pressure, so
  healthy intake and target indexing can progress without losing parked jobs.
- Bot-client notification/validator faults cannot restart or invalidate the
  userbot. Userbot reconnect waits for its active bounded media RPCs through the
  hard timeout plus grace.
- Worker liveness uses monotonic time and suppresses stale cancellation across a
  detected whole-event-loop/host pause.
- Partial checkpoint hydration preserves `(message metadata, path)` pairs and
  enforces one active artifact generation per source item.
- Dashboard statistics use the in-memory target-index size instead of repeatedly
  scanning the 149k-row SQLite table.

## E2/A1 production profile

- Oracle profile lock prevents an old generic/Hugging Face `.env` from restoring
  unsafe E2 settings. `oracle-a1` is the only allowed Oracle override.
- E2 hard bounds: D1/U1/L1/B1, API/download/media/album concurrency 1, download and
  upload queues 24/4, executor pools 1 each, V21 event queue 512.
- A1 bounds: base D1 with adaptive maximum D2, U1 permanently, queues 48/8.
- SQLite uses WAL and `synchronous=NORMAL`; E2 health/stats checks run every 900s.
- Inactive ImageHash/NumPy loading was removed to save E2 memory and import CPU.
- The multi-stage Dockerfile builds TgCrypto natively on amd64 or arm64, leaves
  compilers outside the runtime image, runs Python as PID 1, honors `SIGTERM`, and
  exposes event-loop-aware `/live` health.
- Oracle Compose persists `/data`, grants 90 seconds for shutdown, restarts on
  process failure, binds status locally, and rotates Docker JSON logs at 20 MB x3.

## Verification

- Python syntax compilation: passed for production, helper, and regression files.
- `unittest` discovery: **43/43 passed**.
- Executable runtime/media policy harness: **64/64 passed**.
- Build manifest generation and validation: required after the final file set is
  frozen and recorded in the release checksum inventory.
- Docker engine was not installed on the Windows audit host. The Dockerfile's
  multi-architecture/lifecycle contract was checked by executable/static tests;
  the actual Linux image must be built on the Oracle VM using the supplied runbook.

## Operator requirements

- Stop the old container before starting r11; never run the same user session in
  two processes.
- Preserve and back up the complete `data` directory.
- Add the bot as an administrator of target `-1003205176109` with media-posting
  permission.
- Join/open every private source using the user account behind
  `ROYELLS_USER_SESSION_STRING`; nine unresolved IDs are listed in the forensic
  audit.
- Deploy with `.env.oracle-e2-micro.example` now. Stop E2 before later moving the
  data/session to A1 and rebuild natively with `.env.oracle-a1.example`.

See `ROYELLS_V21_ORACLE_E2_20260820_FORENSIC_AUDIT.md` for all 4,092 input lines,
counts, line evidence, limitations, and the 24-hour post-deployment acceptance
gates. No software can guarantee zero future Telegram/Oracle errors; this release
is designed to retain ownership, back off, recover safely, and avoid media loss.
