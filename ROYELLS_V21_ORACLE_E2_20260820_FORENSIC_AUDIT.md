# Royells v21 Oracle E2.1.Micro Forensic Audit

**Audit date:** 2026-08-20  
**Runtime under review:** Royells v21.0.0 on Oracle `VM.Standard.E2.1.Micro`  
**Candidate deployment profiles:** `oracle-e2-micro` now; `oracle-a1` after migration  
**Audit scope:** every physical line of the supplied Oracle terminal/runtime log, correlated with the release source, runtime state model, retry model, Docker lifecycle, and Oracle deployment configuration

## 1. Executive verdict

The supplied process booted successfully, kept SQLite structurally healthy, and did
not crash, run out of memory, lose disk space, duplicate its Telegram authorization
key, or permanently quarantine the reported media. The runtime nevertheless did not
make useful upload-worker progress: all **236** observed `UP W1` album executions
ended in a recoverable failure path, while the same **26** startup-owned jobs were
re-admitted repeatedly (**213** retry admissions, with a persisted retry count
reaching 24). The only delivery success in the captured session was a separate
direct-copy path at line 920.

The dominant application defect was not that the Telegram links were invalid. The
source files were usually visible and often became downloadable on the bounded
second attempt. The old grouped-upload classifier failed to route Telegram's exact
`MEDIA_INVALID` spelling into the reversible item fallback, so a target-side
`messages.SendMultiMedia` rejection recycled the complete album indefinitely.
Target metadata construction, stale partial-artifact generations, over-broad
userbot reconnects, parked-retry pressure, and an expensive full-table SQLite
statistics query amplified the loop on a fractional E2 CPU.

The candidate source tree contains bounded fixes for those code paths and explicit
E2/A1 profiles. That means the identified defects are addressed in code; it does
**not** mean the deployed Oracle container is already proven fixed. A newly built
container and a new post-deployment log are required for runtime proof. No code can
guarantee zero Telegram errors: FloodWait, deleted source posts, missing channel
membership, Telegram transport loss, OCI host scheduling, disk faults, and upstream
API behavior remain external failure modes.

## 2. Evidence identity and chain of custody

| Field | Value |
|---|---|
| Supplied file | `pasted-text.txt` from the Oracle SSH session |
| Physical line count | **4,092** |
| Byte count | **397,090** |
| SHA-256 | `AA462FAAFB0556575A8B389C88D456BFFBEA7CADD0FE100F6F355E58A3C9F7FB` |
| First bot line | line 6, `[ROYELLS DOCKER] command reached; starting app.py` |
| Boot identifier | `079faf47`, lines 7-16 |
| Last supplied line | line 4,092, a safely handled queue-recovery notification failure |
| Process restarts in capture | **0** |

Lines 1-5 are shell/session preamble, not bot behavior. Line 4 (`-bash: Bot:
command not found`) resulted from typing the human label `Bot Log-` as a shell
command; line 5 immediately runs the correct `sudo docker logs -f royells-test`
command. This shell typo did not affect the bot.

The audit classified every physical line before aggregating repeated signals.
Multi-line JSON ownership dumps and Python stack dumps were retained as structural
evidence, rather than counted as separate application failures. Suppression markers
were used only where stated; raw physical-line counts and suppression-adjusted event
counts are not mixed silently.

## 3. Complete line-coverage map

This table proves contiguous coverage of the supplied file. “Dominant content” is a
routing label; each individual line inside a range was still checked for fatal,
data-loss, delivery, queue, transport, peer, storage, database, and watchdog signals.

| Lines | Dominant content | Audit conclusion |
|---:|---|---|
| 1-5 | PowerShell/SSH and shell commands | One harmless mistyped shell label; correct Docker log command follows. |
| 6-16 | Docker wrapper and build validation | One clean Python boot; build manifest reported valid. |
| 17-129 | Configuration, database, queue/session restoration, task startup | Database healthy; 26 durable jobs restored; target bot lacks membership; runtime did not identify the Oracle profile. |
| 130-632 | Download/upload/retry steady state | Immediate grouped `MEDIA_INVALID` and `MEDIA_EMPTY` recycling begins; no worker delivery success. |
| 633-880 | Watchdog dump 1 | 7,931-second whole-loop/host pause; active Telegram media RPC was misidentified as stale after resume; duplicate partial generations visible. |
| 881-1,517 | Resume and repeated work | Same jobs repeat; one direct-copy success; full-table SQLite statistics remain slow. |
| 1,518-1,973 | Watchdog dump 2 | 1,555-second pause; selector stack is a sampling location, not proof of a Python deadlock. |
| 1,974-2,690 | Resume, download validation, reconnect/retry work | Same queue ownership persists; bot-notification failure triggers an unnecessary userbot transport generation. |
| 2,691-3,023 | Watchdog dump 3 | 1,176-second pause; event loop later reports 1,163.131 seconds of lag. |
| 3,024-3,438 | Resume and repeated work | Truncated downloads are correctly rejected; target peer/reconnect coupling remains visible. |
| 3,439-3,739 | Watchdog dump 4 | 768-second pause; live queue ownership and healthy persistence are preserved. |
| 3,740-4,092 | Resume through end of capture | More invalid/empty loops, bot-error reconnects, one `FILE_PART_X_MISSING`, no process crash. |

