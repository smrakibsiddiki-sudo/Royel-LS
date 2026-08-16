# Royells v21 — 2026-08-12 Recovery-Notification and Pressure Audit

## Scope and evidence

This audit covers every physical line of the supplied startup log and every JSONL record in the supplied incident manifest.

| Evidence | Scope | SHA-256 |
| --- | ---: | --- |
| `pasted-text.txt` | 9,674 physical lines; boot `897f78a8` | `C8C078E037D80B8905EDE8FA8D095872908EBC2EF8137843C1D0674A10669C8A` |
| `incident_manifest.json` | 155 JSONL incident records | `AC9B19B548D5C37B38259F04E413EB29CF54BD691094E9BDE9BA35C786AAAB95` |
| Owner-chat screenshot | 47 repeated recovery messages | Visual corroboration |

The log contains **one** application startup: 2026-08-12 07:33:12. It is not evidence of 47 process restarts. The deployed build had validated identity `9e8474abb8df4bbc2494dc44`, boot ID `897f78a8`, one uploader, one base downloader, a second adaptive downloader, and bounded download/upload windows of 64/8.

## Per-line classification ledger

The following index classifies the complete log by its observed operating domains. Counts may overlap because a single log line can mention both an incident and a retry.

| Domain | Lines / occurrences | Meaning |
| --- | --- | --- |
| Boot, manifest validation, durable-state load, workers, recovery barrier | 1–75 | Healthy boot. The barrier reported **no unfinished queue jobs** and zero reservations before work began. |
| Copy/download/upload activity and successful fallback | 76 onward; 81 album copies, 45 single copies, 47 upload completions | The delivery pipeline and protected-media download/re-upload fallback were working. |
| `Upload session invalidated` | 393; first 130, last 9398 | Telegram `FILE_PART_X_MISSING` upload-session failures. They are transient upload-session errors, not source-media corruption. |
| `MEDIA_EMPTY` | 304; first 712, last 9614 | Retryable/indeterminate target-upload or source-download result; not a permanent media-invalid verdict. |
| `Media download unavailable` | 2,011; first 1175, last 9668 | Repeated empty/missing source-item attempts. Only about 650 unique links were involved, proving repeat admission rather than 2,011 distinct missing media. |
| `Source peer unresolved` | 681; first 161, last 9664 | Access/peer-cache resolution could not currently reach some source channels. Sources stayed active and were cooled down; inaccessible/private sources cannot be made readable by adding workers. |
| Scheduler pauses / local backpressure | 832 / 167; first 452 / 577 | The bounded pipeline correctly stopped taking more work while the upload-ready lane was high. The pressure itself was amplified by duplicate recovery admission. |
| Slow queue waits | download 388 / upload 241; first 96 / 145 | Consequence of the retry storm and bounded publisher, not proof that all media was invalid. |
| Queue-healer stale detection | 121; first 1062, last 9671 | The same delayed descriptors were repeatedly misclassified as stale while the retry scheduler held them outside its visible priority queue. |
| Queue recovery staging | 121; first 1066, last 9673 | All were `admitted=1`, `deferred=0`. The old user-facing notification was sent from this generic recovery path. |
| `0-byte media` / `Invalid media detected` | 0 / 0 | The older false-video-dimensions → zero-byte/dead-media bug was not present in this run. |

The manifest corroborates one runtime interval (2026-08-13 02:00:40Z–06:32:18Z), one build, and one boot. Its 155 records classify as 51 `MEDIA_EMPTY`, 48 timeouts, 27 download failures, 27 `FILE_PART_X_MISSING`, and two upload failures. All are either retryable (128) or indeterminate (27); none is a justified permanent source-media deletion. Its download queue reached the configured bound of 64, upload queue peaked at 7, and durable reservations grew from 92 to 117.

## Root cause of the 47 Telegram messages

The old `recover_queue_state()` routine performed real recovery **and** sent this owner message whenever it admitted a job:

> Recovered 1 unfinished bot jobs after restart. Deferred 1 for pressure-safe later recovery.

It was invoked both by the startup recovery loop (configured to retry every three seconds while a job remained pending) and by the queue healer. Therefore the wording was wrong at runtime: the job was not necessarily recovered “after restart,” and one long-lived descriptor could generate a new Telegram message on every pass.

There was a second, independent amplifier. The retry scheduler removed the earliest future descriptor from its priority queue and then slept until its due time. The healer only inspected visible queue entries, so it saw that valid delayed descriptor as absent/stale and re-admitted it. The same condition created recovery churn, queue pressure, repeated source work, and more notification sends.

A third pressure leak was identified during the line-by-line review: a genuinely unavailable source item was released to a future source scan without its own durable due time. The same link could then be admitted repeatedly before a safe retry interval elapsed.

## r9 corrective architecture

1. **One durable owner status, not per-job chat messages.** Recovery prepares a persisted idempotency cursor before Telegram I/O. The first recovery sends a single `Queue recovery status`; the same job set is deduplicated across healer passes and restarts, while a meaningful update edits the existing message. An ambiguous send result retains its intent rather than creating a duplicate. Owner-notification failure cannot block media recovery.
2. **Visible, preemptible delayed retries.** The retry scheduler peeks at future work instead of removing it while sleeping. A new earlier retry wakes and preempts the sleep. The descriptor stays visible to recovery, checkpoints, temp-file protection, and deduplication until it is actually due.
3. **Durable item-level unavailable backoff.** A missing/empty source item retains its original job ownership, source message descriptors, processing reservation, and any healthy partial-download checkpoint. It is retried at 10, 30, then 90-minute capped intervals, survives restart, and cannot be re-admitted early by a source scan. It is not force-dead solely because Telegram temporarily returns empty bytes.
4. **Single ordered publisher with bounded intake.** The one-publisher / one-to-two downloader profile remains intentional. It prevents concurrent upload-session reuse that triggers `FILE_PART_X_MISSING`, while bounded queues and persisted descriptors prevent loss under pressure.

## What cannot honestly be guaranteed by code

Telegram may still return `FILE_PART_X_MISSING`, a temporary `MEDIA_EMPTY`, a timeout, or refuse access to a private/removed channel. r9 does not pretend those remote conditions never happen. It makes each one durable, classified, throttled, and recoverable without falsely dead-listing accessible media, flooding the owner chat, or repeatedly re-admitting the same work.

## Production acceptance checks

After deploying r9, retain `/data/royells_media_bot` and rebuild the Docker Space. Verify the new build identity in the boot log, then confirm:

1. The old `Recovered ... after restart` text never appears.
2. At most one `Queue recovery status` message is created; later changes edit it rather than sending another message.
3. A delayed retry remains visible to the retry scheduler/healer and is not admitted repeatedly before its due time.
4. A repeated unavailable link is retried only at its persisted 10/30/90-minute schedule.
5. `FILE_PART_X_MISSING` causes a fresh download/retry and never a permanent dead-media record.
6. A source marked forward-restricted is downloaded and re-uploaded; it is never treated as invalid merely because server-side copy is forbidden.

