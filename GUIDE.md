# Running OpenOutreach with the Worker Pool

This guide walks through a local deployment of OpenOutreach using `local.yml`:
one Postgres container, one admin container, and N dynamically-scaled
`worker-pool` containers that claim LinkedIn accounts at runtime.

For the production Hetzner deploy (Caddy + WireGuard + `production.yml`) see
`docs/deploy.md`. For architecture details see `ARCHITECTURE.md`.

## Container layout

`local.yml` defines three services:

| Service       | Purpose                                                           | Exposed ports                         |
|---------------|-------------------------------------------------------------------|---------------------------------------|
| `postgres`    | PostgreSQL 16 — the single source of truth for accounts, campaigns, leads, deals, tasks, and worker claims. | `5440:5432` on the host |
| `admin`       | Django Admin + REST API (`runserver 0.0.0.0:8000`). Owns migrations and `setup_crm`. | `8000:8000` |
| `worker-pool` | Scalable daemon replicas. Each replica claims one eligible `LinkedInAccount` from Postgres and runs its campaign. | VNC `5900` + noVNC `6080` (ephemeral host ports, one pair per replica) |

Only `admin` and `worker-pool` are built from `compose/linkedin/Dockerfile`.
The entrypoint `compose/linkedin/start` dispatches on `RUN_MODE`:
`admin` runs `migrate && setup_crm && runserver`; the default (unset) runs
the daemon.

### Why a dedicated admin container

- It is the **only** service that runs `manage.py migrate` and `setup_crm`.
  Workers skip migrations because they depend on `postgres` being ready
  with the schema already applied.
- It hosts the REST API that operators and the pool rely on:
  `POST /api/campaigns/<id>/activate/`, `PATCH /api/accounts/<id>/`
  (proxy changes), `POST /api/messages/send/`, etc.
- It serves `/admin/` for manual CRUD on `LinkedInAccount`, `Campaign`,
  `Lead`, `Deal`, `Task`, and `SearchKeyword`.

### Why the worker pool instead of pinned workers

Pool mode = **one worker container image, N replicas**. Each replica starts
with an empty `LINKEDIN_PROFILE` and blocks on startup until it can claim
an eligible account via `linkedin/accounts/pool.py:claim_next_account`
(atomic `SELECT FOR UPDATE SKIP LOCKED`). An account is *eligible* when:

1. `LinkedInAccount.active = True`
2. `LinkedInAccount.is_archived = False`
3. It has exactly one `Campaign` with `active = True`
4. It is not currently claimed by a live worker (heartbeat within
   `STALE_CLAIM_TIMEOUT = 60s`)

If an account's campaign is deactivated mid-flight, the worker cancels its
pending tasks, releases the claim, closes the browser, and claims the
next eligible account — all without a container restart. If the account's
`proxy_url` is changed via `PATCH /api/accounts/<id>/`, the browser is
torn down and relaunched with the new proxy on the next loop iteration
(Playwright requires proxies at `new_context()` time).

Pinned mode (`LINKEDIN_PROFILE=<username>`) is commented out in `local.yml`
and exists only for local debugging against a specific account.

## Prerequisites

- Docker Engine + Compose v2 (`docker compose version` must print v2.x).
- Host user able to read/write `./data/` and `./` (the repo is bind-mounted
  into the worker container). `HOST_UID`/`HOST_GID` env vars override
  the default `1000:1000` in `local.yml`.
- Playwright needs 2 GB of `/dev/shm` per worker — already set via
  `shm_size: 2gb` on the `x-worker-base` anchor.
- Roughly 1 vCPU + 1–1.5 GB RAM per worker replica; the admin + postgres
  pair fits in ~512 MB on an idle host.

## Step 1 — Bootstrap `.env`

```bash
cp .env.copy .env
chmod 600 .env
```

Fill in at minimum:

```bash
# Required in local dev — bearer token for /api/* and for the MCP client.
API_KEY=$(openssl rand -hex 32)

# Required for any task that calls the LLM (qualification, follow-up agent,
# search-keyword generation). Any OpenAI-compatible endpoint works.
LLM_API_KEY=sk-...
AI_MODEL=gpt-4o-mini
LLM_API_BASE=          # blank → defaults to https://api.openai.com/v1

# Optional — enable if you have a webhook receiver ready.
WEBHOOK_URL=
WEBHOOK_SECRET=$(openssl rand -hex 32)

# Optional global fallback proxy. Per-account proxies on
# LinkedInAccount.proxy_url take precedence when set.
PROXY_URL=
```

