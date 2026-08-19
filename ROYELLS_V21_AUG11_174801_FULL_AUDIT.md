# Royells v21 - 2026-08-11 17:48:01 Full Forensic Audit

## Scope and evidence

This audit covers every physical log line in the supplied startup file.  It does
not infer that a Telegram response proves a source file is corrupt: each error
is classified by its transport, upload-session, access, or local-artifact
failure domain before selecting a recovery action.

| Evidence | Value |
|---|---|
| Input | `===== Application Startup at 2026-08-11 17:48:01 =====` |
| Physical/logical lines | 407 |
| SHA-256 | `df6bfb6b9d0b99b63a675e62173d1a59ad4c915924279d1cbb2db18d179166e6` |
| Recorded build | `67e5733673084e1d06d75977` (line 13) |
| Final-package status | This input is from the old image/profile, not the final package. It launches three downloaders, three uploaders, two API/media lanes, unlimited admission, and a 900-second watchdog. Docker regenerates the manifest during image build, so the deployed replacement ID will be new but need not equal the ZIP's pre-build manifest ID. |

The distinction matters: an internal manifest being valid only proves that the
files inside *that old container* agree with one another. It does not prove that
the container contains the current release. The deployment acceptance section
at the end is therefore mandatory.

## Complete line ledger

Every line is accounted for below. Contiguous lines are grouped only where they
have the same operational meaning; no error line is hidden by grouping.

