# Royells v21 — August 10/11 Forensic Log Audit and Final Remediation

## Outcome

Both supplied logs are from an **older runtime image/profile**, not from this final package. The old image ran three download workers, three upload workers, two API/media lanes, an unlimited in-memory queue, a 900-second watchdog, a six-hour peer-resolution cooldown, and a 420-second large-download cap. This package hardens each identified path at code level, so an old persisted runtime setting cannot re-enable unsafe worker counts.

This audit is deliberately evidence-based: code and automated tests are verified, but a post-deployment runtime log is still required to prove the new image is actually running in the Space.

## Input evidence

| Input | Physical lines | SHA-256 | Scope |
|---|---:|---|---|
| `royells_diagnostic_20260811_203317.txt` | 87 | `488871504633d34071a86165bce2d7344607c14d7651a3b2522e42c3934d8c09` | Daily diagnostic window ending Aug 11 20:33:17 |
| Aug 10 14:27 startup attachment | 338 | `b6858bb88afd73173c67453dbfbe000c9a206b2edec9430d608e29967e00db5b` | Boot through active retry storm |

The source log build identifier is `67e5733673084e1d06d75977` (startup line 13). It predates this package. The random boot ID `bc10b24d` is a process instance ID, not a build ID.

## Line-by-line diagnostic ledger (87 lines)

| Lines | Exact evidence and interpretation | Final status |
|---|---|---|
| D1–D5 | Report title, timestamp, period start, separators, and System header. The apparent Bengali mojibake in a Windows console rendering is not data corruption; the supplied diagnostic is valid UTF-8. | Informational |
| D6–D7 | Uptime is one day and memory is 457 MB. No restart loop, OOM, or memory-pressure signature. | Healthy |
| D8–D9 | Both filesystems report `capacity_exceeds_ceiling`/untrusted virtual capacity. This is the intended fix for the previously false multi-petabyte Space capacity; it must not be presented as usable disk. | Already fixed; retained |
| D10–D13 | 14.14-MB DB, integrity `ok`, healthy persistence circuit, and valid Telegram session. | Healthy |
| D14–D19 | Separator, Queue header, empty live queues and a maximum of four jobs, then Sources header. Long earlier waits do not mean a queue was still blocked at report generation. | Healthy snapshot |
| D20 | 54 active, 2 historical issues and 2 historical removals. D23 says none was retired this period, so this is not proof of a fresh deletion. | Observe only |
| D21–D22 | “Guard disabled” / “Sync disabled” is misleading in an adaptive-intake profile. The final diagnostic now reports explicit adaptive-intake and startup-gate state instead of implying no source intake. | Fixed observability |
| D23–D24 | Zero source retirements and no per-period retired source. | Healthy |
| D25–D28 | Media header, 35,637 total delivered and 90 delivered in this period. The pipeline is making real progress. | Healthy |
| D29 | Ledger 147,716 vs DB/target index 129,945: a 17,771 (13.7%) stale-ledger delta. Old in-memory duplicate logic could falsely skip a UID that had been deleted from target while offline. | Fixed: canonical target index only; full-scan reconciliation |
| D30–D33 | 2,917 historical dead entries, no new dead entries, no duplicate deletion, and five subscriptions. Zero new dead entries is evidence against a current invalid-media/dead-media storm. | Historical / healthy period |
| D34–D35 | Separator and Errors header. | Informational |
| D36 | `Timeout: 445` is inflated telemetry, not 445 independent transport failures: old diagnostics counted every progress/retry line containing the word timeout. | Fixed structured log bridge |
| D37 | 52 `FILE_PART_X_MISSING` upload-session invalidations. These are Telegram upload-session failures, not invalid source media; recovery must use a fresh local download. | Recoverable path preserved and hardened |
| D38 | Source `-1003920934626` was deferred 24 times. This was an access/cooldown loop, not a media verdict. | Fixed peer retry and deferred coordinator |
| D39 | 22 retry admissions are successful scheduler actions, not 22 errors. | Fixed diagnostic classification |
| D40 | One non-empty media validation retry. It did not produce a dead-media entry. | Safe retry behavior |
| D41–D55 | Fifteen more source deferrals, one per source. These lines show the old peer cooldown/access condition, not a global Telegram outage. | Fixed bounded peer recovery |
| D56–D57 | Separator and Error Details header. | Informational |
| D58 | `post_ca3…` `send_video` timeout after 900 s; a durable upload retry was admitted. Ambiguous delivery must be confirmed before resend. | Retry retained; watchdog/in-flight protection added |
| D59 | `FILE_PART_X_MISSING` part 41. Fresh download is correct; never re-upload the same invalidated upload session. | Fixed/recoverable |
| D60 | Same job was declared stalled at exactly 900 s. This demonstrates the old watchdog collided with a legitimate bounded media call. | Fixed bounded media-RPC heartbeat + invariant |
| D61–D63 | More `FILE_PART_X_MISSING` parts 23, 5, and 24. They are separate recoverable session events, though the old log omitted job ID/recovery count. | Fixed richer recovery state |
| D64–D65 | Second named 900-s send timeout and durable retry admission for the same post. | Retry retained; no dead UID |
| D66–D67 | Separator and Recent Logs header. | Informational |
| D68 | Repeats the part-23 recovery from D61. | Duplicate recent-log context |
| D69–D70 | 90.61-s upload wait, then `UP W3` starts an album. `UP W3` is conclusive evidence of the old unsafe three-uploader profile. | Final code/runtime cap: one publisher |
| D71–D72 | Keepalive succeeds (HTTP 200); memory 458 MB and only one queued upload. | Healthy |
| D73–D74 | Repeats part-5 recovery; `post_ca3…` waited 256.82 s behind the old multi-worker upload contention. | Fixed ordered publisher profile |
| D75–D76 | One unresolved delivery intent is correctly retained for confirmation; `UP W2` begins retry. The unresolved intent prevents blind duplicate upload, while W2 proves old concurrency. | Intent safety retained; worker cap fixed |
| D77–D80 | Repeats part-24/session timeout/retry and unresolved-intent safety state. | Recoverable; old profile evidence |
| D81–D84 | Backup archive creation, empty queue, a 5.68-s document API call, and successful backup delivery. | Healthy |
| D85–D86 | Retry after ambiguity-confirmation grace and one still-unresolved delivery intent. This is correct duplicate-loss protection, not permanent loss. | Safe recovery |
| D87 | `UP W1` starts attempt 4; the file ends without outcome. This log alone cannot prove success or failure of this job. | Requires post-deploy confirmation |

