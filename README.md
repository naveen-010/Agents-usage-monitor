# Usage Dashboard

Self-hosted dashboard + installable PWA that tracks your **Claude** and **Codex/ChatGPT**
subscription usage limits — session (5h) and weekly windows — with history, a trend
chart, and notifications (ntfy / Telegram / Discord) when a window resets or is about
to reset.

> Built for a personal server (tested on ARM64 Ubuntu). One small FastAPI service +
> SQLite, packaged with Docker Compose.

![dashboard](docs/screenshot.png) <!-- optional: add your own screenshot -->

## What it does

- **Claude** usage from `claude.ai/api/organizations/{id}/usage` (replays your `sessionKey` cookie).
- **Codex/ChatGPT** usage from `chatgpt.com/backend-api/codex/usage` (ChatGPT OAuth token;
  auto-renews via the stored refresh token).
- Shows **% remaining** per window, a **history trend chart** (% used over time), and
  records a sample every poll (default every 2 min) — 24/7, independent of the browser.
- **Notifications** on *window reset* and *reset imminent* (configurable), each carrying a
  full snapshot of every window. Channels: ntfy (with Refresh/Open buttons), Telegram, Discord.
- Installable as a **PWA** (manifest + service worker + offline shell).

## How it gets the data (and the security reality)

Neither Claude nor ChatGPT exposes an official "read my subscription usage" API, so this
replays the same private endpoints their own apps use. That means the credentials are
**full-account session credentials**, not scoped read-only tokens:

- Claude: the `sessionKey` cookie (account session).
- Codex: a ChatGPT OAuth access token (from `~/.codex/auth.json`).

They are **encrypted at rest** (Fernet) — but the key lives next to the database, so that
protects against disk/backup theft, **not** a full server compromise. Treat the box that
runs this as you would anything holding account passwords. You can revoke instantly:
log out of claude.ai everywhere (rotates the cookie) / `codex login` again (rotates the token).

Both endpoints sit behind **Cloudflare**, which fingerprints TLS and blocks plain HTTP
clients — so the adapters use [`curl_cffi`](https://github.com/lexiforest/curl_cffi) with
Chrome impersonation (pinned to a fingerprint that passes from datacenter IPs).

## Quick start (local)

```bash
cp .env.example .env          # set DASHBOARD_PASSWORD at minimum
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
# open http://localhost:8000
```

## Deploy (Docker)

```bash
cp .env.example .env          # edit: DASHBOARD_PASSWORD, PUBLIC_URL, notification channels
docker compose up -d --build
```

The dashboard binds to `127.0.0.1:8090` — put a reverse proxy (nginx/Caddy) with TLS in
front of it. Self-hosted **ntfy** is included in `docker-compose.yml` (optional; point
`NTFY_URL`/`NTFY_BASE_URL` at your own subdomain, or just use a public ntfy server).

### Reverse proxy (nginx example)

```nginx
server {
    server_name usage.example.com;
    location / { proxy_pass http://127.0.0.1:8090; proxy_set_header Host $host; }
    # ... certbot adds the TLS block
}
```

## Configuration

All via `.env` (see `.env.example`):

| Key | Purpose |
|---|---|
| `DASHBOARD_PASSWORD` | gates the dashboard + credential API (set a strong one) |
| `PUBLIC_URL` | your public HTTPS URL — enables the ntfy notification "Refresh" button |
| `POLL_INTERVAL_SECONDS` | how often to poll/record (default 120) |
| `NTFY_URL` / `NTFY_TOPIC` / `NTFY_TOKEN` | ntfy channel |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Telegram channel |
| `DISCORD_WEBHOOK_URL` | Discord channel |

## Adding your credentials

Open the dashboard → **⚙ Settings**:
- **Claude:** paste the `sessionKey` cookie value from claude.ai (DevTools → Application →
  Cookies → the row named exactly `sessionKey`). It's `HttpOnly`, so it must come from
  DevTools, not a script.
- **Codex:** paste the contents of `~/.codex/auth.json` (includes the refresh token for
  auto-renew), or just the `access_token`.

## Architecture

```
FastAPI (app/) ── providers/{claude,codex}.py  (curl_cffi → Cloudflare-protected APIs)
      │            base.py = Provider protocol → normalized Metric(label, %used, resets_at)
      ├── SQLite (encrypted creds + usage history)
      ├── APScheduler → polls every N seconds, records samples, fires notifications
      ├── notify.py → ntfy / Telegram / Discord fan-out
      └── static/ → vanilla-JS PWA (cards, canvas trend chart, settings)
```

Adding a provider is one file: implement `validate()` + `fetch()` returning normalized
`Metric`s; the scheduler, DB, API, and UI need no changes.

## Security notes

- Credentials encrypted at rest (Fernet); never displayed back via the API.
- Dashboard gated by a password (constant-time check, httpOnly cookie); rate-limit login at
  the proxy.
- App binds to localhost; expose only via your TLS reverse proxy.
- Outbound traffic only to: `claude.ai`, `chatgpt.com`, `auth.openai.com`, and your
  configured notification endpoints.

## License

MIT — see [LICENSE](LICENSE).
