# Royells Bot v20 - SQLite Growth and Forensic Final Report

Date: 2026-07-31

## Evidence Reviewed

- 29-hour runtime log: 15,958 lines, SHA256 `bca21c515be8b0218b31c10507d42b197395d630995900ad8931c60f87d2bb70`
- Latest runtime log: 1,091 lines, SHA256 `ddb2a159426a8fe70e600e6164ad8bc7eef003412648cd42e580ecd343b4059a`
- Entire current Royells v20 source tree and all SQLite read/write paths
- Synthetic production-schema databases at 100k, 500k, and 1M media

The 29-hour log contains one application startup, not a restart loop. It records 54 malformed-database messages, 56 persistent-storage input/output errors, 874 slow Telegram API reports, 259 slow SQLite reports, 92 slow lock reports, 83 download error reports, and 144 timeout references. The latest log also contains one startup; worker `restart_count: 0` is metadata, not evidence of a process restart.

## Forensic Timeline

1. At `2026-07-30 01:41:22`, v19.5 started with a 354.32 MiB database containing 89,494 canonical target rows, 89,494 legacy duplicate target rows, 91,604 posted rows, and 2,343 job rows.
2. Startup restored durable queue/checkpoint state and reserved unfinished media before source intake. This prevented a queue reset, but recovery and target scanning added substantial database and Telegram pressure.
3. Repeated mounted-storage I/O failures destabilized concurrent SQLite/JSON writes. Writer retries then amplified lock waits and connection latency.
4. Synchronous integrity work executed from diagnostics blocked the asyncio event loop. The latest log captured an approximately 912-second stall attributable to that blocking path.
5. Telegram control/media calls experienced external latency and timeouts. Repeated refresh/recovery calls magnified the delay and reduced upload throughput.
6. Downloads previously used persistent storage, so media I/O competed with SQLite, checkpoints, and queue JSON writes on the same mount.
7. Automatic process restart was not observed in either supplied log. The code-level automatic restart paths remain disabled.

## Root Causes and Permanent Changes

### Persistent-storage contention and EIO

Root cause: database, JSON state, checkpoints, and downloaded media competed on the Hugging Face persistent mount.

Implemented:

- Downloads and temporary media now use `/tmp`; authoritative DB/JSON remains under `/data`.
- Atomic writes use unique temporary files and destination-specific locks.
- Persistent EIO activates a shared circuit breaker with exponential backoff.
- Recovery requires consecutive successful writes before the circuit closes.
- SQLite/JSON writers and the delivery ledger respect the same backoff state.

### Event-loop stalls

Root cause: SQLite integrity and diagnostic work ran synchronously in asyncio-owned paths.

Implemented:

- Integrity, diagnostic, maintenance, and blocking persistence work run in bounded executors.
- Dashboard callbacks are answered before queued control work.
- Only the main dashboard performs dynamic refresh.

### Database corruption and growth

Root cause: duplicated UID tables, redundant metadata/indexes, unbounded terminal job payloads, unbounded compatibility hashes, and delayed space reclamation.

Implemented:

- Legacy `target_media` UIDs migrate to `target_media_full_index`; the duplicate table is then emptied.
- UID duplicates are removed from `posted`.
- `posted` is retained only as a bounded 100,000-row compatibility history.
- Deep-clean no longer writes canonical UIDs into both `posted` and `target_media_full_index`.
- Unused `idx_posted_channel` and `idx_target_media_full_message` indexes are removed.
- Repeated target/posted metadata is compacted to empty values.
- Terminal `media_job_state.messages_json` becomes `[]`, then terminal rows are capped at 5,000 and retained for at most 7 days.
- Audit rows are capped at 5,000; metrics rows are capped at 10,000.
- New databases enable `auto_vacuum=INCREMENTAL`.
- Legacy databases are atomically compacted and migrated to incremental auto-vacuum during an idle maintenance cycle, regardless of their current size.
- WAL is checkpointed when WAL mode is selected. Hugging Face defaults to DELETE journal mode to avoid mounted-volume WAL sidecar pressure.
- Validated atomic compaction uses backup, `VACUUM`, integrity verification, fsync, and replacement while DB callers are serialized.

### Queue, checkpoint, and delivery recovery

Implemented:

- Queue/checkpoint dirty generations cannot be cleared by a concurrent mutation.
- Delivery-intent updates are serialized and batch-persisted.
- Startup intent recovery batches target history and source fetches.
- Queue healing can recover stale jobs while unrelated jobs are active, subject to pressure and ownership checks.
- Missing download paths and expired TTL cache entries are pruned without deleting unfinished jobs.
- Runtime queue histories are bounded while every unfinished job remains durable.

## Direct Answers: SQLite Growth

1. **Can `royells.db` still grow to 900 MiB or more?**  
   Yes, eventually, because the permanent canonical UID index must retain one row per unique media. With the measured schema it would take roughly 10.5 million unique media, not 100k or 1M. Nonessential histories no longer grow without bounds.

2. **What prevents unlimited nonessential growth?**  
   Incremental vacuum, periodic maintenance, atomic compaction, duplicate-table migration, compatibility-history caps, terminal-job caps, audit/metrics caps, metadata compaction, and unused-index removal.

3. **Is automatic cleanup implemented?**  
   Yes. It runs periodically and never removes unfinished jobs or canonical duplicate/dead-media records.

4. **Is automatic `VACUUM` implemented?**  
   Yes. Routine reclamation uses incremental vacuum. Full `VACUUM` occurs on a validated temporary database before atomic replacement.

5. **Is `auto_vacuum` enabled?**  
   New DB: yes, `INCREMENTAL`. Legacy DB: automatically converted by the first eligible idle maintenance compaction.

6. **Is WAL checkpoint implemented?**  
   Yes. Passive/truncate checkpoints are used when WAL mode is active. Hugging Face production defaults to DELETE mode.

7. **Are old records automatically removed?**  
   Terminal job history, old compatibility hashes, excess audit/metrics rows, disabled content-filter rows, duplicate legacy UID rows, and redundant metadata are removed. Required canonical UIDs are retained.

8. **Which tables can still grow?**  
   `target_media_full_index` grows intentionally with unique media. `dead_media` and enabled `content_filter_hashes` grow with required user/runtime data. Channels and subscriptions grow only when added. `posted`, terminal jobs, audit, and metrics are bounded.

9. **Measured compact database sizes**

   | Canonical media | Measured size |
   |---:|---:|
   | 100,000 | 17.30 MiB |
   | 500,000 | 50.93 MiB |
   | 1,000,000 | 93.00 MiB |

   Fixture assumptions: actual v20 schema and indexes, blank compact metadata, 100,000 retained compatibility hashes, 5,000 terminal job summaries, 40 channels, 100 subscriptions, and zero freelist pages after `VACUUM`.

10. **Is manual maintenance required?**  
    No routine `VACUUM` or DB deletion is required. Back up `/data/royells_media_bot` before first deployment. Atomic legacy compaction needs sufficient free space; if storage is nearly full, cleanup is deferred rather than risking data.

11. **Was the code modified to minimize the DB without losing required data?**  
    Yes. The authoritative UID index, unfinished jobs, sources, subscriptions, and permanent dead-media protection remain intact; only duplicate, rebuildable, terminal, or bounded compatibility data is compacted.

## Validation

- Python compilation: passed
- Critical Ruff checks `E9,F63,F7,F82`: passed
- Test suite: 63 passed, 2 skipped
- Skips: live PostgreSQL and live Redis integration tests; both adapters remain disabled and are not production authority
- Automatic process restart: disabled
- SQLite/JSON: still the default production authority

## Deployment Notes

- Reuse the current `/data/royells_media_bot` persistent directory.
- Do not delete queue JSON, target indexes, checkpoints, delivery intents, or the current database.
- Make one backup before deploying.
- No new required environment variable was introduced.
- Optional tuning: `ROYELLS_DB_POSTED_COMPAT_MAX_ROWS=100000`.
- The first idle maintenance cycle may shrink and convert a legacy database. Queue processing continues; compaction waits for an eligible idle period and sufficient disk space.

## Residual External Risks

Telegram latency, FloodWait, network interruption, Hugging Face mount failure, and provider-level container restarts cannot be eliminated in application code. The implementation isolates, retries, checkpoints, confirms ambiguous sends, and recovers from those conditions without enabling an automatic process-restart loop.