Everything under the `Production:` headers in `.env.copy` (`SECRET_KEY`,
`DJANGO_ALLOWED_HOSTS`, `CADDY_*`, `WG_*`, …) is **unused by `local.yml`**.
`django_settings.py` falls back to safe dev defaults (DEBUG=True, wildcard
`ALLOWED_HOSTS`, committed dev `SECRET_KEY`) when those vars are unset.

Postgres credentials are hardcoded in `local.yml`
(`openoutreach/openoutreach/openoutreach`) so the `POSTGRES_*` vars in `.env`
are ignored locally.

Per-worker timing knobs (`MIN_DELAY`, `MAX_DELAY`, `ACTIVE_START_HOUR`,
`ACTIVE_END_HOUR`, `MIN_ACTION_INTERVAL`, etc.) are set on the
`x-worker-env` anchor in `local.yml`, not in `.env`. Edit `local.yml`
directly if you need to vary them across replicas.

## Step 2 — Bring up Postgres and admin

```bash
make build                              # build the linkedin image
docker compose -f local.yml up -d postgres admin
docker compose -f local.yml logs -f admin
```

Wait for the log line `Starting development server at http://0.0.0.0:8000/`.
On first boot the admin container runs `migrate` and `setup_crm`, which
creates the default Site row.

Create a Django superuser so you can reach the admin UI:

```bash
docker compose -f local.yml exec admin \
  .venv/bin/python manage.py createsuperuser
```

Then log in at `http://localhost:8000/admin/`.

## Step 3 — Create a LinkedInAccount

Every worker needs at least one eligible account. Two paths:

### Option A — interactive VNC onboarding (recommended for real accounts)

The account row must already exist in the DB (create it via the REST API
below or in `/admin/`) before running setup. Then:

```bash
make setup-account <username>
```

This brings up `postgres`, runs the `worker-pool` image once with
`RUN_MODE=setup` and `LINKEDIN_PROFILE=<username>`, and publishes VNC on
fixed ports `5910` (raw VNC) and `6090` (noVNC). Attach with any VNC
client or open `http://localhost:6090/vnc.html` and complete the LinkedIn
email/password + 2FA flow inside the Chromium window. The container polls
for the auth cookie, writes it to `LinkedInAccount.cookie_data`, and exits.

The browser launches with the account's stored `proxy_url`
(`_resolve_proxy_url(account)` falls back to env `PROXY_URL` if unset), so
the login and all subsequent Voyager traffic go through the same proxy the
daemon will use.

### Option B — REST API (for automation / testing)

```bash
curl -sS -X POST http://localhost:8000/api/accounts/ \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "alice",
    "linkedin_username": "alice@example.com",
    "linkedin_password": "hunter2",
    "active": true
  }'
```

You still need to log that account in at least once via `RUN_MODE=setup` to
populate `cookie_data` before the daemon can hit Voyager.

Optionally attach a per-account proxy (overrides env `PROXY_URL`):

```bash
curl -sS -X PATCH http://localhost:8000/api/accounts/<id>/ \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"proxy_url": "http://user:pass@proxy.example.com:8080/"}'
```

The running worker picks up the new proxy on its next loop iteration
(browser is torn down and relaunched).

## Step 4 — Create and activate a Campaign

A `LinkedInAccount` is only eligible for a worker claim when exactly one
of its campaigns has `active=True`. The partial unique constraint
`one_active_campaign_per_account` enforces that; activating a new campaign
atomically deactivates the previous one.

```bash
# Create a campaign (starts with active=False)
curl -sS -X POST http://localhost:8000/api/campaigns/ \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Q2-outbound-sdr",
    "account": <account-id>,
    "campaign_objective": "Book discovery calls with VP-Eng at 50-500 person SaaS companies.",
    "booking_link": "https://cal.com/you/discovery",
    "seed_public_ids": ["some-public-id", "another-public-id"]
  }'

# Activate it — atomically deactivates any other active campaign on the same account.
curl -sS -X POST http://localhost:8000/api/campaigns/<campaign-id>/activate/ \
  -H "Authorization: Bearer $API_KEY"
```

You can also do both in the Django admin (`/admin/linkedin/campaign/`).
Until at least one campaign is active, pool workers idle at
`_IDLE_POLL_INTERVAL = 5s` polling the DB — they do not crash.