## Line-by-line startup ledger (338 lines)

All lines are covered below; repeated operational lines are grouped only when their exact meaning is identical.

| Lines | Exact evidence and interpretation | Final status |
|---|---|---|
| S1–S2 | Startup delimiter and blank separator. | Informational |
| S3–S13 | Docker starts app, folders/clients/status server are created, app wrapper starts, and manifest validates. S13 records old build `67e…`; there is no boot or manifest failure. | Healthy boot; old image confirmed |
| S14–S18 | Version, Space target, and data/runtime roots. | Informational |
| S19 | Old concurrency: DL=3, UP=3, API=2, media=2. | Fixed and code-enforced: DL=1 (adaptive max 2), UP=1, API/media=1 |
| S20–S22 | v20 composition and v21 engine load; host owns public port. | Healthy |
| S23 | Old runtime config says D3/U3, unlimited queue, no upgraded profile. This independently proves a deployment/config mismatch, not a final-package regression. | Fixed/enforced bounded profile |
| S24 | Isolated 1.393-s state-writer lock wait. The following integrity/maintenance lines are healthy; no sustained DB contention. | Observe only |
| S25–S31 | Delivery ledger replays; SQLite maintenance and integrity are healthy; seven pending intents but no recoverable jobs; 0/57 startup scan progress; 146,652 target UIDs and 806 historical dead skips load. | Healthy recovery baseline |
| S32–S40 | Bot/userbot start, bot lacks target/report membership but userbot warmup succeeds. S39 says 0/57 source peers in cache and lazy resolution is expected to follow. | Peer resolver bug exposed later |
| S41–S44 | JSON/crash recovery ready; old watchdog threshold is 900 s. | Watchdog invariant fixed |
| S45–S79 | Task registration. S54–S56 create DL W1–W3 and S57–S59 create UP W1–W3: direct old-profile evidence. S75 starts startup scan, S76 waits queue barrier, S77 starts adaptive intake. | Final worker caps prevent this profile |
| S80–S83 | Normal resource state, no unfinished jobs, queue-recovery barrier releases, owner ping succeeds. | Healthy |
| S84–S85 | First unresolved source and 21,599-s peer cooldown. | Fixed 5m→10m→20m→30m bounded retry |
| S86–S90 | Backup/start command/scaler/initial intake wait/keepalive. S88 preserves old D3/U3; S89 shows intake blocked on scan completion. | Cap + nonblocking deferred coordinator fixed |
| S91–S115 | Additional source peer failures and 21,600-s cooldowns. S109–S115 reach second failure but keep sources active. | Fixed direct candidate resolver and cap |
| S116–S130 | More peer failures. S119 and S122 have stale count 915; S125 and S130 have count 13. Historical counts were multiplied into six-hour bans. | Fixed legacy count/cooldown normalization |
| S131–S143 | Deferred scans and further unresolved peers, including S135 count 909. | Fixed durable deferred retry; no synthetic completion |
| S144 | Health shows 27 accessible and 30 temporary/unchecked sources. | Expected old peer-cache symptom |
| S145–S156 | One userbot download takes 100.46 s and successfully uploads in 26.05 s. This proves Telegram storage and the ordinary media path work; failures below are throughput/timeout policy, not invalid content. | Healthy evidence |
| S157–S164 | More deferred peers, one repair-pending source, successful keepalive, retention, and healthy memory/queues. | Fixed independent repair retry / released intake |
| S165–S175 | Healthy guard/retention plus five successful direct copy operations at S166–S170 (1+3+1+3+10 media). Mojibake titles are display encoding only. | Healthy copy path |
| S176–S183 | Remaining peer cooldown deferrals. | Fixed bounded retry state |
| S184–S189 | DL W2 and DL W3 concurrently start albums, retention/backup/keepalive remain healthy. | Old concurrency evidence; final max two bounded downloads |
| S190–S199 | Size-derived item retries (205/420/338 s), DL W3 timeout, wait 173.50 s, durable retry. These are slow transport timeouts, not `invalid media` or dead-media events. | Timeout budget expanded; exponential retry |
| S200–S211 | DL W2 watchdog cancellation/requeue at 900 s, then retries and DL W1 timeout at 338 s. | Bounded in-flight heartbeat prevents false stall cancellation |
| S212–S224 | Repeated 205/420/280-s item timeouts, waits 423.66/275.03 s, and durable retries. | Expanded 8-s/MB, 900-s cap; retry backoff |
| S225–S236 | More retention/backup/retry plus DL W2 cancellation/requeue. | Same old watchdog thrash fixed |
| S237–S246 | 725.04-s queue wait, DL W3 205-s timeout, retries and growing queue pressure. | Backoff prevents immediate same-job monopolization |
| S247–S256 | Repeated retry, repair-source suppression (52 similar), DL W1 cancellation of `post_ca3…`, and 595.10-s wait. | Deferred scans no longer block intake; heartbeat fixes cancellation |
| S257–S270 | Further 420/280/338 timeouts, waits 670.10/595.11 s, DL W3 cancellation, and retries. | Same root cause; no dead-media poisoning |
| S271–S282 | Retention/backup and 205/338 timeouts, DL W1/DL W2 retries, waits 653.13/538.10 s. | Same root cause; bounded backoff |
| S283–S294 | More timeout attempts, DL W3 cancellation of `post_ca3…`, 852.18-s wait, retry. | Same root cause; no permanent media verdict |
| S295–S306 | DL W1 timeout at 280 s, target waits for idle pipeline, DL W2 cancellation/requeue. | Same root cause; ordered final pipeline |
| S307–S323 | Queue reaches D3; two target-index idle waits; further 338/205/420/280 timeouts; a new post waits 786.83 s. | Old retry stampede; final caps/backoff prevent it |
| S324–S330 | Keepalive and guard healthy; a 74.26-s download succeeds; then UP W2 successfully uploads. This again proves media is not globally invalid or inaccessible. | Healthy evidence |
| S331–S338 | More timeout retries, DL W2 420-s failure, DL W3 280-s failure, and log ends during active work. No terminal outcome appears. | Requires post-deploy verification |

