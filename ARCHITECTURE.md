# Architecture

Detailed module documentation for OpenOutreach. See `CLAUDE.md` for rules and quick reference.

## Entry Flow

`manage.py` — stock Django management entrypoint. Bare `python manage.py` (no args) defaults to `rundaemon`.

### `rundaemon` management command (`management/commands/rundaemon.py`)

Startup sequence:
1. **Configure logging** — DEBUG level, suppresses noisy third-party loggers (urllib3, httpx, langchain, openai, playwright, etc.).
2. **Ensure DB** — `run_premigrations()` (filesystem migrations) → `migrate --no-input` + `setup_crm` (idempotent).
3. **Onboard** — checks `missing_keys()`; if incomplete: uses interactive wizard (TTY) or exits with clear error (no TTY). Onboarding creates a `LinkedInAccount` directly — no Django auth User involved.
4. **Validate** — `LLM_API_KEY` and an active `LinkedInAccount`. Campaigns are optional at startup; the worker will idle until one is activated via the API.
5. **Session** — `get_or_create_session(account)`, sets the default campaign (first non-freemium) if any are active.
6. **Newsletter** — GDPR override + `ensure_newsletter_subscription()` (marker-guarded, runs once).
7. **Run** — `run_daemon(session)`.

Docker `start` script dispatches on `RUN_MODE` env var: `admin` (Django Admin web server, no browser), `setup` (interactive VNC login), `browse` (browser + VNC, no daemon), default (daemon with `--profile` if `LINKEDIN_PROFILE` set).

### Other management commands

- `onboard` — standalone onboarding (interactive or `--non-interactive` with `--config-file` / individual flags).
- `setup_account <username>` — interactive VNC login for a LinkedIn account (polls for auth cookie, saves session).
- `browse_account <username>` — launches Playwright with the account's `proxy_url` + saved `cookie_data` (if any), navigates to `/feed` (or `/login` if no cookies), then blocks on `signal.pause()` for free VNC use. No login detection, no timeout — runs until `docker stop` / Ctrl+C. SIGTERM/SIGINT close the browser cleanly.
- `setup_crm` — idempotent CRM bootstrap (default Site).
- `add_seeds` — add seed LinkedIn profile URLs to a campaign.

## Onboarding (`onboarding.py`)

`OnboardConfig` — pure dataclass with all onboarding fields. Two constructors:
- `OnboardConfig.from_json(path)` — from JSON file (cloud / non-interactive).
- `collect_from_wizard()` — interactive questionary wizard (needs TTY), only asks for `missing_keys()`.

Single write path: `apply(config)` — idempotent, creates missing LinkedInAccount, Campaign, and legal acceptance. Three components:

1. **LinkedInAccount** — email, password, newsletter, rate limits. `username` is derived from the email slug. No Django User created.
2. **Campaign** — name, product docs, objective, booking link, seed URLs. New campaigns are created with `active=False` and an `account` FK; the operator activates one via `POST /api/campaigns/<id>/activate/`.
3. **Legal notice** — per-account acceptance stored as `LinkedInAccount.legal_accepted`.

LLM config (`LLM_API_KEY`, `AI_MODEL`, `LLM_API_BASE`) is NOT part of onboarding — it lives in `.env` and is read at process start via `linkedin.conf.get_llm_config()`. Rotating a key requires updating `.env` and restarting the admin + worker containers.

## Profile State Machine

`enums.py:ProfileState` (TextChoices) values ARE CRM stage names: QUALIFIED, READY_TO_CONNECT, PENDING, CONNECTED, COMPLETED, FAILED. Pre-Deal states: url_only (no profile_data), enriched (has profile_data). `Lead.disqualified=True` = permanent account-level exclusion. LLM rejections = FAILED Deals with "Disqualified" closing reason (campaign-scoped).

`crm/models/deal.py:ClosingReason` (TextChoices): COMPLETED, FAILED, DISQUALIFIED. Used by `Deal.closing_reason`.

## Task Queue