## Step 5 — Start the worker pool

```bash
docker compose -f local.yml up -d --scale worker-pool=4
docker compose -f local.yml logs -f worker-pool
```

Each replica:

1. Boots with `LINKEDIN_PROFILE=""` and no `--profile` flag.
2. Runs `premigrations` + `migrate` (idempotent — no-op after the admin
   container has done it).
3. Blocks on `claim_next_account(worker_id)` until an eligible account is
   available, then swaps it onto the session.
4. Enters the daemon loop: active-hours guard → pool refresh → proxy
   refresh → campaign hot-swap check → dequeue one task → execute →
   repeat.

Check worker assignment:

```bash
curl -sS http://localhost:8000/api/accounts/ \
  -H "Authorization: Bearer $API_KEY" | jq '.[] | {username, claimed_by, last_heartbeat}'
```

A fresh `claimed_at` and a `last_heartbeat` within the last 5s means the
worker is running. Empty `claimed_by` means the account is idle (no active
campaign, archived, or simply more accounts than workers).

### Viewing a worker's browser via VNC

Pool replicas get ephemeral host ports. Find them with:

```bash
docker compose -f local.yml port --index 1 worker-pool 6080
# → 0.0.0.0:32801
docker compose -f local.yml port --index 1 worker-pool 5900
# → 0.0.0.0:32802
```

Then open `http://localhost:32801/vnc.html` in a browser, or connect a
VNC client to `vnc://localhost:32802`.

### Scaling up and down

```bash
# Add two more workers:
docker compose -f local.yml up -d --scale worker-pool=6

# Stop a worker cleanly (releases the claim via SIGTERM handler):
docker compose -f local.yml up -d --scale worker-pool=5
```

`linkedin/browser/session.install_shutdown_handler()` hooks SIGTERM/SIGINT
to release the claim immediately, so `docker stop` does not leave a dead
claim waiting for the 60-second `STALE_CLAIM_TIMEOUT` to expire.

## Step 6 — Enqueue manual sends (optional)

Manual messages enqueued via the REST API sort ahead of other task types
(`TaskQuerySet.pending()` annotates a priority `CASE`), so the worker
picks them up as the next task without preempting anything in-flight:

```bash
curl -sS -X POST http://localhost:8000/api/messages/send/ \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "campaign_id": <campaign-id>,
    "public_id": "target-public-id",
    "message": "Quick question about your stack — open to chat?"
  }'
# → 202 Accepted, {"task_id": 42}

# Poll the task:
curl -sS http://localhost:8000/api/tasks/42/ \
  -H "Authorization: Bearer $API_KEY"
```

## Operational cheatsheet

```bash
# Tail everything
docker compose -f local.yml logs -f

# Tail only workers
docker compose -f local.yml logs -f worker-pool

# Run the test suite inside the admin image (SQLite, no volumes)
make docker-test

# Shell into Postgres
docker compose -f local.yml exec postgres \
  psql -U openoutreach -d openoutreach

# Stop everything (keeps the pgdata volume)
make stop

# Rebuild after code or requirements changes
make build && docker compose -f local.yml up -d
```

## Troubleshooting

- **Worker idles forever**: check `LinkedInAccount.active`,
  `is_archived`, and that exactly one `Campaign.active=True` exists for
  that account. Verify via `GET /api/accounts/` and
  `GET /api/campaigns/`.
- **Worker logs `AuthenticationError` on every task**: the account's
  `cookie_data` is stale. Run `RUN_MODE=setup` again to re-login.
- **Worker logs `playwright: TimeoutError` on navigation**: the proxy is
  slow or broken. `PATCH /api/accounts/<id>/` with a new `proxy_url` or
  clear it to fall back to `PROXY_URL`.
- **Two workers claim the same account**: cannot happen under normal
  operation — the claim uses `SELECT FOR UPDATE SKIP LOCKED`. If
  `claimed_by` looks stuck on a dead container, wait 60s for
  `STALE_CLAIM_TIMEOUT` to expire or run
  `UPDATE linkedin_linkedinaccount SET claimed_by='', claimed_at=NULL WHERE id=<id>;`.
- **`API_KEY must be set in .env`** on `docker compose up`: the `admin`
  service's env block uses `${API_KEY:?...}` — populate `.env` before
  running compose.
