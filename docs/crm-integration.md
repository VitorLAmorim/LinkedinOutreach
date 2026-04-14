# CRM Integration Guide

Contract for plugging an external CRM into OpenOutreach. Covers:

1. **Webhooks** — push events OpenOutreach sends to your CRM (`connection.accepted`, `message.received`, `message.sent`, `message.failed`).
2. **REST API** — CRUD endpoints the CRM calls into OpenOutreach (accounts, campaigns, leads, deals, messages) plus two bulk sync endpoints.
3. **Shared payload shapes** — the `MessagePacket`, `DealPacket`, and `ParsedLead` objects used by both webhooks and sync endpoints.

> **Note on transport:** OpenOutreach pushes events over **HTTP webhooks** (POST to a URL your CRM exposes). There is no websocket support. If you need real-time bidirectional, see the REST API + webhook combination below.

---

## Authentication

### REST API
Every request must include:

```
Authorization: Bearer <API_KEY>
```

`API_KEY` is the env var set on the admin container. Missing or wrong key → `401 Unauthorized`. If `API_KEY` is not configured server-side → `503`.

### Webhooks
OpenOutreach POSTs to `WEBHOOK_URL`. If `WEBHOOK_SECRET` is set, every request includes:

```
X-Webhook-Secret: <secret>
Content-Type: application/json
```

Retry: 3 attempts with exponential backoff (1s → 10s). Non-2xx responses are retried. Your endpoint should return 2xx within 10 seconds.

---

## Shared Payload Shapes

All shapes use **snake_case** to match the existing REST surface. All timestamps are ISO 8601 in UTC.

### `MessagePacket`

Emitted by `message.*` webhooks and by `GET /api/sync/messages/`.

```json
{
  "linkedin_urn": "urn:li:fsd_message:2-NTg...",
  "public_identifier": "alice-smith-123",
  "first_name": "Alice",
  "last_name": "Smith",
  "linkedin_url": "https://www.linkedin.com/in/alice-smith-123/",
  "message": "Thanks for reaching out!",
  "is_outgoing": false,
  "created_at": "2026-04-13T10:15:32.411000+00:00",
  "campaign_id": 7
}
```

| Field | Type | Notes |
|---|---|---|
| `linkedin_urn` | string | `ChatMessage.linkedin_urn` — unique, dedup key |
| `public_identifier` | string | Lead slug from the LinkedIn URL |
| `first_name`, `last_name` | string | From `Lead.first_name` / `Lead.last_name` |
| `linkedin_url` | string | `Lead.linkedin_url` — canonical LinkedIn profile URL |
| `message` | string | Message body text |
| `is_outgoing` | boolean | `true` = we sent it; `false` = received |
| `created_at` | string (ISO 8601) | `ChatMessage.creation_date` |
| `campaign_id` | integer \| null | Active campaign at the time the message was synced |

### `DealPacket`

Emitted by `connection.accepted` and `GET /api/sync/deals/`.

```json
{
  "deal_id": 42,
  "campaign_id": 7,
  "account_username": "vitor",
  "state": "Connected",
  "closing_reason": null,
  "reason": "LLM qualified: strong ICP match on role + industry",
  "connect_attempts": 1,
  "created_at": "2026-04-10T09:00:00+00:00",
  "updated_at": "2026-04-13T10:15:32+00:00",
  "lead": { /* ParsedLead, see below */ }
}
```

| Field | Type | Notes |
|---|---|---|
| `deal_id` | integer | `Deal.pk` |
| `campaign_id` | integer | `Deal.campaign_id` |
| `account_username` | string | The OpenOutreach account (LinkedIn profile) that owns this deal |
| `state` | enum | `Qualified`, `Ready to Connect`, `Pending`, `Connected`, `Completed`, `Failed` |
| `closing_reason` | enum \| null | `COMPLETED`, `FAILED`, `DISQUALIFIED` (set when state is `Completed`/`Failed`) |
| `reason` | string | Human-readable qualification / failure reason |
| `connect_attempts` | integer | Number of connect retries |
| `created_at` | string (ISO 8601) | `Deal.creation_date` |
| `updated_at` | string (ISO 8601) | `Deal.update_date` |
| `lead` | object | Fully parsed lead — see `ParsedLead` below |

### `ParsedLead`