### Exact timeout chronology

The old timeout values are deterministic from the old `120 + 4 × MB`, capped at 420 seconds: approximately 21 MB → 205 s, 40 MB → 280 s, 54.5 MB → 338 s, and ≥75 MB → 420 s. The final curve is `120 + 8 × MB`, capped at 900 seconds: the same examples receive 288, 440, 556, and 720 seconds. The outer hard cap remains bounded, and a source timeout now backs off deterministically (600 → 1200 → 2400 … ≤7200 seconds) instead of immediately monopolizing the lane.

## Final code remediation matrix

| Finding | Final implementation |
|---|---|
| Old values could launch DL/UP=3 | `royells_media_bot_ready.py` hard-caps D=1/U=1 and adaptive D≤2/U≤1; bounded queues are D=64/U=8. Docker defaults match. |
| A `video metadata missing dimensions` result was mislabeled 0-byte and force-dead | Validator v2 treats unavailable video metadata as advisory, separates empty/incomplete/corrupt states, and releases legacy false-dead entries for rescan. |
| Valid fragmented MP4 could fail a 512-box inspection limit | ISO-BMFF inspection is tri-state; inspection-limit is advisory, only confirmed malformed bounds are hard corruption. |
| Cold cache skipped actual peer recovery | Dialog cache refresh and direct candidate `get_chat` have separate gates; direct recovery gets one bounded attempt after a typed peer error. |
| Legacy 915/909 peer counters created six-hour bans | Peer-invalid state is capped/migrated to a bounded 5m→10m→20m→30m schedule; source remains active. |
| Cooling source became successfully complete or blocked all intake | Startup state now persists `deferred_sources`; cooldown is never success; normal adaptive intake releases while due source/validator repairs retry independently. |
| Valid slow transfer was canceled as worker stall | Active, hard-bounded media RPC heartbeat protects it; after the RPC deadline the watchdog regains cancellation authority. Worker stall always exceeds hard media timeout plus margin. |
| Timeout retries thrashed six jobs | Slow download timeout curve increased to 8 seconds/MB with a 900-s cap; retryable hybrid timeouts use bounded deterministic exponential backoff. |
| `FILE_PART_X_MISSING` could be ambiguous for a manual link after ordinary recovery budget | Manual Telegram links receive durable bounded 10/30/90-minute fresh-download recovery rather than silent release or dead-listing. |
| Daily report counted retry text as errors | Plain retry/defer/backoff logs are excluded; direct worker exceptions remain counted. The report now includes build ID, boot ID, actual caps, intake state, and phase. |
| Stale duplicate ledger could hide target-deleted media | Fast duplicate checks use canonical target evidence only. A full verified target history scan reconciles stale UI ledger entries; a limited scan only merges and never prunes. |