| Lines | Evidence and interpretation | Resolution/status |
|---|---|---|
| 1-2 | Startup delimiter and blank separator. | Informational. |
| 3-12 | Docker reaches `app.py`; process, folders, clients, wrapper, and status server initialize. | Healthy boot sequence. |
| 13 | Old manifest validates as build `67e5733673084e1d06d75977`. | Confirms an older image, not a manifest corruption. |
| 14-18 | Version, deployment target, and data/runtime roots. | Informational. |
| 19 | `download=3`, `upload=3`, API/media concurrency `2`. | Unsafe old transfer shape. Final profile code-enforces DL 1 (adaptive maximum 2), UP 1, API 1, publish/media 1. |
| 20-22 | v20 composition, v21 engine, and host-owned HTTP port are initialized. | Healthy infrastructure. |
| 23 | Old runtime says `media_queue_limits=unlimited` and `speed_defaults_upgraded=False`. | This is the old persisted profile. Final code forces bounded D64/U8 windows and ignores unsafe saved counts. |
| 24 | One 0.583-second state-lock wait. | Isolated startup contention; database health below is good. |
| 25-31 | Ledger replay, SQLite maintenance/integrity, checkpoint/intents, queue reservations, target index, and historical dead list load correctly. | Healthy recovery baseline; historical `dead media skip list: 806` is not a new dead-media event. |
| 32-40 | Userbot and bot start. Bot lacks target/report membership but userbot warmup succeeds. The source dialog cache is empty (0/57), so peer recovery must work lazily. | Final peer resolver uses a bounded, single-flight refresh plus direct candidate resolution after a concrete peer failure. |
| 41-45 | JSON/crash recovery is active; one delivery intent remains unresolved. Old hard watchdog is 900 seconds. | Delivery intent protects against blind resend. Final watchdog uses bounded media-RPC heartbeat and a minimum 1800-second stall threshold. |
| 46-80 | Worker, retry, recovery, maintenance, source-scan, and adaptive-intake tasks register. Lines 55-60 create DL W1-W3 and UP W1-W3. | Direct proof of the old unsafe worker profile. Final code allows only one ordered uploader. |
| 81-88 | Memory is normal, owner ping succeeds, three downloads start, five persisted jobs are staged, and recovery barrier releases. | Recovery itself is healthy; old unlimited admission is the risk. |
| 89-98 | 13 unrestricted `copy_media_group`/`copyMessage` transfers succeed. Caption fallback on line 98 is intentional because target posts must be captionless. | Proves Telegram access and normal fast-copy path work. |
| 99-101 | Old adaptive intake waits for startup scan; local keepalive and `/start` work. | Final coordinator releases normal adaptive intake while deferred peers retry independently. |
| 102-106 | 90.36-second hybrid download completes; queue wait is 88.74 seconds; first upload starts. | Slow but valid source transfer, not invalid media. |
| 107-108 | Source peer `-1003166116436` becomes unresolved and is cooled for 21,600 seconds. | Old six-hour policy. Final policy migrates legacy counters and retries 5m -> 10m -> 20m -> 30m maximum without retiring the source. |
| 109-111 | Backup is created and sent; a document call is slow but succeeds. | Healthy side services. |
| 112-117 | One-item album remainder uses Telegram-required single fallback; download/upload begin; `FILE_PART_12_MISSING` appears during `messages.UploadMedia`. | FILE_PART is a remote upload-session failure, not source-media invalidity. Final behavior discards handles/sidecars and schedules file-free fresh source download. |
| 118-130 | More slow valid downloads, queue waits, worker starts, source photo uses original Telegram file, and `FILE_PART_10_MISSING` occurs. | Same upload-session domain. No dead-media verdict is allowed. |
| 131-140 | Retention/runtime guard remain healthy; upload queue is 3-4; slow photo send finishes and successful uploads continue. | The pipeline is functioning despite old multi-uploader contention. |
| 141-154 | More old peer failures, including legacy counts 916, and a link report/direct copy succeed. | The huge historical counts are normalized by the final peer migration; sources stay active. |
| 155-160 | `copy_media_group` cannot copy one source type, so it explicitly falls back to grouped download; then `FILE_PART_45_MISSING` occurs and successful albums continue. | A generic copy refusal is expected fallback, not a forwarding/access denial. Fresh-download upload recovery handles FILE_PART. |
| 161-182 | More old peer cooldowns, three concurrent downloader/uploader operations, `FILE_PART_24_MISSING`, protected album send (18.24 seconds), and successful album delivery. | Confirms album re-upload works. Final profile serializes publisher work to prevent session collisions. |
| 183-210 | Additional queue waits, peer failures (including 14 and 910 legacy counts), single-item album remainder, direct copy, and multiple successful albums. | Old peer state and worker contention; no systemic invalid-media condition. |
| 211-232 | Deferred peer scans continue; Random 2 has parallel downloads, valid downloads, singleton album fallback, and one `get_media_group` call. | Final per-source ordering plus bounded download lanes avoids over-fetching and preserves Telegram album constraints. |
| 233-251 | V21 pauses downloads at upload high-water 6; local backpressure reports 438-462 MB; old startup scan keeps deferring cooling peers. | Backpressure is a safety signal, not loss. Final admission persists a durable delayed job before releasing any processing key. |
| 252-277 | One `send_video` times out after 900 seconds; durable retry and ambiguity-confirmation retry occur. Other sends complete at 128/73 seconds. | Ambiguous delivery is retained and confirmed before resend. Final heartbeat prevents watchdog conflict; timeout retry is source-aware exponential backoff. |
| 278-319 | Uploads progress while queue waits reach 604.53 seconds; upload high-water stays at 6; queue healer stages recoverable work. A 388.38-second valid video completes. | Strong proof that long transfer duration is not invalid media. Final one-publisher lane and bounded queue prevent old upload-session storm. |
| 320-336 | More uploads. Line 323 returns `CHAT_FORWARDS_RESTRICTED`; downloads begin. Lines 335-336 show `MEDIA_EMPTY` from a protected album and old immediate member isolation. | Server-side forwarding restriction cannot be bypassed. Final capability cache skips future copy attempts and uses durable download -> validation -> ordered re-upload. `MEDIA_EMPTY` first triggers whole-album fresh download, never force-dead. |
| 337-371 | Deferred peers, valid re-upload activity, slow downloads, `FILE_PART_31_MISSING`, a second `CHAT_FORWARDS_RESTRICTED`, protected source downloads, and recovery staging occur. | All FILE_PART events are `messages.UploadMedia` session failures. Restricted-source fallback is working; final cache persists the capability across restart. |
| 372-388 | Backpressure at 1.2-1.33 GB, successful unrestricted copies, and an 8-item successful upload occur. | Bounded final admission keeps such pressure durable rather than permitting unlimited live objects. |
| 389-407 | Another `MEDIA_EMPTY` protected-album response, item isolation, more valid downloads/copies, and continued backpressure/scheduler pauses. Log ends during active work. | Not proof of corrupt source media. Final recovery refreshes the album source bytes before any isolation; no terminal outcome can be inferred from this truncated log. |

## Exact counts and conclusions

