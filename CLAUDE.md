# CLAUDE.md

## Rules

- **Python env**: Always use `.venv/bin/python` (not system `python3`).
- **Commits**: No `Co-Authored-By` lines. Single-line messages (no body).
- **Dependencies**: Managed in `requirements/*.txt` (used by local dev and Docker).
- **Docs sync**: When modifying code, update CLAUDE.md and ARCHITECTURE.md to reflect changes.
- **No memory**: Never use the auto-memory system (no MEMORY.md, no memory files). All persistent context belongs in CLAUDE.md or ARCHITECTURE.md.
- **Error handling**: App should crash on unexpected errors. `try/except` only for expected, recoverable errors. Custom exceptions in `exceptions.py`.
- **No backward compat**: CRM models are owned by this project — no need for backward compatibility shims, legacy migration code, or re-export modules. Simplify freely.

## Project Overview

OpenOutreach — self-hosted LinkedIn automation for B2B lead generation. Playwright + stealth for browser automation, LinkedIn Voyager API for profile data, Django + Django Admin for CRM (models owned by this project).

## Commands

```bash
# Docker
make build / make up / make stop / make logs / make up-view

# Local dev
make setup    # install deps + browsers + migrate + bootstrap CRM
make run      # run daemon
make admin    # Django Admin at localhost:8000/admin/

# Testing
make test / make docker-test
pytest tests/api/test_voyager.py   # single file
pytest -k test_name                # single test
```

## Architecture (quick reference)

For detailed module docs, see `ARCHITECTURE.md`.

- **Entry**: `manage.py` — stock Django management. `rundaemon` command (premigrations → migrate → onboard → validate → task queue loop); `rundaemon --profile <username>` runs for a specific account. `manage.py` with no args defaults to `rundaemon`. `setup_account <username>` for interactive VNC login; `browse_account <username>` for browser-only VNC mode. Onboarding logic in `onboarding.py`: `OnboardConfig` (pure dataclass, `from_json()` classmethod), `missing_keys()`, `collect_from_wizard()`, single `apply()` write path. Docker `start` script dispatches on `RUN_MODE` env var: `admin` (web server), `setup` (VNC login), `browse` (browser only), default (daemon).
- **Premigrations**: `linkedin/premigrations/` — numbered Python files for pre-Django filesystem changes (run before `migrate`). Tracked via `data/.premigrations` JSON file. Add new migrations as `NNNN_description.py` with a `forward(root_dir)` function.
- **State machine**: `enums.py:ProfileState` — QUALIFIED → READY_TO_CONNECT → PENDING → CONNECTED → COMPLETED / FAILED. Deal.state is a CharField with ProfileState choices (no Stage model). `ClosingReason` (COMPLETED/FAILED/DISQUALIFIED) on Deal.closing_reason. `Lead.disqualified=True` = permanent exclusion. LLM rejections = FAILED Deals with DISQUALIFIED closing reason (campaign-scoped).
- **Task queue**: `Task` model (persistent). Three types: `connect`, `check_pending`, `follow_up`. Handlers in `linkedin/tasks/`, signature: `handle_*(task, session, qualifiers)`. On 401 (`AuthenticationError`), daemon calls `session.reauthenticate()` and resets the task to pending.
- **ML pipeline**: GPR (sklearn) + BALD active learning + LLM qualification. Per-campaign models stored in `Campaign.model_blob` (DB).
- **Config**: `SiteConfig` DB singleton (LLM_API_KEY, AI_MODEL, LLM_API_BASE — editable via Django Admin), `conf.py:CAMPAIGN_CONFIG` (timing/ML defaults), `conf.py` browser constants (`BROWSER_*`, `HUMAN_TYPE_*`), `conf.py` schedule constants (`ENABLE_ACTIVE_HOURS` flag, active hours/timezone/rest days), `conf.py` onboarding defaults (`DEFAULT_*_LIMIT`), `conf.py:FASTEMBED_CACHE_DIR` (persistent model cache, defaults to `<project>/.cache/fastembed/`), Campaign/LinkedInProfile models (Django Admin). `VOYAGER_REQUEST_TIMEOUT_MS` lives in `api/client.py` (constructor default on `PlaywrightLinkedinAPI`). `conf.py:DUMP_PAGES` (default `False`) — enable to save page HTML snapshots for fixture collection. Per-worker timing env vars: `MIN_DELAY`, `MAX_DELAY`, `ACTIVE_START_HOUR`, `ACTIVE_END_HOUR`, `ACTIVE_TIMEZONE`, `MIN_ACTION_INTERVAL`, `DEFAULT_SPREAD_WINDOW_HOURS`.
- **Lazy accessors**: `Lead.get_profile(session)`, `Lead.get_urn(session)`, `Lead.get_embedding(session)` — fetch from API and cache in DB on first access. Chained: `get_embedding` → `get_profile` → Voyager API. `Lead.to_profile_dict()` reads existing data only. `AccountSession.campaigns` (cached_property, list). `AccountSession.self_profile` (cached_property, reads from `LinkedInProfile.self_lead`, discovers via API on first run).
- **Django apps**: `linkedin` (main — Campaign with users M2M), `crm` (Lead with embedding/Deal), `chat` (ChatMessage).
- **Data dir**: `data/` holds persistent state (`db.sqlite3`, `.premigrations`). Docker users mount volumes at `/app/data`.
- **Database**: PostgreSQL by default (env vars: `POSTGRES_*`); SQLite fallback with `DB_ENGINE=django.db.backends.sqlite3`.
- **Docker**: Playwright base image, `BUILD_ENV` arg selects requirements. Multi-account: `local.yml` runs postgres + admin + N worker containers. Each worker gets `LINKEDIN_PROFILE` env var, own VNC port. `RUN_MODE`: `admin` (web server), `browse` (browser + VNC, no daemon), `setup` (interactive login), default (daemon).
- **CI/CD**: `.github/workflows/tests.yml` (pytest), `deploy.yml` (build + push to ghcr.io).