## 4. Startup and configuration findings

| Evidence | Meaning | Assessment |
|---|---|---|
| Lines 6-16 | Python reached `app.py`; manifest validated. | Healthy boot. |
| Lines 18-21 | v21.0.0 and persistent `/data/royells_media_bot` paths. | Correct persistent-data location. |
| Lines 22 and 26 | `download=1`, `upload=1`, but `button=2`; `environment pinned=none`. | The intended hard E2 profile was not active. |
| Lines 27-29 | Slow `quick_check`, state lock, and startup insert. | E2 CPU/I/O contention, not corruption. |
| Line 31 | SQLite integrity `ok`; 149,467 target index rows; 1,704 job-state rows; dead list 0. | Database structurally healthy. |
| Lines 34-35 | 26 recoverable jobs and 306 media-key reservations; source progress 52/62. | Durable recovery works, but the starting backlog is already poisoned. |
| Lines 39-44 | Both Telegram clients start; bot cannot resolve target; userbot can. | Only one target route exists and reconnects remove it temporarily. |
| Lines 75-78 | Delivery intents unresolved; queues restore 18 download, 1 upload, 5 retry. | At-least-once state is retained, but no intent commits. |
| Lines 80-113 | Task inventory. | Two button workers and an adaptive scaler are unnecessary on E2. |
| Line 101 and repeats | `SELECT COUNT(*) FROM target_media_full_index`. | Repeated O(n) work over 149,467 rows on 1/8 OCPU. |
| Line 118 | Every reservation is live or terminal. | Recovery bookkeeping is internally consistent. |
| Line 120 | Adaptive scaler active even though capacity remains 1 -> 1. | Monitor overhead with no E2 throughput benefit. |

No startup evidence shows `AUTH_KEY_DUPLICATED`, revoked credentials, invalid API
credentials, corrupt JSON, corrupt SQLite, schema failure, or missing persistent
volume.

## 5. Upload-worker outcome census

There are exactly **236** physical lines matching `[UP W1] Uploading album`. The
following terminal classification is one-to-one with those 236 starts:

| Terminal outcome | Executions | Share | Layer |
|---|---:|---:|---|
| Group upload failed with `MEDIA_INVALID` | **153** | 64.8% | Target-side `messages.SendMultiMedia` rejection; one execution also contained a FloodWait attempt. |
| `MEDIA_EMPTY`; fresh source download requested | **76** | 32.2% | Target upload artifact/session path, not evidence that the public source link is permanently empty. |
| Ambiguous delivery/connection state | **5** | 2.1% | Transport/confirmation path. |
| `FILE_PART_X_MISSING` | **1** | 0.4% | Target-side `messages.UploadMedia` session/storage generation. |
| Stalled job cancellation after whole-loop pause | **1** | 0.4% | False-positive stale handling following a host/event-loop pause. |
| Successful `UP W1` delivery | **0** | 0.0% | No upload-worker success in the capture. |

The 153 grouped failures are directly represented by 153 `[UP ERROR W1]
RuntimeError: grouped album upload failed` lines. The 76 empty-artifact outcomes are
directly represented by 76 `[UP ERROR W1] MediaArtifactRefreshRequired` lines. The
five ambiguous outcomes, single invalidated file-part session, and single stalled
job account for the remaining seven executions.

The only success is line 920:

```text
copy_media_group uploaded album (6) from Teen via userbot.
```

That success is important: target access and Telegram delivery were not universally
broken. It used a separate direct-copy route and therefore does not validate the
download-then-upload worker path that failed 236 times.

### Per-source execution distribution

| Source | Starts | `MEDIA_INVALID` | `MEDIA_EMPTY` | Ambiguous | File part | Stall |
|---|---:|---:|---:|---:|---:|---:|
| Tango South | 55 | 49 | 0 | 4 | 1 | 1 |
| পারিবারিক বিনোদন 💖🥺 | 54 | 46 | 8 | 0 | 0 | 0 |
| 𝙳𝙴𝚂𝙷𝙸 𝚅𝙸𝙳𝙴𝙾 | 30 | 16 | 14 | 0 | 0 | 0 |
| Naughty Desert | 26 | 21 | 5 | 0 | 0 | 0 |
| Fun never end | 26 | 21 | 4 | 1 | 0 | 0 |
| Random 2 | 15 | 0 | 15 | 0 | 0 | 0 |
| Wrong Zone 💥 | 15 | 0 | 15 | 0 | 0 | 0 |
| 🌸 ♡ Beauty Vibes ♡ 🌸 | 5 | 0 | 5 | 0 | 0 | 0 |
| Beauty glamours 🌸 | 5 | 0 | 5 | 0 | 0 | 0 |
| Bangladesh Non Stop | 5 | 0 | 5 | 0 | 0 | 0 |
| **Total** | **236** | **153** | **76** | **5** | **1** | **1** |