The flat `Lead.profile_data` JSON blob (raw Voyager response) is normalized into structured sub-objects the CRM can ingest directly. Keys are stable across leads; anything that can be missing from LinkedIn's response is `null` or an empty array.

```json
{
  "public_identifier": "alice-smith-123",
  "linkedin_urn": "urn:li:fsd_profile:ACoAAA...",
  "linkedin_url": "https://www.linkedin.com/in/alice-smith-123/",
  "first_name": "Alice",
  "last_name": "Smith",
  "full_name": "Alice Smith",
  "headline": "Senior Engineer @ Acme",
  "summary": "Building distributed systems...",
  "industry": "Software Development",
  "company_name": "Acme",
  "connection_degree": 1,
  "geo": {
    "country": "United States",
    "country_code": "US",
    "location_name": "San Francisco Bay Area",
    "city": null
  },
  "experience": [
    {
      "title": "Senior Engineer",
      "company_name": "Acme",
      "company_urn": "urn:li:fs_company:12345",
      "location": "San Francisco, CA",
      "description": "Led the payments platform rewrite.",
      "start_date": { "year": 2022, "month": 3 },
      "end_date": null,
      "is_current": true
    },
    {
      "title": "Engineer",
      "company_name": "Initech",
      "company_urn": null,
      "location": "Austin, TX",
      "description": null,
      "start_date": { "year": 2018, "month": 7 },
      "end_date": { "year": 2022, "month": 2 },
      "is_current": false
    }
  ],
  "education": [
    {
      "school_name": "Stanford University",
      "degree_name": "BS",
      "field_of_study": "Computer Science",
      "start_date": { "year": 2014, "month": 9 },
      "end_date": { "year": 2018, "month": 6 }
    }
  ],
  "languages": ["en_US", "pt_BR"]
}
```

#### `geo` sub-object

| Field | Source | Notes |
|---|---|---|
| `country` | Resolved from `profile_data.geo.defaultLocalizedName` or `location_name` tail | Best-effort; may be null |
| `country_code` | `profile_data.country_code` | ISO 3166-1 alpha-2; may be null |
| `location_name` | `profile_data.location_name` | Freeform LinkedIn location label ("San Francisco Bay Area") |
| `city` | Parsed from `location_name` when it's a clean "City, Country" or "City, State, Country" | **Often null** — LinkedIn does not separate city/region cleanly. Use `location_name` for display |

#### `experience` array (sorted newest → oldest by `start_date`)

| Field | Source | Notes |
|---|---|---|
| `title` | `Position.title` | |
| `company_name` | `Position.company_name` | |
| `company_urn` | `Position.company_urn` | LinkedIn company URN; `null` for companies not in LinkedIn's graph |
| `location` | `Position.location` | Job location string |
| `description` | `Position.description` | |
| `start_date` | `Position.date_range.start` | `{year, month}`; `month` may be null |
| `end_date` | `Position.date_range.end` | `null` → current role |
| `is_current` | computed | `true` iff `end_date` is null |

#### `education` array (sorted newest → oldest by `start_date`)

| Field | Source | Notes |
|---|---|---|
| `school_name` | `Education.school_name` | |
| `degree_name` | `Education.degree_name` | e.g. "BS", "MBA" |
| `field_of_study` | `Education.field_of_study` | |
| `start_date`, `end_date` | `Education.date_range.*` | |

#### Missing-data policy

Fields that LinkedIn does not return are `null` (scalars) or `[]` (arrays). The CRM should **not** treat missing fields as errors — the profile enrichment is best-effort and re-runs on access.

---

## Webhook Events

All webhook envelopes share this shape:

```json
{
  "event": "<event_type>",
  "timestamp": "2026-04-13T10:15:32+00:00",
  "data": { /* event-specific payload */ }
}
```

### `connection.accepted`

Fired when a deal transitions to `Connected` (either via `check_pending` detecting an accepted invite, or on initial connect if already a 1st-degree connection).

```json
{
  "event": "connection.accepted",
  "timestamp": "2026-04-13T10:15:32+00:00",
  "data": { /* DealPacket with lead.* fully parsed */ }
}
```

Fires exactly once per state transition into `Connected`. If a deal is already `Connected` and another check runs, no webhook fires.

### `message.received`

Fired when `check_inbox` detects a new incoming message (`is_outgoing=false`, `creation_date > last_checked_at`).