Persistent queue backed by `Task` model. Worker loop in `daemon.py`: `seconds_until_active()` guard pauses outside active hours/rest days → `_refresh_active_campaign()` checks whether the active campaign for this worker's account changed → pop oldest due task → set campaign on session → RUNNING → dispatch via `_HANDLERS` dict → COMPLETED/FAILED/CANCELLED. Failures captured by `failure_diagnostics()` context manager. `heal_tasks()` reconciles on startup or after a hot-swap (scoped to own campaigns — won't reset other daemons' running tasks). `AuthenticationError` (401) triggers `session.reauthenticate()` and resets the task to pending for automatic retry.

**Hot-swap (one active campaign per account):** A partial unique constraint on `Campaign` enforces at most one `active=True` row per `account_id`. `POST /api/campaigns/<id>/activate/` runs an atomic swap with `SELECT FOR UPDATE` on the account row. The daemon checks `get_active_campaign_id(session.account)` at the top of every loop iteration; on a change it cancels pending tasks for the old campaign (status `cancelled`), busts `session.invalidate_campaigns_cache()`, rebuilds qualifiers, and re-runs `heal_tasks()`. Workers idle (5s polls) when no campaign is active for their account.

Five task types (handlers in `linkedin/tasks/`, signature: `handle_*(task, session, qualifiers)`):

1. **`handle_connect`** — Unified via `ConnectStrategy` dataclass. Regular: `find_candidate()` from `pools.py`; freemium: `find_freemium_candidate()`. Unreachable detection after `MAX_CONNECT_ATTEMPTS` (3). Spread-delay scheduling via `compute_spread_delay()`: distributes requests across the active window (`remaining_active_seconds / remaining_daily_quota`). Post-connection delay uses the full interval (floored by `min_action_interval`); search-only retries use 25% of the interval. Freemium campaigns keep their own `action_fraction` scaling.
2. **`handle_check_pending`** — Per-profile. Exponential backoff with jitter. On acceptance → enqueues `follow_up`.
3. **`handle_follow_up`** — Per-profile. Calls `run_follow_up_agent()` which returns a `FollowUpDecision` (structured output: `send_message`/`mark_completed`/`wait`). Handler executes the decision deterministically.
4. **`handle_send_message`** — API-driven. Enqueued by `POST /api/messages/send/`. Resolves profile from Deal or Lead fallback, ensures the lead URN is cached, then calls `send_raw_message()` which **defaults to the Voyager `createMessage` API** (single-digit-second sends) and only falls back to browser navigation strategies if the API call fails. Syncs conversation to ChatMessage, fires `message.sent`/`message.failed` webhook.
5. **`handle_check_inbox`** — Self-rescheduling. Polls recent LinkedIn conversations via `fetch_conversations()`, syncs new messages for known Leads, fires `message.received` webhook for each new incoming message. Tracks `last_checked_at` in task payload. Seeded by `heal_tasks()` when `WEBHOOK_URL` is configured.

## Qualification ML Pipeline

GPR (sklearn, ConstantKernel * RBF) inside Pipeline(StandardScaler, GPR) with BALD active learning:

1. **Balance-driven selection** — n_negatives > n_positives → exploit (highest P); otherwise → explore (highest BALD).
2. **LLM decision** — All decisions via LLM (`qualify_lead.j2`). GP only for candidate selection and confidence gate.
3. **READY_TO_CONNECT gate** — P(f > 0.5) above `min_ready_to_connect_prob` (0.9) promotes QUALIFIED → READY_TO_CONNECT.

384-dim FastEmbed embeddings stored directly on Lead model, per-campaign GP models at ``Campaign.model_blob` (BinaryField)`. Cold start returns None until >=2 labels of both classes.

## Django Apps

Three apps in `INSTALLED_APPS`:

- **`linkedin`** — Main app: `LinkedInAccount`, `Campaign` (with `account` FK), `SearchKeyword`, `ActionLog`, `Task` models. All automation logic. Django auth User is kept only for `/admin/` login and is no longer referenced by any business model.
- **`crm`** — Lead (with embedding) and Deal models (in `crm/models/lead.py` and `crm/models/deal.py`). Also defines `ClosingReason` enum.
- **`chat`** — `ChatMessage` model (GenericForeignKey to any object, content, answer_to threading, topic). The `owner` field is nullable and unused after the account refactor.

## CRM Data Model

- **SiteConfig** (`linkedin/models.py`) — Legacy singleton table. LLM config has moved to `.env` env vars (`LLM_API_KEY`, `AI_MODEL`, `LLM_API_BASE`); the model still exists to keep migration 0003 valid but is no longer read by runtime code. Not registered in Django Admin.
- **LinkedInAccount** (`linkedin/models.py`) — Standalone account model (renamed from `LinkedInProfile`, no Django User FK). Fields: `username` (unique, replaces the old Django username), `linkedin_username`, `linkedin_password`, `cookie_data`, `self_lead` FK to Lead, `subscribe_newsletter`, `active`, `is_archived` (soft-delete), rate limits (`connect_daily_limit`, `connect_weekly_limit`, `follow_up_daily_limit`), `legal_accepted`, `newsletter_processed`, `proxy_url` (per-account proxy, overrides env `PROXY_URL`), `claimed_by` (worker id — empty string = unclaimed, db_index), `claimed_at`, `last_heartbeat`. Methods: `can_execute`/`record_action`/`mark_exhausted`. In-memory `_exhausted` dict for daily rate limit caching.
- **Campaign** (`linkedin/models.py`) — `name` (unique), `account` FK (was `users` M2M), `product_docs`, `campaign_objective`, `booking_link`, `is_freemium`, `action_fraction`, `seed_public_ids` (JSONField), `active` (default `False` — flipped via API). Constraint: `one_active_campaign_per_account` partial unique index on `(account)` where `active=True`.
- **SearchKeyword** (`linkedin/models.py`) — FK to Campaign. `keyword`, `used`, `used_at`. Unique on `(campaign, keyword)`.
- **ActionLog** (`linkedin/models.py`) — FK to LinkedInAccount (`account`) + Campaign. `action_type` (connect/follow_up), `created_at`. Composite index on `(account, action_type, created_at)`.
- **Lead** (`crm/models/lead.py`) — Per LinkedIn URL (`linkedin_url` = unique). `public_identifier` (derived from URL). `first_name`, `last_name`, `company_name`. `profile_data` = JSONField (parsed profile dict, nullable). `embedding` = 384-dim float32 BinaryField (nullable). `disqualified` = permanent exclusion. `embedding_array` property for numpy access. `get_labeled_arrays(campaign)` classmethod returns (X, y) for GP warm start. Labels: non-FAILED state → 1, FAILED+DISQUALIFIED → 0, other FAILED → skipped.
- **Deal** (`crm/models/deal.py`) — Per campaign (campaign-scoped via FK). `state` = CharField (ProfileState choices). `closing_reason` = CharField (ClosingReason choices: COMPLETED/FAILED/DISQUALIFIED). `reason` = qualification/failure reason. `connect_attempts` = retry count. `backoff_hours` = check_pending backoff. `creation_date`, `update_date`.
- **Task** (`linkedin/models.py`) — `task_type` (connect/check_pending/follow_up/send_message/check_inbox), `status` (pending/running/completed/failed/cancelled — `cancelled` is set when a task's campaign is deactivated mid-flight), `scheduled_at`, `payload` (JSONField), `error`, `started_at`, `completed_at`. Composite index on `(status, scheduled_at)`. `mark_cancelled()` helper sets status + completed_at. `TaskQuerySet.pending()` annotates a `CASE` priority so `send_message` tasks always sort ahead of other types (secondary sort: `scheduled_at`), making manual `/api/messages/send/` calls run as the next task the daemon picks up — no migration, no preemption of in-flight tasks.
- **ChatMessage** (`chat/models.py`) — GenericForeignKey to any object. `content`, `owner` (nullable, unused after refactor), `answer_to` (self FK), `topic` (self FK), `recipients`, `to`.

## Key Modules

- **`daemon.py`** — Worker loop with active-hours guard (`ENABLE_ACTIVE_HOURS` flag, `seconds_until_active()`), `_build_qualifiers()`, `heal_tasks()`, freemium import, `_FreemiumRotator`. Hot-swap hooks at the top of every loop iteration: `_refresh_account_binding(session, worker_id)` (pool mode only — heartbeats, detects ineligibility, releases, claims next account, swaps the session, relaunches browser); `_refresh_proxy(session)` (compares `account.proxy_url` to `session._launched_with_proxy`, tears down + relaunches if changed); then `_refresh_active_campaign(...)`. `_PlaceholderAccount` is the no-op stand-in used when the pool has no eligible account.
- **`accounts/pool.py`** — `claim_next_account(worker_id)` (atomic `SELECT FOR UPDATE SKIP LOCKED` via `Exists` subquery — Postgres rejects `FOR UPDATE` with `DISTINCT`, so the active-campaign check is an `Exists` annotation rather than a join + `.distinct()`), `release_account`, `heartbeat`, `is_still_eligible`. `STALE_CLAIM_TIMEOUT = 60`. Matches heartbeat cadence to `_IDLE_POLL_INTERVAL = 5`. No cross-worker broker: DB is the only coordination mechanism.
- **`browser/session.py`** — `AccountSession` holds the Playwright stack plus `_launched_with_proxy` (for runtime proxy-change detection) and `_worker_id` (for claim release on shutdown). `swap_account(new_account)` asserts the browser is closed then rebinds; `bind_worker_id(worker_id)` is called once at startup. Module-level `install_shutdown_handler(session)` installs SIGTERM/SIGINT handlers that release the pool claim and close the browser before exiting — critical so `docker stop` doesn't leave a dead claim waiting on `STALE_CLAIM_TIMEOUT`.
- **`browser/login.py`** — `_resolve_proxy_url(account)` returns `account.proxy_url or PROXY_URL or ""`. `launch_browser(storage_state, account)` returns a 5-tuple `(page, context, browser, playwright, proxy_url)` where the last element is the resolved proxy string the context was launched with; `start_browser_session` stamps it onto `session._launched_with_proxy` so `_refresh_proxy` can detect live changes. `_build_proxy_config()` already parses `user:pass@host:port` for Playwright's separate username/password fields.
- **`diagnostics.py`** — `failure_diagnostics()` context manager, `capture_failure()` saves page HTML/screenshot/traceback to `/tmp/openoutreach-diagnostics/`.
- **`tasks/connect.py`** — `handle_connect`, `ConnectStrategy`, `enqueue_connect`/`enqueue_check_pending`/`enqueue_follow_up`.
- **`tasks/check_pending.py`** — `handle_check_pending`, exponential backoff.
- **`tasks/follow_up.py`** — `handle_follow_up`, rate limiting.
- **`tasks/send_message.py`** — `handle_send_message`, `enqueue_send_message()` (creates Task directly, no dedup — each send is unique).
- **`tasks/check_inbox.py`** — `handle_check_inbox`, `enqueue_check_inbox()` (deduped by campaign_id). Polls `fetch_conversations()`, syncs via `sync_conversation()`, fires webhooks.
- **`api_views.py`** — REST API views (plain Django, `@csrf_exempt` + Bearer token via `API_KEY`). Account CRUD: `accounts_collection_view`, `account_detail_view`. Campaign CRUD: `campaigns_collection_view`, `campaign_detail_view`, `campaign_activate_view` (atomic swap with `SELECT FOR UPDATE`), `campaign_deactivate_view`, `campaign_deals_view`, `campaign_stats_view`. Lead/deal reads: `lead_detail_view` (cache only — no Voyager fetch), `lead_deals_view`, `deal_detail_view`. Messages: `send_message_view`, `task_status_view`, `conversation_view`.
- **`mcp_server/`** (top-level, separate package) — Code-mode MCP wrapper over the REST API, runtime-independent of Django.
  - `client.py` — `OpenOutreachClient`, sync httpx wrapper. One method per REST endpoint, raises `OpenOutreachAPIError` on non-2xx. Reads `OPENOUTREACH_BASE_URL`, `OPENOUTREACH_API_KEY`, `OPENOUTREACH_TIMEOUT` env vars; supports a `transport=` kwarg for `httpx.MockTransport` injection in tests.
  - `server.py` — `FastMCP("openoutreach")` instance with twenty `@mcp.tool()` functions: `list_accounts`, `create_account`, `get_account`, `update_account`, `archive_account`, `list_campaigns`, `create_campaign`, `get_campaign`, `update_campaign`, `delete_campaign`, `activate_campaign`, `deactivate_campaign`, `list_campaign_deals`, `get_campaign_stats`, `get_lead`, `list_lead_deals`, `get_deal`, `send_message`, `get_conversation`, `get_task`. Strict-typed args only (IDs, enums, literal field values) — no NL `intent`/`action` fields. Errors are returned as `{"error": {"status_code", "body"}}` instead of raising, so code-mode clients can branch on the envelope.
  - `__main__.py` — `python -m mcp_server` runs stdio by default; `--transport http|sse` switches to Streamable HTTP / SSE.
  - Tests in `tests/mcp/test_client.py` (httpx.MockTransport wire-shape verification) and `tests/mcp/test_tools.py` (tool registration, schema strictness, code-mode rule that no field name is in the forbidden NL set, dispatch through an injected mock client).
- **`webhooks.py`** — `fire_webhook(event_type, data)`. POSTs to `WEBHOOK_URL` with `X-Webhook-Secret` header. 3x retry via tenacity. Logs successful deliveries at INFO, logs at INFO when skipped because `WEBHOOK_URL` is empty (so a silent worker env misconfig is visible in `docker logs`), logs retry exhaustion at WARNING. `local.yml` wires `WEBHOOK_URL` / `WEBHOOK_SECRET` into both the `admin` service and the `&worker-env` anchor — the workers are the ones that execute `send_message` / `check_inbox` handlers and therefore must see these vars, otherwise `fire_webhook()` no-ops and `heal_tasks()` never seeds `check_inbox`.
- **`pipeline/qualify.py`** — `run_qualification()`, `fetch_qualification_candidates()`.
- **`pipeline/search.py`** — `run_search()`, keyword management.
- **`pipeline/search_keywords.py`** — `generate_search_keywords()` via LLM.
- **`pipeline/ready_pool.py`** — GP confidence gate, `promote_to_ready()`.
- **`pipeline/pools.py`** — Composable generators: `search_source` → `qualify_source` → `ready_source`.
- **`pipeline/freemium_pool.py`** — Seed priority + undiscovered pool, ranked by qualifier.
- **`ml/qualifier.py`** — `Qualifier` protocol, `BayesianQualifier`, `KitQualifier`, `qualify_with_llm()`.
- **`ml/embeddings.py`** — FastEmbed utilities, `embed_text()`, `embed_texts()`.
- **`ml/profile_text.py`** — `build_profile_text()`.
- **`ml/hub.py`** — HuggingFace kit loader (`fetch_kit()`).
- **`browser/session.py`** — `AccountSession`: `account` FK (was `linkedin_profile`/`django_user`), page, context, browser, playwright. `campaigns` cached_property (list, filtered by `account=self.account, active=True`); `invalidate_campaigns_cache()` busts it for hot-swaps. `ensure_browser()` launches/recovers browser. `self_profile` cached_property (reads from `self_lead`, discovers via API on first run). Cookie expiry check via `_maybe_refresh_cookies()`.
- **`browser/registry.py`** — `get_or_create_session()`, `get_first_active_account()`, `resolve_account()`, `cli_parser()`/`cli_session()` (shared CLI bootstrap for `__main__` scripts).
- **`browser/login.py`** — `start_browser_session()` — browser launch + LinkedIn login.
- **`browser/nav.py`** — Navigation, auto-discovery, `goto_page()`.
- **`db/leads.py`** — Lead CRUD, `get_leads_for_qualification()`, `disqualify_lead()`.
- **`db/deals.py`** — Deal/state ops, `set_profile_state()`, `increment_connect_attempts()`, `create_freemium_deal()`.
- **`db/chat.py`** — `sync_conversation()` (fetch from Voyager API + upsert to ChatMessage), `_read_from_db()` (read all messages for a lead).
- **`url_utils.py`** — `url_to_public_id()`, `public_id_to_url()` — LinkedIn URL ↔ public identifier conversion. Pure utility, no DB dependency.
- **`conf.py`** — Config constants, `CAMPAIGN_CONFIG`, `get_llm_config()` (reads `LLM_API_KEY` / `AI_MODEL` / `LLM_API_BASE` from environment). Path constants. Timing constants (`MIN_DELAY`, `MAX_DELAY`, `ACTIVE_START_HOUR`, `ACTIVE_END_HOUR`, `ACTIVE_TIMEZONE`, `MIN_ACTION_INTERVAL`) are env-var configurable for per-worker anti-detection jitter.
- **`exceptions.py`** — `AuthenticationError`, `TerminalStateError`, `SkipProfile`, `ReachedConnectionLimit`.
- **`onboarding.py`** — Interactive setup.
- **`agents/follow_up.py`** — Follow-up agent. Single LLM call with structured output (`FollowUpDecision`). Conversation is read in Python and injected into the prompt. No tool-calling loop.
- **`actions/`** — `connect.py` (`send_connection_request`), `status.py` (`get_connection_status`), `message.py` (`send_raw_message`), `profile.py` (profile extraction), `search.py` (LinkedIn search), `conversations.py` (`get_conversation`).
- **`api/client.py`** — `PlaywrightLinkedinAPI`: browser-context fetch (runs JS `fetch()` inside Playwright page for authentic headers). `timeout_ms` constructor param (default 30s). `get_profile()` with tenacity retry.
- **`api/voyager.py`** — `LinkedInProfile` dataclass (url, urn, full_name, headline, positions, educations, country_code, supported_locales, connection_distance/degree). `parse_linkedin_voyager_response()`.
- **`api/newsletter.py`** — `subscribe_to_newsletter()` via Brevo form, `ensure_newsletter_subscription()`. No config parsing — subscribe_newsletter is a BooleanField.
- **`api/messaging/send.py`** — Send messages via Voyager messaging API.
- **`api/messaging/conversations.py`** — Fetch conversations/messages.
- **`api/messaging/utils.py`** — Shared helpers: `encode_urn()`, `check_response()`.
- **`setup/freemium.py`** — `import_freemium_campaign(account, kit_config)` (one freemium campaign per account), `seed_profiles()`.
- **`setup/gdpr.py`** — `apply_gdpr_newsletter_override()`.
- **`setup/self_profile.py`** — `discover_self_profile()` — fetches self profile via Voyager API, sets `account.self_lead`.
- **`setup/seeds.py`** — User-provided seed profiles: parse URLs, create Leads + QUALIFIED Deals.
- **`management/setup_crm.py`** — Idempotent CRM bootstrap (Site creation).
- **`admin.py`** — Django Admin: Campaign, LinkedInAccount, SearchKeyword, ActionLog, Task, ChatMessage. `SiteConfig` is NOT registered — LLM config lives in `.env`.
- **`django_settings.py`** —  PostgreSQL by default (env vars: `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT`); SQLite fallback when `DB_ENGINE=django.db.backends.sqlite3`. Apps: crm, chat, linkedin. Django settings (SQLite at `data/db.sqlite3`). Apps: crm, chat, linkedin.
- **`premigrations/`** — Pre-Django filesystem migrations. Numbered `NNNN_*.py` files with `forward(root_dir)` functions. Runner in `__init__.py` discovers and applies unapplied migrations, tracked via `data/.premigrations` JSON file.

## Configuration

- **LLM config** — `LLM_API_KEY` (required), `AI_MODEL` (required), `LLM_API_BASE` (optional, defaults to OpenAI). Set in `.env`; propagated to `admin` and `worker-pool` services via `local.yml` / `production.yml`. Read at process start by `linkedin.conf.get_llm_config()`. Rotating a key requires restarting the admin and worker containers.
- **`conf.py` schedule** — `ACTIVE_START_HOUR` (9), `ACTIVE_END_HOUR` (17), `ACTIVE_TIMEZONE` ("UTC"), `REST_DAYS` ((5, 6) = Sat+Sun). All overridable via env vars for per-worker timing isolation. Daemon sleeps outside this window.
- **`conf.py:CAMPAIGN_CONFIG`** — `min_ready_to_connect_prob` (0.9), `min_positive_pool_prob` (0.20), `connect_delay_seconds` (10), `connect_no_candidate_delay_seconds` (300), `check_pending_recheck_after_hours` (24), `check_pending_jitter_factor` (0.2), `qualification_n_mc_samples` (100), `enrich_min_interval` (1), `min_action_interval` (120), `embedding_model` ("BAAI/bge-small-en-v1.5").
- **`conf.py` REST API & webhooks** — `API_KEY` (Bearer token for API auth), `WEBHOOK_URL` (POST target for incoming message notifications), `WEBHOOK_SECRET` (sent as `X-Webhook-Secret` header), `CHECK_INBOX_INTERVAL_SECONDS` (default 300). All from env vars.
- **Prompt templates** (at `linkedin/templates/prompts/`) — `qualify_lead.j2` (temp 0.7), `search_keywords.j2` (temp 0.9), `follow_up_agent.j2`.
- **`requirements/`** — `base.txt`, `local.txt`, `production.txt`, `crm.txt` (empty — DjangoCRM installed via `--no-deps`).

## Docker

Base image: `mcr.microsoft.com/playwright/python:v1.55.0-noble`. `BUILD_ENV` arg selects requirements. Dockerfile at `compose/linkedin/Dockerfile`. Install: uv pip → DjangoCRM `--no-deps` → requirements → Playwright chromium.

### Multi-Account Setup (`local.yml`)

Runs N LinkedIn accounts in parallel, each in its own container with full process isolation:

- **`postgres`** — PostgreSQL 16, shared database for all services.
- **`admin`** — Django Admin + REST API web server (port 8000). `RUN_MODE=admin`.
- **`worker-N`** — One daemon per account, hard-coded in `local.yml`. Each gets its own Xvfb display, browser, VNC. `LINKEDIN_PROFILE` env var selects the `LinkedInAccount.username`. Container management is static — to add/remove accounts, edit `local.yml`.

Container entrypoint (`compose/linkedin/start`) dispatches based on `RUN_MODE`: `admin` runs `runserver`, `setup` runs interactive login, `browse` runs browser-only (no daemon), otherwise starts Xvfb/VNC + daemon with `--profile $LINKEDIN_PROFILE`.

Each daemon's task queue is scoped to its own account's active campaign — workers only claim tasks whose `payload.campaign_id` matches a campaign owned by their account.

## CI/CD

- `tests.yml` — pytest in Docker on push to `master` and PRs.
- `deploy.yml` — Tests → build + push to `ghcr.io/eracle/openoutreach`. Tags: `latest`, `sha-<commit>`, semver.

## Dependencies

`requirements/` files. DjangoCRM's `mysqlclient` excluded via `--no-deps`. `uv pip install` for fast installs.

Core: `playwright`, `playwright-stealth`, `Django`, `django-crm-admin`, `pandas`, `langchain`/`langchain-openai`, `jinja2`, `pydantic`, `jsonpath-ng`, `tendo`, `termcolor`, `tenacity`, `requests`
ML: `scikit-learn`, `numpy`, `fastembed`, `joblib`