The source clustering reinforces that this is deterministic pipeline behavior, not
236 unrelated broken posts. Tango, the family channel, Deshi, Naughty, and Fun take
the grouped rejection branch; the remaining sources repeatedly take the fresh-byte
branch.

## 6. Why visible Telegram links were wrongly perceived as invalid

The log contains **366** `Media download unavailable` notices covering **153 unique
links**. Of those notices, 365 are `attempt 1/2`. Only line 2,378 reaches `attempt
2/2`, for `https://t.me/c/3980226936/14252`; lines 2,379-2,382 retain that item and
its album for a delayed durable retry instead of quarantining it. This is compatible
with transient CDN/session/API emptiness even when a Telegram client can display the
message.

The captured runtime does **not** contain the old false-terminal patterns:

- no `Invalid media detected` final classification;
- no `0-byte media quarantined` event;
- no `video metadata missing dimensions` terminal rejection;
- no `dead_media` insertion; startup explicitly reports `dead_media: 0` at line 31.

Three real truncated downloads were correctly rejected before upload:

| Lines | Expected bytes | Actual bytes | Correct action |
|---:|---:|---:|---|
| 2,229-2,230 | 189,851,693 | 170,917,888 | Validation failure and source-item retry. |
| 3,222-3,223 | 189,851,693 | 181,403,648 | Validation failure and source-item retry. |
| 3,381-3,382 | 189,851,693 | 66,060,288 | Validation failure and source-item retry. |

Therefore the observed `MEDIA_INVALID` is not a statement about source-link access.
It is a Telegram RPC error returned while publishing an already downloaded group to
the **target**. Lines 130-138 explicitly name `messages.SendMultiMedia`; lines
3,893-3,899 explicitly name target-side `messages.UploadMedia` for the one missing
file part.

## 7. Retry livelock and progress starvation

The session begins with 26 owned jobs (lines 34 and 118), records **213** retry
admissions, and continues to report 24-26 pipeline jobs throughout the capture. A
persisted `permanent_retry_count` reaches 24 in the third/fourth ownership dump (line
3,575). There are **39** delivery-intent recovery cycles with `committed=0`.

This created two forms of starvation:

- Adaptive source intake reports a visible pause 63 times; expanding the repeated
  `suppressed 2 similar events` markers yields 186 equivalent pause events.
- The target media index waits for an “idle pipeline” **32** times even when many of
  those owned jobs are delayed until a future retry time.

The ownership set is correct for durability, but it was incorrectly reused as the
definition of runnable pressure. A future-due retry must keep its reservation and
processing key, yet must not block a healthy current download or the target index.

Queue healing itself is not the duplicate source: ten healer passes detect eleven
stale records and stage sixteen admissions with `duplicate=0`. The endless work is
caused by the same deterministic outcome being legitimately re-admitted without a
successful alternative branch.

## 8. Event-loop/VM pauses

Four whole-loop pauses dominate elapsed time:

| Watchdog line | Watchdog wall gap | Heartbeat line | Event-loop lag | Snapshot location |
|---:|---:|---:|---:|---|
| 633 | 7,931 s | 881 | 7,917.642 s | Sample lands in `current_memory_mb()` while an upload RPC owns the media semaphore. |
| 1,518 | 1,555 s | 1,974 | 1,542.759 s | Sample lands in the asyncio selector. |
| 2,691 | 1,176 s | 3,024 | 1,163.131 s | Sample lands in normal event-loop scheduling comparison. |
| 3,439 | 768 s | 3,740 | 754.816 s | Sample lands in normal asyncio callback scheduling. |

The stack snapshots identify where Python happened to be when the watchdog thread
sampled it. They do not prove those trivial statements blocked for 13-132 minutes.
The corresponding heartbeat gaps show the complete loop did not run. Plausible E2
causes include fractional/burstable CPU scheduling, CPU steal, host pause, severe
block-volume delay, swap pressure, or a network/VM suspension. Container stdout
alone cannot choose among them.

The first resume demonstrates an application-side consequence: lines 829-832 show
an active album RPC with a deadline, but line 883 cancels the worker as stale after
the entire event loop returns. Host suspension consumed wall-clock time without the
coroutine getting CPU time, so wall time was unsafe for stale cancellation.