```json
{
  "event": "message.received",
  "timestamp": "2026-04-13T10:15:32+00:00",
  "data": { /* MessagePacket, is_outgoing=false */ }
}
```

### `message.sent`

Fired after `handle_send_message` successfully delivers a message via Voyager (or browser fallback).

```json
{
  "event": "message.sent",
  "timestamp": "2026-04-13T10:15:32+00:00",
  "data": { /* MessagePacket, is_outgoing=true */ }
}
```

### `message.failed`

Fired when send fails (lead not found, API error, browser failure). Same shape as `message.sent` plus an `error` field:

```json
{
  "event": "message.failed",
  "timestamp": "2026-04-13T10:15:32+00:00",
  "data": {
    "public_identifier": "alice-smith-123",
    "first_name": "Alice",
    "last_name": "Smith",
    "linkedin_url": "https://www.linkedin.com/in/alice-smith-123/",
    "linkedin_urn": null,
    "message": "Hi Alice!",
    "is_outgoing": true,
    "created_at": null,
    "campaign_id": 7,
    "error": "Lead not found"
  }
}
```

`linkedin_urn` and `created_at` may be null if the message was never persisted to `ChatMessage`.

---

## REST API

All endpoints are under `/api/`. All require the Bearer token header.

### Accounts

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/accounts/` | List non-archived accounts |
| `POST` | `/api/accounts/` | Create account (requires `username`, `linkedin_username`, `linkedin_password`) |
| `GET` | `/api/accounts/{id}/` | Retrieve account |
| `PATCH` | `/api/accounts/{id}/` | Update limits / credentials / active flag |
| `DELETE` | `/api/accounts/{id}/` | Soft-delete (`is_archived=true`, `active=false`) |

**Account object:**
```json
{
  "id": 1,
  "username": "vitor",
  "linkedin_username": "vitor@example.com",
  "active": true,
  "is_archived": false,
  "legal_accepted": true,
  "connect_daily_limit": 20,
  "connect_weekly_limit": 100,
  "follow_up_daily_limit": 30,
  "subscribe_newsletter": true,
  "active_campaign_id": 7,
  "proxy_url": "http://user:pass@host:6754/",
  "claimed_by": "worker-pool-3-1",
  "last_heartbeat": "2026-04-13T21:48:11.124000+00:00"
}
```

`POST /api/accounts/` body accepts the same fields plus `linkedin_password` (write-only, never returned). All numeric limits are optional with sane defaults. `proxy_url` is writable via `POST` / `PATCH`; `claimed_by` and `last_heartbeat` are read-only (set by the worker pool — see "Cluster operations" below). See the **Cluster operations** section for how `proxy_url` changes propagate to a running worker and how the pool claim model works.

### Campaigns

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/campaigns/?account_id=&active=` | List, filterable |
| `POST` | `/api/campaigns/` | Create (inactive by default) |
| `GET` | `/api/campaigns/{id}/` | Retrieve with deal stats |
| `PATCH` | `/api/campaigns/{id}/` | Update |
| `DELETE` | `/api/campaigns/{id}/` | Hard delete |
| `POST` | `/api/campaigns/{id}/activate/` | Atomic swap — deactivates the account's previously-active campaign |
| `POST` | `/api/campaigns/{id}/deactivate/` | |
| `GET` | `/api/campaigns/{id}/deals/?state=&limit=&offset=` | Deals in one campaign |
| `GET` | `/api/campaigns/{id}/stats/` | Deal counts grouped by state |

**Campaign object:**
```json
{
  "id": 7,
  "name": "Q2 Enterprise Outbound",
  "account_id": 1,
  "campaign_objective": "Book discovery calls with VP Eng at 500+ person SaaS",
  "product_docs": "...",
  "booking_link": "https://cal.com/vitor/discovery",
  "is_freemium": false,
  "action_fraction": 0.2,
  "seed_public_ids": ["jdoe", "asmith"],
  "active": true
}
```

`GET /api/campaigns/{id}/` additionally includes a `stats` object keyed by ProfileState value.

**Constraint:** only one campaign per account may be `active=true`. Activating a new campaign atomically deactivates the previous one — your CRM should use `POST .../activate/` rather than `PATCH` with `active=true`.

### Leads (read-only)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/leads/{public_id}/` | Lead detail (cache-only, no Voyager fetch) |
| `GET` | `/api/leads/{public_id}/deals/` | All deals for a lead across campaigns |