| Category | Count | Lines | Meaning |
|---|---:|---|---|
| `FILE_PART_X_MISSING` | 5 | 116, 129, 157, 177, 355 | Remote Telegram upload-session invalidation; each must use fresh source bytes. |
| `CHAT_FORWARDS_RESTRICTED` | 2 | 323, 357 | Expected Telegram server restriction on direct copy; download/re-upload is the correct path. |
| `MEDIA_EMPTY` | 3 | 335, 364, 389 | Upload rejection of an album artifact; insufficient evidence to mark a source UID dead. |
| Peer unresolved records | 29 | 107, 141, 147-154, 162-176, 185-207 | Old cache/cooldown loop, made worse by legacy counts up to 916. |
| Startup peer deferrals | 14 | 108, 142, 211, 226, 247-250, 337-339, 351-352, 358 | Recoverable access/cooldown state, not deleted media. |
| Successful direct copies | 14 | 89-97, 122, 144, 197, 368, 377, 392 | Source/target connectivity is demonstrably healthy. |
| Successful upload completions | 27 | 136-210, 236-387 (individual logged completions) | Confirms no global target upload outage. |
| False 0-byte / invalid quarantine | 0 | none | The old video-dimensions false-quarantine signature is absent from this log. |

## Permanent remediation in this release

1. **One ordered publisher, bounded parallel download.** The main process is
   hard-capped to one uploader and one base downloader (adaptive maximum two).
   Download, metadata, and publish lanes are separated, but target publishing
   remains serialized to protect album order and Telegram upload sessions.

2. **No-loss admission invariant.** If upload/disk pressure lasts past the
   admission wait, the complete source descriptor is recorded as `retry_later`
   and durably scheduled before any processing ownership is released. A source
   cursor can no longer advance merely because local capacity is temporarily
   unavailable.

3. **FILE_PART and temporary artifact recovery.** `FILE_PART_X_MISSING` clears
   every upload handle, cache, fingerprint, and known session sidecar, tolerates
   a concurrently absent spool file, and always re-enters as a file-free source
   download. Startup cleanup protects active checkpoint/queue files under the
   dedicated download root; only unreferenced stale artifacts are removed. After
   the two immediate fresh-download attempts, both manual links *and
   source-managed channel jobs* retain their exact source descriptor on a
   durable 10 -> 30 -> 90-minute (then capped 90-minute) renewal cadence.
   A later source scan is only an optimisation, never the sole recovery path.

4. **Restricted-forward capability routing.** A source that returns
   `CHAT_FORWARDS_RESTRICTED` is cached as copy-restricted with a bounded TTL
   that survives restart. New jobs skip known-doomed copy RPCs and use the
   source userbot to fetch bytes, validate them, and send them through the
   ordered publisher. This is the only legal and reliable response to Telegram
   protected-content policy.

5. **MEDIA_EMPTY is recoverable evidence.** A protected album rejected with
   `MEDIA_EMPTY` first receives a whole-album fresh-download recovery. It is
   never called permanently invalid or force-added to dead-media state from that
   response. Only explicit source deletion/unavailability can become terminal.

6. **Peer recovery is bounded and persistent.** Empty dialog cache, stale
   peer-invalid counters, and first typed peer failures use separate gates. A
   single-flight dialog refresh and a direct candidate resolve are attempted
   safely; failed sources retry on a capped 5/10/20/30-minute schedule rather
   than old six-hour or multi-day cooldowns.

7. **Timeouts and slow media remain retryable.** A valid long transfer gets
   media-RPC heartbeat protection, a size-aware 120 + 8 seconds/MB budget
   capped at 900 seconds, and source-aware exponential backoff on real timeout.
   It is never converted into `0-byte`, invalid, or dead media solely because it
   was slow.

## Verification and deployment gate

- Production modules are compiled and behavior suites cover validator states,
  peer migration/retry, durable backpressure admission, protected-album
  fallback, FILE_PART reset, temp-file protection, manual-link recovery, and
  watchdog boundaries.
- `build_manifest.json` is regenerated and validated only after all source and
  audit changes are complete.
- Deploy by replacing the Space repository with this release and using **Factory
  Rebuild** once. Preserve `/data/royells_media_bot`; it contains queue,
  checkpoint, delivery-intent, and duplicate state.
- The first post-deploy boot must show a **new** manifest build ID (Docker
  regenerates it during the image build), `download=1`, `upload=1`, API/media
  `1`, and bounded queues D64/U8. If it still shows build
  `67e5733673084e1d06d75977`, DL/UP `3`, or
  `media_queue_limits=unlimited`, the old image/environment is still deployed.
- A post-deploy log is needed to prove Telegram's external service behavior in
  production. The code guarantees durable recovery paths; no program can
  truthfully guarantee that Telegram will never issue a transient network or
  upload-session error.