Process memory is not the demonstrated cause. There are 68 runtime samples, with a
minimum of about **115 MB**, maximum **275 MB**, and mean about **240 MB**. No
`MemoryError`, `Killed`, kernel OOM line, or process restart appears in the supplied
log. Kernel/host diagnostics are still required because a container log cannot see
all host-level pressure.

## 9. Database and persistence

SQLite reports `integrity=ok` at line 31, and the watchdog snapshots report healthy
persistence with no open circuit. There is no malformed database, locked-database
failure loop, lost volume, or failed schema migration.

There is avoidable E2 work:

- Fifteen slow-log entries execute `SELECT COUNT(*) FROM
  target_media_full_index`, which has 149,467 rows at startup. Examples include
  lines 101, 155, 1,517, 2,284, 2,545, and 3,972.
- Line 922 records an 18.290-second SQLite commit immediately after the longest
  host/event-loop pause. It is evidence of post-resume I/O pressure, not database
  corruption.
- Persistence and media executors have two threads in the first ownership dump
  (lines 847-855), which is excessive scheduling overhead for an E2 1/8-OCPU
  instance.

The target index is already held in memory. The hot statistics loop should read its
O(1) in-memory size and perform slow database checks on a much longer interval.

## 10. Telegram reconnect analysis

The log reaches **transport generation 18**, so 18 recovery generations occurred.
Eleven are legitimate userbot/session/media transport recoveries. Seven are
incorrectly initiated by a **bot-only owner notification** failure:

| Trigger lines | Trigger |
|---:|---|
| 2,281-2,289 | `gateway app.send_message queue recovery status: Connection lost` |
| 3,215-3,219 | Same bot notification path |
| 3,371-3,377 | Same bot notification path |
| 3,753-3,757 | Same bot notification path; target userbot then reports invalid peer |
| 3,818-3,822 | Same bot notification path |
| 3,934-3,937 | Same bot notification path |
| 4,088-4,092 | Same bot notification path |

A bot-account `app.send_message` error must not stop or restart the separate
userbot session that owns source downloads and, in this deployment, is also the
only target publisher. Lines 3,832-3,837 show the operational effect: a session
validator recovery overlaps the album operation and the upload reports `Client has
not been started yet`. Lines 3,249 and 3,755 show target resolution also disappears
during these transitions.

The safe rule is role-specific: only a reconnectable error from a userbot call or
the userbot validator may schedule a userbot restart. A reconnect must also wait for
active userbot download/upload calls to drain for at least their bounded media RPC
deadline plus grace.

## 11. Partial artifacts and storage ownership

The first watchdog ownership dump contains five current album files at lines
756-761 and five different `partial_download_files` for the exact same five source
messages at lines 769-826. Later dumps repeat the pattern. Line 2,687 reports local
download storage around **570 MB** while the upload queue is blocked.

This is a stale-generation ownership leak, not proof that ten distinct media items
exist. Filtering saved path and metadata arrays independently could also misalign a
file with the wrong source message after restart. The correct invariant is a paired
`(source-message-key, path)` collection with at most one live partial generation per
source message. Replacing a generation must remove the old owned path only after it
is confirmed not to be a current upload file or protected by another live job.

## 12. Peer resolution and Telegram permissions

Seven initially uncached source peers later auto-resolve, proving the deferred dialog
refresh path works. Nine peers remain unresolved at retry count 6:

```text
-1003309484439
-1003920502025
-1003920934626
-1003936657517
-1003946266410
-1003982563055
-1004302371605
-1004310508645
-1004465744281
```

The full first unresolved sequence appears at lines 173-208. Code may preserve the
channel, cool it down, refresh dialogs, and retry; it cannot invent a private
channel's access hash or membership.

Operator action for each ID:

1. Log into Telegram using the same user account represented by
   `ROYELLS_USER_SESSION_STRING`.
2. Obtain the real invite link from the source owner and join/open the channel with
   that account. A numeric `-100...` ID alone is not a join link.
3. Confirm the channel appears in that account's dialog list and that at least one
   media post opens.
4. Restart only the single Royells container, or wait for the bounded resolver
   cooldown. Look for `Auto peer resolver refreshed <id>`.
5. If access cannot be obtained, intentionally disable/remove that source through
   the bot's supported source-management path. Do not let an impossible peer count
   as expected work forever.

Separately, line 41 proves the bot account is not a member/admin of target
`-1003205176109`; line 44 proves only the userbot resolves it. Add the bot account
as a target administrator with permission to post messages/media. Keep the userbot
as source reader and protected-source fallback. This gives the ordered publisher an
independent target route during a userbot source reconnect.

`CHAT_FORWARDS_RESTRICTED` is not solved by ordinary Telegram forwarding. For an
account that is legitimately authorized to view the protected source, the supported
fallback is: download original bytes with the userbot, validate the artifact, then
publish those bytes through the one ordered target publisher. More workers cannot
bypass Telegram permissions or missing membership.

## 13. Root-cause-to-fix matrix