## Verification performed before packaging

- Python syntax compilation for production modules.
- Behavioral media-validation suite: metadata-unavailable video, zero-byte/incomplete detection, source-size mismatch, bounded fragmented MP4, malformed MP4, tiny photo threshold, and false-dead migration.
- Runtime-policy suite: lane caps, FloodWait handling, watchdog bounded RPC behavior, timeout backoff, peer-recovery separation, deferred startup coordinator, diagnostic filtering, and target-index reconciliation.
- Build manifest regeneration and validation after every final source/document change.

## Deployment and acceptance gate

1. Replace the old image with this ZIP’s source and rebuild. Preserve the `/data/royells_media_bot` volume; do not delete state/ledger files.
2. Verify the first boot log has a **new build ID**, not `67e5733673084e1d06d75977`.
3. Verify the runtime line says download `1`, upload `1`, API `1`, media `1`, queues D64/U8; adaptive download may later reach `2`, upload must remain `1`.
4. Verify old peer entries produce a `Normalized legacy peer retry cooldown` log or a bounded retry (`≤1800s`), never 21,600 seconds.
5. Verify a deferred source reports a retry deadline and `Startup source initial pass released adaptive intake`; it must not block realtime intake.
6. Verify large media uses the expanded budget and, if it still times out, logs `Temporary hybrid download timeout retained for retry` with a retry-after value—not invalid/0-byte/dead media.
7. Verify `post_ca3…` by target/index before manually re-sending. The supplied diagnostic ends while its delivery intent is unresolved, so the only safe completion test is target confirmation or the durable retry state after deployment.

