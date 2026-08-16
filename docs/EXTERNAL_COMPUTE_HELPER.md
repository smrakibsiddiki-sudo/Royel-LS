# Optional External Compute Helper

## Purpose

Royells keeps its production delivery path on the main bot:

```text
Telegram source -> 1–2 controlled downloaders -> local validation + durable ledger
                -> one ordered publisher -> target channel
```

The separate compute helper is optional. It can perform bounded CPU/I/O work such as SHA-256 calculation and advisory MP4 metadata inspection. It is **not** a Telegram worker and cannot bypass forwarding restrictions, peer access, FloodWait, Telegram upload sessions, or `FILE_PART_X_MISSING`.

## Strict trust boundary

Never give the helper any of these values or files:

- `ROYELLS_USER_SESSION_STRING`, API ID/hash, bot token, owner ID, or target ID.
- `/data/royells_media_bot`, SQLite, JSON state, checkpoints, delivery ledger, Telegram sessions, or source list.
- A shared Docker volume with the main bot.

The helper receives at most one explicitly submitted media artifact, performs bounded inspection in a private temporary directory, returns signed JSON, and deletes the temporary file. It has no Telegram dependency.

The main bot remains the only authority for Telegram download/copy fallback/upload/album order; delivery intents, duplicate checks, queue ownership and retries; dead-media decisions; SQLite/JSON state; and owner notifications. Therefore helper outage, delay, compromise, or disablement cannot make the main delivery path lose or dead-list media.

## Included components

| File | Purpose |
| --- | --- |
| `royells_compute_helper.py` | Standard-library-only bounded file/bytes inspector with no socket, environment, Telegram or credential access. |
| `royells_compute_helper_service.py` | Authenticated standalone HTTP service. |
| `royells_compute_helper_client.py` | Explicit operator/sidecar client that verifies the signed response. It is not imported by the bot. |
| `Dockerfile.compute-helper` | Minimal helper-only image; it copies no bot code, state or session. |
| `docker-compose.compute-helper.yml` | Hardened local/private deployment example. |
| `compute_helper.env.example` | Non-secret configuration template. |

## Protocol and security

`POST /v1/inspect` accepts `application/octet-stream` with these HMAC-SHA256 protected headers:

- `X-Royells-Request-Id`: unique 16–96-character request ID.
- `X-Royells-Timestamp`: Unix epoch accepted only within a bounded skew.
- `X-Royells-Content-SHA256`: hash calculated by the submitter.
- `X-Royells-Filename`: display-only name, never a server-side path.
- `X-Royells-Signature`: HMAC over protocol, method, path, timestamp, request ID, byte length, content hash and display name.

Responses have the request ID, response-body SHA-256 and a second HMAC signature. The service enforces a request-size limit, one bounded inflight inspection by default, read timeout, replay cache, private temporary files, regular-file checks, symlink rejection, SHA-256 coherence and bounded MP4 parsing.

Run it behind HTTPS or a private tunnel. HMAC authenticates the peer and payload; it does **not** encrypt media. Prefer mTLS at the reverse proxy when available. Never expose the raw helper port publicly without HTTPS, a random 32+-byte helper-only secret, and an IP/access policy.

## Deploy separately

Generate a dedicated secret locally or in your host's secret manager:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Store it only as `ROYELLS_COMPUTE_HELPER_SHARED_SECRET` in the helper host's secret manager. Do not add it to `Dockerfile`, a committed `.env` file, the current main-bot Space secrets, or Telegram chat.

For the private Compose example, first copy `compute_helper.env.example` to an
untracked `compute_helper.env` and replace its placeholder. The service rejects
the sample placeholder and will fail closed if a real helper-only secret is not
provided.

Build and run on a separate machine/container:

```powershell
docker build -f Dockerfile.compute-helper -t royells-compute-helper .
docker run --rm -p 127.0.0.1:8080:8080 --env-file compute_helper.env royells-compute-helper
```

For a public cloud/free host, route its HTTPS domain or private tunnel to the helper's internal port. The service has no persistent state requirement and does not need the main bot's disk. A free host can be useful for testing, but it does not replace the persistent disk/database required by the main bot.

The included Compose file is suitable for a private Linux host:

```powershell
docker compose -f docker-compose.compute-helper.yml up --build -d
```

## Verify without touching the bot

Create a test file you are authorized to send, set the same helper-only secret in your terminal, and call the explicit client:

```powershell
$env:ROYELLS_COMPUTE_HELPER_SHARED_SECRET = "your-helper-only-secret"
python royells_compute_helper_client.py `
  --url "https://helper.example.com/v1/inspect" `
  --file "C:\safe-test\sample.mp4"
```

The client refuses plaintext remote URLs, redirects, weak secrets, invalid response signatures and response/file-hash mismatches. `--allow-insecure-loopback` is available only for a local `http://127.0.0.1` smoke test.

## Operational rules

1. Keep the main bot at download `1 -> adaptive 2`, upload `1`, Telegram media concurrency `1`, and album concurrency `1`. Raising upload workers does not increase safe speed and can revive duplicate/order/`FILE_PART` failures.
2. Do not automatically stream live in-flight Telegram artifacts to a remote helper. That duplicates WAN traffic and can race file cleanup. A future production artifact-offload design needs an object store, signed object IDs, reference counts and an explicit privacy-retention policy.
3. Treat every helper result as **advisory**. It cannot make a media item dead, trigger an upload, alter a retry, or replace local validation/delivery ledger.
4. If the helper times out, returns bad HMAC, reaches its limit, or disappears, leave the main bot's local pipeline unchanged. No media is dropped or reclassified because of helper availability.

## Why this is the safe speed model

Most observed Royells bottlenecks are Telegram-side transfer/session limits, not local checksum CPU. A remote helper can isolate future CPU-heavy work, but it cannot bypass forward restrictions, private-channel access, FloodWait or Telegram upload sessions. The proven speed/reliability path remains:

```text
bounded parallel source downloads + durable per-media retries + one ordered target publisher
```

That is why the external helper is opt-in and separate, rather than a second Telegram worker that would compete for the same account and destabilize the delivery ledger.