Status vocabulary:

- **Code-fixed candidate**: the release source contains a bounded implementation
  and regression invariant, but the new Oracle image has not yet supplied runtime
  proof.
- **Already validated**: the supplied old runtime itself proves the safeguard.
- **Operator required**: no safe code-only solution exists.
- **Infrastructure investigation**: application hardening limits damage, but OCI
  host evidence is needed for the underlying pause.

| Problem and evidence | Root cause | Candidate correction | Status and required proof |
|---|---|---|---|
| 153 grouped `MEDIA_INVALID` finals; e.g. lines 130-138 | Exact Telegram `MEDIA_INVALID` spelling did not enter the reversible group-rejection fallback. | Exact normalized classifier; on first group rejection, preserve order and caption semantics while trying bounded per-item publication; one-time narrow migration for legacy nonterminal grouped-invalid retries. | **Code-fixed candidate.** New log must show item fallback or a precise per-item terminal reason, not another whole-album loop. |
| Target rejects uploaded videos | Upload metadata could be synthetic/unsafe, including forced streaming or incomplete dimensions. | Sanitize positive integer width/height/duration and preserve `supports_streaming` only when the source value is a real boolean. | **Code-fixed candidate.** Prove with target delivery and no metadata-driven group recycle. |
| FloodWait embedded inside grouped aggregate at line 518 | Retry aggregation obscured Telegram backoff semantics. | Propagate FloodWait immediately to the global per-role gate; do not relabel it `MEDIA_INVALID`. | **Code-fixed candidate.** New log should pause once for the requested duration and resume without a hot loop. |
| 76 `MEDIA_EMPTY` outcomes | Stale/missing local artifacts or upload-session generation requires source rehydration; not permanent source invalidity. | Clear stale upload/partial fields, refresh from source, retain durable ownership, and retry without adding to `dead_media`. | **Code-fixed candidate.** The same link must either deliver, stay delayed with a reason, or prove source deletion/access loss. |
| One `FILE_PART_X_MISSING`, lines 3,893-3,899 | Telegram upload session/file-part generation became invalid. | Invalidate generation, discard stale upload artifact safely, redownload, and start a fresh upload session. | **Code-fixed candidate.** No reuse of the invalidated generation. |
| 366 empty-download notices | Transient Telegram/CDN/session empty return; UI visibility does not guarantee the same API call returns bytes at that instant. | Bounded item retry, strict size validation, durable delayed retry, no false quarantine. | **Already validated** for safety; final delivery still needs post-deploy proof. |
| Truncated downloads at lines 2,229, 3,222, 3,381 | Partial byte stream. | Compare actual and expected size before upload, reject and retry. | **Already validated.** |
| Same 26 jobs, 213 admissions, retry count 24 | Deterministic grouped failure plus capped counter created a livelock/data-loss boundary. | Renewable nonterminal source retry; diagnostic counter saturates without becoming a discard boundary; grouped-invalid legacy migration only once. | **Code-fixed candidate.** Backlog should decline and no job should disappear merely because its counter reached 24. |
| Parked jobs block intake/index | Owned-job count was used as runnable-pressure count. | Separate `runnable_retry_job_count()` / `runnable_pipeline_job_count()` from durable ownership. | **Code-fixed candidate.** Future retries retain reservations but do not block current work or indexing. |
| Bot notification errors restart userbot, seven sequences | Client roles were not isolated in gateway recovery. | Track explicit `bot`/`userbot` role; bot errors never schedule userbot reconnect; split validators. | **Code-fixed candidate.** Zero userbot generations sourced from `app.send_message queue recovery status`. |
| Reconnect interrupts active media | Restart did not fully respect media hard deadline. | Role-aware in-flight ledger and bounded drain at least media RPC hard timeout plus grace. | **Code-fixed candidate.** No `Client has not been started yet` inside an active userbot upload/download. |
| Four whole-loop pauses; one stale cancel | Wall clock advanced while coroutine had no execution opportunity. | Monotonic heartbeat, global host-pause flag, post-resume suppression grace; do not cancel a worker while the whole loop is stale. | **Code-fixed candidate** for false cancellation; **infrastructure investigation** for the pause itself. |
| Duplicate current/partial paths in dumps | Stale generations and independently filtered checkpoint arrays. | Paired hydration; one partial path per source key; safely remove superseded generations; clear partials when complete source download wins. | **Code-fixed candidate.** Runtime directory must not retain two live generations for one message. |
| Repeated 149k-row `COUNT(*)` | Hot monitoring query is O(n) on a fractional CPU. | Use O(1) in-memory target-index length; stretch E2 health/stats interval to 900 seconds; one DB/executor lane. | **Code-fixed candidate.** New log should contain no hot `COUNT(*) FROM target_media_full_index`. |
| Profile reports `environment pinned=none`, button 2 | Oracle E2-specific hard bounds were not active. | Explicit `oracle-e2-micro` and `oracle-a1` profiles; hard E2 D1/U1/B1 and bounded queues/executors. | **Code-fixed candidate.** Startup must report the exact profile and limits. |
| Bot cannot resolve target, nine sources unresolved | Telegram membership/access-hash/admin state. | Add target bot admin; join private sources using the userbot account; retain bounded cooldown. | **Operator required.** |
| Status port could answer while event loop was frozen | Process liveness was confused with readiness. | Bounded status server; `/ping` process check separated from `/live` event-loop readiness; Docker health check uses `/live`. | **Code-fixed candidate.** `/live` must return 503 when heartbeat age exceeds its bound. |
| Main coroutine unexpectedly returns | A silent container can remain “up.” | Exit non-auth unexpected returns with code 75 and let Docker `restart: unless-stopped` recover; retain fail-closed behavior for duplicate auth key. | **Code-fixed candidate.** Docker restart count and health must be monitored. |