The single-lead endpoint returns the raw `profile_data` as the `profile` field. For the **parsed** shape, use `GET /api/sync/deals/` which embeds `ParsedLead`.

### Deals (read-only)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/deals/{id}/` | Single deal (lean, no parsed profile) |

### Messages

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/messages/send/` | Enqueue a send — returns `202 {task_id, status}` |
| `GET` | `/api/messages/{public_id}/` | Conversation history for one lead (DB-only) |
| `GET` | `/api/tasks/{task_id}/` | Poll task status (any task, not just messages) |

`POST /api/messages/send/` body:
```json
{
  "campaign_id": 7,
  "public_id": "alice-smith-123",
  "message": "Hi Alice!"
}
```

`send_message` tasks are **priority-scheduled** — they always run as the next task the daemon picks up, ahead of any pending `connect`/`follow_up`/`check_pending`/`check_inbox` tasks. Expect single-digit-second latency in normal operation.

### Sync (bulk, paginated) — **NEW**

Both sync endpoints use the same pagination envelope:

```json
{
  "total": 523,
  "limit": 100,
  "offset": 0,
  "items": [ /* ... */ ]
}
```

Results are sorted **ascending** by `created_at` / `updated_at` so incremental sync works with `since=<last_seen>`.

#### `GET /api/sync/messages/`

Returns all `ChatMessage` rows as `MessagePacket` objects.

| Query param | Type | Default | Notes |
|---|---|---|---|
| `since` | ISO 8601 | — | Only messages with `creation_date > since` |
| `until` | ISO 8601 | — | Only messages with `creation_date <= until` |
| `campaign_id` | int | — | Filter to a campaign (scoped via deal) |
| `is_outgoing` | bool | — | `true` / `false` filter |
| `limit` | int | 100 | Max 1000 |
| `offset` | int | 0 | |

Response:
```json
{
  "total": 523,
  "limit": 100,
  "offset": 0,
  "messages": [ /* MessagePacket[] */ ]
}
```

#### `GET /api/sync/deals/`

Returns deals with fully parsed leads. Defaults to `state=Connected` (the common CRM pull).

| Query param | Type | Default | Notes |
|---|---|---|---|
| `state` | string | `Connected` | ProfileState value or `all` |
| `campaign_id` | int | — | Filter |
| `since` | ISO 8601 | — | `update_date > since` |
| `until` | ISO 8601 | — | |
| `limit` | int | 50 | Max 500 |
| `offset` | int | 0 | |
| `include_profile` | bool | `true` | Set `false` for lean deals without `ParsedLead` |

Response:
```json
{
  "total": 200,
  "limit": 50,
  "offset": 0,
  "deals": [ /* DealPacket[] */ ]
}
```

### Pagination convention

- Offset-based. `total` is the filtered count (respects `since`/`until`/`campaign_id`/etc).
- Results sorted ascending by timestamp. Safe for incremental sync: pass `since=<last_created_at_you_saw>` on every call.
- Hard cap on `limit` prevents accidental whole-table pulls.

---

## End-to-end flow: "sync everything nightly"

1. **Accounts + campaigns** — `GET /api/accounts/` + `GET /api/campaigns/`. Small; refetch in full.
2. **Connected deals** — `GET /api/sync/deals/?state=Connected&since={last_run}&limit=500`, paginate until `offset + len(deals) >= total`.
3. **Messages** — `GET /api/sync/messages/?since={last_run}&limit=1000`, paginate.
4. **Live updates** — subscribe to webhooks: `connection.accepted`, `message.received`, `message.sent`, `message.failed`. These fire between sync runs so your CRM stays near real-time without hammering the API.

## Cluster operations

OpenOutreach supports a dynamic worker pool: N account rows, K active campaigns, and K worker containers that auto-bind to whichever K accounts are currently eligible. The CRM drives everything via the existing REST endpoints — no separate orchestrator.

### Per-account proxy

Each `LinkedInAccount` has a `proxy_url` field (empty → falls back to the global `PROXY_URL` env var). Update it with a normal PATCH:

```
PATCH /api/accounts/42/
Authorization: Bearer <API_KEY>
Content-Type: application/json

