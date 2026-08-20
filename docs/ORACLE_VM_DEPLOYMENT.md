# Oracle E2.1.Micro deployment and A1 migration

## Architecture

The Oracle VM runs exactly one Telegram session owner. Source work flows through a
bounded durable queue into one E2 download lane and one ordered target publisher.
The SQLite database, JSON state, checkpoints, temp ownership records, and delivery
intent journal live under `./data`; never run a second container against that data
or the same user session.

An external compute helper is optional and does not make Telegram download/upload
faster. Do not deploy it on E2.1.Micro; keep the VM's limited CPU and RAM for the
main bot.

## One-time E2 setup

Run these commands after SSH login as `opc`, inside the extracted release folder:

```bash
cp .env.oracle-e2-micro.example .env
chmod 600 .env
nano .env
```

Fill the six blank Telegram/owner/target values. Do not put quotes around numeric
IDs. Save with `Ctrl+O`, Enter, then exit with `Ctrl+X`. Verify that secrets are not
printed:

```bash
grep -E '^(ROYELLS_RUNTIME_PROFILE|ROYELLS_DOWNLOAD_WORKERS|ROYELLS_UPLOAD_WORKERS|ROYELLS_IN_MEMORY_QUEUE_MAX)=' .env
mkdir -p data
chmod 700 data
docker compose -f docker-compose.oracle.yml config --quiet
docker compose -f docker-compose.oracle.yml build --pull
docker compose -f docker-compose.oracle.yml up -d
docker compose -f docker-compose.oracle.yml ps
docker logs --tail 200 -f royells-bot
```

The expected startup profile is `oracle-e2-micro`, `download=1`, `upload=1`,
Telegram API/download/publish concurrency `1/1/1`, queues `24/4`, SQLite `WAL`, and
V21 event queue `512`. `ROYELLS_ORACLE_PROFILE_LOCK=1` prevents an old `.env` from
restoring the unsafe generic profile; only the explicit `oracle-a1` profile may
override E2. The image health becomes `healthy` after startup.

## Telegram access checklist

- Add the bot account to the target channel as an administrator with permission to
  post media. The userbot remains the source reader and large-file fallback, while
  the bot account becomes an independent ordered publisher when Telegram supports
  the artifact. The attached Oracle log showed that only the userbot could resolve
  the target, so every reconnect temporarily removed the only publish route.
- Ensure the user account represented by `ROYELLS_USER_SESSION_STRING` has joined
  every private source channel. A numeric `-100...` ID is not enough when the
  session has no cached access hash. Adding workers cannot create missing Telegram
  membership or permission.
- `CHAT_FORWARDS_RESTRICTED` is expected for protected sources. The bot must download
  the original bytes with the userbot, validate them, and publish them through the
  one target publisher; it must never classify that restriction as invalid media.

## Persistent data and safe upgrades

Before replacing a release, stop the only running bot and make a filesystem copy:

```bash
docker compose -f docker-compose.oracle.yml stop -t 90 royells-bot
cp -a data "data.backup.$(date -u +%Y%m%dT%H%M%SZ)"
docker compose -f docker-compose.oracle.yml build --pull
docker compose -f docker-compose.oracle.yml up -d
```

Do not use `docker compose down -v`, do not delete `data`, and do not start the old
container at the same time. Two processes using one Telegram user session can cause
`AUTH_KEY_DUPLICATED`; two publishers can also duplicate or reorder albums.

## Diagnose an E2 host freeze

Container stdout cannot prove or disprove a kernel OOM, CPU steal, swap pressure, or
block-volume stall. Run these on the VM after a long event-loop gap:

```bash
free -h
swapon --show
vmstat 1 10
df -hT
df -i
uptime
sudo dmesg -T | egrep -i 'oom|killed process|out of memory|hung task|blocked for more|I/O error'
sudo journalctl -k --since '2 hours ago'
sudo docker stats --no-stream royells-bot
sudo docker inspect royells-bot --format '{{json .Mounts}} {{.HostConfig.Memory}} {{.HostConfig.MemorySwap}} {{.HostConfig.RestartPolicy.Name}} {{.RestartCount}}'
```

Keep at least 4 GB free on the real host-mounted data filesystem. Docker stdout is
rotated by Compose at 20 MB × 3. Swap is only an emergency buffer; it is not a way
to make E2 fast.

## Move from E2 to Ampere A1

1. Stop E2 with the 90-second command above. Confirm `docker ps` shows no old bot.
2. Copy the release, `.env` values, and the complete `data` directory to A1 over an
   encrypted channel. Verify archive/file hashes before extraction.
3. On A1, copy `.env.oracle-a1.example` to `.env`, then transfer only the six secret
   values from the old `.env`.
4. Build on A1 with `docker compose ... build --pull`. The multi-stage Dockerfile
   compiles TgCrypto for Linux ARM64 and keeps the compiler out of the final image.
5. Start A1 and confirm `profile=oracle-a1`, download capacity `1->2`, and upload
   capacity `1->1`.

Never copy an amd64 Python virtual environment, `site-packages`, or Docker image to
ARM64. Copy source plus persistent data and rebuild on A1.

## Operational limits

E2.1.Micro is a fractional, burstable 1 GB machine. It can run this reliability
profile but cannot guarantee continuous “ultra speed” during Oracle host scheduling
pauses or Telegram rate limits. A1 provides much more headroom; even there, keep the
target publisher at one because Telegram album ordering and upload-session integrity
matter more than raw worker count.