## 14. Safe production architecture

The target must have exactly one ordered publisher. Increasing upload workers is
unsafe for album ordering, Telegram upload-session integrity, delivery-intent
deduplication, and target FloodWait behavior. Extra capacity belongs on independent
source downloads only after enough CPU/RAM is available.

```text
private/public sources
        |
        v
userbot source reader ---- peer/access cache
        |
        v
bounded durable download queue ---- delayed retry ledger (owned, but not runnable)
        |
        v
E2: one download lane / A1: one lane, adaptively at most two
        |
        v
strict byte + metadata validation ---- one-generation temp ownership
        |
        v
bounded upload-ready queue
        |
        v
ONE ordered target publisher (bot preferred, userbot permitted fallback)
        |
        v
delivery-intent commit + target UID index + safe temp cleanup
```

SQLite, JSON state, checkpoints, Telegram sessions, delivery intents, and temp
ownership must live in one persistent `./data` mount. Never run two containers
against the same data/session. An external compute helper may perform optional CPU
work on another host, but it must receive neither Telegram credentials nor the
SQLite/session directory, and it cannot accelerate Telegram's own download/upload
or remove FloodWait.

### E2 versus A1 profile

| Control | Oracle E2.1.Micro | Oracle Ampere A1 safe profile | Reason |
|---|---:|---:|---|
| Runtime profile | `oracle-e2-micro` | `oracle-a1` | Prevent accidental generic/Hugging Face defaults. |
| Source download workers | 1 fixed | 1 base, adaptive max 2 | E2 has 1/8 OCPU; A1 can overlap source I/O conservatively. |
| Target upload workers | **1** | **1** | Preserve ordering, intents, and Telegram session safety. |
| Telegram API concurrency | 1 | 1 | Avoid session churn and broad FloodWait. |
| Telegram download concurrency | 1 | 2 | Only A1 receives the second source lane. |
| Telegram media/publisher concurrency | 1 | 1 | One ordered publication lane. |
| Max concurrent transmissions | 1 | 2 | A1 may download while publishing; E2 should not contend. |
| Button workers | 1 | 2 | UI work is bounded; E2 avoids unnecessary tasks. |
| Adaptive media workers | Off | Download only, max 2 | Never adapt target publisher above 1. |
| Download/upload queues | 24 / 4 | 48 / 8 | Bound memory and disk ownership. |
| Event queue | 512 | 1,000 | Sized to available headroom. |
| DB/persistence/media/control executors | 1 each | DB 1; other nonpublisher work up to 2 | Avoid thread contention on E2; SQLite writer remains serialized. |
| SQLite | WAL, `synchronous=NORMAL` | WAL, `synchronous=NORMAL` | Reliable serialized persistence with practical throughput. |
| DB health/stats interval | 900 s | 300 s | Remove needless E2 scans. |
| Memory soft/critical telemetry | 480 / 620 MB | 4,096 / 6,144 MB when the A1 allocation supports it | Admission pressure, not an unsafe forced restart. |
| Publisher ordering | Strict | Strict | Hardware does not relax delivery semantics. |

Oracle's current Always Free page should be checked when provisioning A1. At the
time of this audit it describes monthly A1 allowances equivalent to approximately
2 OCPUs and 12 GB continuously; do not size a new VM from older 4-OCPU/24-GB blog
posts without checking the tenancy's current limits.

## 15. E2 deployment procedure

Run these commands after SSH login as `opc`, from the extracted candidate release
directory. Fill the six secret fields locally on the VM; never send the completed
`.env` file or copy Telegram credentials to a compute-helper host.

```bash
cp .env.oracle-e2-micro.example .env
chmod 600 .env
nano .env

grep -E '^(ROYELLS_RUNTIME_PROFILE|ROYELLS_DOWNLOAD_WORKERS|ROYELLS_UPLOAD_WORKERS|ROYELLS_BUTTON_WORKERS|ROYELLS_IN_MEMORY_QUEUE_MAX)=' .env
mkdir -p data
chmod 700 data
docker compose -f docker-compose.oracle.yml config --quiet
```