{"proxy_url": "http://user:pass@host:port/"}
```

The worker currently running this account will pick up the change **on its next loop iteration** (≤ `_IDLE_POLL_INTERVAL = 5s` + browser relaunch ~3s). Playwright applies the proxy at `browser.new_context()` time, so the worker tears down and relaunches the browser with the new proxy automatically — no manual restart needed. The same account's cookies are preserved across the relaunch (they're stored in `LinkedInAccount.cookie_data`, not in the browser context).

### Pool claim model

Worker containers started **without** `LINKEDIN_PROFILE` set enter pool mode. On startup, each worker calls `claim_next_account(worker_id)` which atomically (`SELECT FOR UPDATE SKIP LOCKED`) picks one account that is:

- `is_archived=False`, `active=True`
- has at least one `Campaign.active=True`
- either unclaimed (`claimed_by=""`), claimed by this same worker already, or claimed by a worker whose last heartbeat is older than `STALE_CLAIM_TIMEOUT = 60s`

When a campaign is deactivated on the CRM side (`POST /api/campaigns/<id>/deactivate/`), the worker running that account's daemon notices on its next loop iteration, releases the claim, closes the browser, claims the next eligible account, and relaunches. A different container can end up running the "same" work — from the CRM's perspective, the only thing that matters is that there are K active campaigns and K running workers.

**Scaling:**

```
docker compose -f local.yml up -d --scale worker-pool=4
```

Each replica claims a different row. If you activate more campaigns than replicas, the extras wait un-worked. If you scale down, excess workers release their claims and exit cleanly on SIGTERM.

### Monitoring cluster state

`GET /api/accounts/` now returns three extra fields per account so the CRM can render a live cluster view:

```json
{
  "id": 42,
  "username": "alice",
  "linkedin_username": "alice@example.com",
  "active": true,
  "is_archived": false,
  "proxy_url": "http://user:pass@host:6754/",
  "claimed_by": "worker-pool-3-1",
  "last_heartbeat": "2026-04-13T21:48:11.124000+00:00",
  ...
}
```

- `proxy_url` — the per-account proxy (empty = uses env default).
- `claimed_by` — empty string means unclaimed. Any non-empty value is the worker-id string (`<hostname>-<pid>`) of the container currently running this account.
- `last_heartbeat` — ISO 8601; `null` when not claimed. A running worker heartbeats every 5 seconds. If this is older than 60 seconds, the row is considered stale and the next polling worker will reclaim it.

### Graceful shutdown

Containers handle `SIGTERM` and `SIGINT` by releasing their claim and closing the browser before exit. A `docker stop worker-pool-1` frees the claim in milliseconds; without the handler you'd wait up to `STALE_CLAIM_TIMEOUT` for the claim to be considered dead.

### Latency guarantees

| Event | Time until worker reacts |
|---|---|
| Campaign activated on a different account | ≤ 10s (one loop iteration + claim + browser launch) |
| Campaign deactivated on the claimed account | ≤ 10s (release + swap + relaunch) |
| `proxy_url` patched on the claimed account | ≤ 10s (proxy change + browser relaunch) |
| Crashed worker's claim freed | ≤ `STALE_CLAIM_TIMEOUT` (60s) |
| Graceful `docker stop` frees claim | ≤ 1s (SIGTERM handler) |

### Typical CRM workflow

1. **Provision**: `POST /api/accounts/` × N, `POST /api/campaigns/` × N. Set `proxy_url` inline on each account (or PATCH later).
2. **Activate K**: `POST /api/campaigns/<id>/activate/` for the K campaigns you want running right now.
3. **Scale K containers**: `docker compose up -d --scale worker-pool=K`. Each container claims one active account.
4. **Rotate**: deactivate old campaigns, activate new ones — workers rebind automatically.
5. **Live proxy change**: `PATCH /api/accounts/<id>/` with a new `proxy_url` — browser relaunches on the next loop iteration with the new exit IP.
6. **Monitor**: poll `GET /api/accounts/` for `claimed_by` + `last_heartbeat` to render a cluster status view.

## Error responses

All errors return JSON: `{"error": "<message>"}` with an appropriate HTTP status:

- `400` — malformed request body / invalid query param
- `401` — missing or wrong Bearer token
- `403` — (reserved; not currently used)
- `404` — resource not found
- `409` — uniqueness conflict (e.g. duplicate account username)
- `503` — `API_KEY` not configured server-side