For an existing deployment, make the upgrade single-owner and recoverable:

```bash
docker compose -f docker-compose.oracle.yml stop -t 90 royells-bot
cp -a data "data.backup.$(date -u +%Y%m%dT%H%M%SZ)"
docker compose -f docker-compose.oracle.yml build --pull
docker compose -f docker-compose.oracle.yml up -d
docker compose -f docker-compose.oracle.yml ps
docker logs --timestamps --tail 300 -f royells-bot
```

Do not run the old and new containers together. Do not use `docker compose down -v`,
delete `data`, copy an amd64 virtual environment to ARM64, or reuse one Telegram
session from two processes. On A1, stop E2 first, copy source plus the complete data
directory over an encrypted channel, choose `.env.oracle-a1.example`, and rebuild on
A1. The multi-stage image builds native dependencies for the destination
architecture.

## 16. Host diagnostics after any new long pause

The supplied log did not include kernel/OCI metrics, so the four pauses cannot be
assigned permanently to CPU steal, OOM, swap, disk, network, or Oracle maintenance.
Capture the following immediately after a recurrence:

```bash
date -u
uptime
free -h
swapon --show
vmstat 1 10
df -hT
df -i
sudo dmesg -T | egrep -i 'oom|killed process|out of memory|hung task|blocked for more|I/O error|reset'
sudo journalctl -k --since '2 hours ago'
sudo docker stats --no-stream royells-bot
sudo docker inspect royells-bot --format '{{json .State}} {{json .Mounts}} {{.HostConfig.Memory}} {{.HostConfig.MemorySwap}} {{.HostConfig.RestartPolicy.Name}} {{.RestartCount}}'
sudo docker logs --timestamps --since 2h royells-bot
```

Also enable and inspect OCI Compute instance metrics around the exact UTC interval,
especially CPU utilization, network bytes/packets, disk I/O, and instance health.
Keep at least 4 GB free on the actual host-mounted data filesystem. Swap may prevent
an emergency allocation failure but will make a 1-GB E2 instance slower; it is not a
throughput upgrade. Compose rotates Docker JSON logs at 20 MB times three so stdout
cannot fill the boot volume indefinitely.

## 17. Post-deployment acceptance gates

The candidate is accepted only when a fresh image, the preserved production data,
and a fresh timestamped log meet all applicable gates.

### Startup gates

- Exactly one boot/container owns the Telegram session and data volume.
- Manifest validation succeeds.
- Startup reports `profile=oracle-e2-micro`, D1/U1/L1/B1, queue 24/4,
  Telegram API/download/media/album concurrency 1, and adaptive downloads off.
- SQLite reports integrity `ok`; delivery ledger and queue checkpoint load without
  destructive reset.
- Docker reports `healthy`; `/ping` proves the process and `/live` proves a fresh
  event-loop heartbeat.
- Target warmup succeeds through the bot after it is added as administrator, or the
  userbot fallback is explicitly reported.
- Each of the nine source peers either reports a refreshed peer or is intentionally
  disabled because membership is unavailable.

### Functional gates

- At least one previously failing Tango/family/Deshi/Naughty/Fun album completes
  through the download/upload worker path.
- An exact grouped `MEDIA_INVALID` produces the bounded per-item fallback immediately;
  it does not consume repeated whole-album retries.
- Valid source video metadata is preserved, while absent/invalid metadata is not
  fabricated and `supports_streaming` is not forced to true.
- A transient empty item receives bounded immediate retries and then a durable
  delayed retry; it is not put in `dead_media` merely for returning zero bytes.
- A truncated file never reaches target upload.
- `FILE_PART_X_MISSING` invalidates the upload generation, redownloads source bytes,
  and does not reuse stale parts.
- `CHAT_FORWARDS_RESTRICTED` uses the authorized byte-download fallback and reaches
  the target without attempting to bypass source membership.
- Album order, captions, deduplication, and one-publisher delivery-intent commit are
  preserved during item fallback.

### Stability and progress gates

- No userbot recovery generation is triggered by `gateway app.send_message queue
  recovery status` or another bot-only operation.
- No active userbot media call is interrupted by a reconnect; no `Client has not
  been started yet` appears inside an active upload/download.
- Future-due retry jobs retain durable reservations but do not keep adaptive intake
  or target indexing permanently paused.
- The original 26-job backlog trends downward. A renewable job may remain delayed,
  but must not disappear solely because its diagnostic counter reaches 24.
- No hot `SELECT COUNT(*) FROM target_media_full_index` loop appears.
- Runtime downloads contain at most one live partial generation per source message;
  used space stabilizes and remains below configured pressure limits.
- A whole-event-loop pause does not immediately cancel an active worker as stale
  after resume.
- No `AUTH_KEY_DUPLICATED`, database corruption, unbounded owner-message spam,
  restart storm, or repeated duplicate target delivery appears.

### Observation window

Use a minimum 24-hour timestamped run that includes the formerly failing sources.
If Oracle pauses recur, the application may remain recoverable and still fail the
infrastructure gate; correlate the UTC interval with the host commands and OCI
metrics above. “No error line” is not enough—there must be positive delivery commits
and a declining/backed-off retry population.

## 18. What is proven now versus what remains unproven

### Proven by the supplied old-runtime log

- One process booted and remained alive through line 4,092.
- SQLite integrity and persistent checkpoint/ledger recovery worked.
- The source validator caught all three observed truncated files.
- Empty downloads were retried/retained instead of falsely quarantined.
- No media was added to the dead list in this capture.
- The queue healer admitted stale durable work without reported duplicates.
- Direct copy can reach the target through the userbot.
- The upload-worker path did not succeed and was trapped in deterministic retries.
- Nine sources lack usable peer access for the current userbot session.
- The target bot lacks target membership/admin access.
- Four whole-event-loop/VM pauses occurred.

### Addressed in candidate code, pending Oracle proof

- Exact `MEDIA_INVALID` recognition and immediate reversible item fallback.
- Safe source-video metadata propagation.
- FloodWait escape from grouped error aggregation.
- Role-isolated bot/userbot transport recovery and active-transfer drain.
- Renewable retries without a count-24 data-loss boundary.
- Runnable-pressure accounting separate from parked durable ownership.
- Host-pause-aware monotonic stale cancellation.
- Single-generation partial artifact ownership and paired checkpoint hydration.
- O(1) target-index statistics and E2 executor/queue bounds.
- Explicit E2/A1 profiles, multi-architecture build, Docker lifecycle health, and
  rotated logs.

### Cannot be solved or guaranteed by code alone

- Membership/access hashes for the nine private peers.
- Target posting privileges for the bot account.
- Source deletion, bans, revoked invites, or Telegram content restrictions.
- Telegram FloodWait, CDN/API outages, upload-session invalidation, or network loss.
- Oracle host scheduling pauses, kernel OOM, block-volume faults, or tenancy limits.
- Literal zero errors forever or “ultra speed” on a fractional 1/8-OCPU E2 VM.

## 19. Official operational references

- Oracle Always Free compute limits and current A1 allowance:
  <https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm>
- Oracle burstable instance behavior and sustained-load warning:
  <https://docs.oracle.com/en-us/iaas/Content/Compute/References/burstable-instances.htm>
- Oracle Compute instance metrics:
  <https://docs.oracle.com/en-us/iaas/Content/Compute/References/computemetrics.htm>
- Oracle Linux swap implementation guidance:
  <https://docs.oracle.com/en-us/iaas/oracle-linux/stordev/ol-stordev-implementing-swap-spaces.htm>
- Docker JSON-file logging and rotation:
  <https://docs.docker.com/engine/logging/drivers/json-file/>
- Docker memory/CPU resource constraints:
  <https://docs.docker.com/engine/containers/resource_constraints/>
- TgCrypto 1.2.5 package artifacts, relevant to native A1/ARM64 builds:
  <https://pypi.org/project/TgCrypto/1.2.5/>

## 20. Final release decision

The old Oracle log is **not production-acceptable** because its worker publisher has
0/236 successes and the same durable backlog loops indefinitely. The candidate
architecture is appropriate for E2 reliability—controlled D1/U1, one safe
publisher, bounded queues, durable delayed retries—and its A1 profile adds only one
extra source-download lane while preserving ordered U1 publication.

Release may proceed to a controlled E2 deployment only after the full automated
suite and manifest validation pass, the current `data` directory is backed up, the
old container is stopped, the target bot is granted posting access, and the userbot
membership checklist is completed. Final closure requires a new 24-hour Oracle log
meeting the acceptance gates above. Until then, the accurate status is **code-fixed
candidate; runtime verification pending**, not “all errors permanently gone.”

## 21. Final package verification addendum

After the forensic fixes and Oracle profile were frozen, the release passed:

- Python syntax compilation for all production, compute-helper, and regression
  modules;
- **43/43** discovered unit tests;
- **64/64** executable runtime/media policy tests;
- build-manifest validation with zero fatal errors and zero warnings; and
- complete ZIP entry, embedded file-inventory, and SHA-256 inventory verification.

The Windows audit host did not have Docker Engine installed. Therefore the
Dockerfile's multi-architecture, PID-1, health-check, secret-boundary, and resource
contracts were verified statically/behaviorally, while the real Linux image build
remains an explicit Oracle deployment step. These completed pre-deployment checks
do not replace the 24-hour post-deployment acceptance window above.
