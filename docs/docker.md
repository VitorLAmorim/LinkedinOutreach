# Docker Installation and Usage

## Quick Start (Pre-built Image — Recommended)

Pre-built production images are published to GitHub Container Registry on every push to `master`.

```bash
docker run --pull always -it -p 5900:5900 -v openoutreach_db:/app/data ghcr.io/eracle/openoutreach:latest
```

The interactive onboarding will guide you through LinkedIn credentials, LLM API key, and campaign setup on first run. All data (CRM database, cookies, model blobs, embeddings) persists in the `openoutreach_db` Docker volume.

### Available Tags

| Tag | Description |
|:----|:------------|
| `latest` | Latest build from `master` |
| `sha-<commit>` | Pinned to a specific commit |
| `1.0.0` / `1.0` | Semantic version (when tagged) |

### VNC (Live Browser View)

The container includes a VNC server for watching the automation live. Connect any VNC client to `localhost:5900` (no password).

On Linux with `vinagre`:
```bash
vinagre vnc://127.0.0.1:5900
```

### Stopping & Restarting

```bash
# Find the container
docker ps

# Stop it
docker stop <container-id>

# Restart (data persists in the openoutreach_db volume)
docker run --pull always -it -p 5900:5900 -v openoutreach_db:/app/data ghcr.io/eracle/openoutreach:latest
```

---

## Build from Source (Docker Compose)

For development or customization, you can build the image locally. The compose file (`local.yml`)
mounts the entire project directory into the container for live code editing.

### Prerequisites

- [Make](https://www.gnu.org/software/make/)
- [Docker](https://www.docker.com/)
- [Docker Compose](https://docs.docker.com/compose/)

### Build & Run

```bash
git clone https://github.com/eracle/OpenOutreach.git
cd OpenOutreach

# Build and start
make up
```

This builds the Docker image from source with `BUILD_ENV=local` (includes test dependencies) and starts the daemon.

**Note:** The compose file uses `HOST_UID` / `HOST_GID` environment variables (defaulting to 1000)
for file ownership. If your host UID differs from 1000, set them explicitly:

```bash
HOST_UID=$(id -u) HOST_GID=$(id -g) make up
```

### Useful Commands

| Command | Description |
|:--------|:------------|
| `make build` | Build the Docker image without starting |
| `make up` | Build and start all services (postgres + admin + 4 workers) |
| `make stop` | Stop the running containers |
| `make logs` | Follow application logs |
| `make up-view` | Start + open VNC viewer (Linux, requires `vinagre`) |
| `make view-1` … `make view-4` | Open VNC viewer for a specific worker |
| `make docker-test` | Run the test suite in Docker |

### VNC with Docker Compose

Each worker exposes its own VNC port: worker-1 on 5901, worker-2 on 5902, etc. noVNC web access on 6081-6084. Use `make view-1` through `make view-4` to open them.

### Volume Mounts

The pre-built `docker run` command uses a named Docker volume (`openoutreach_db`) mounted at `/app/data` for data persistence (database, config). The compose setup (`local.yml`) mounts the entire repo `.:/app` for live code editing during development. PostgreSQL data persists in the `pgdata` named volume.

---

## Multi-Account Setup (4 Campaigns, 4 Accounts)

The `local.yml` compose file runs 4 LinkedIn accounts in parallel, each in its own isolated container with a separate browser process. All share a single PostgreSQL database.

### Architecture

| Service | Purpose | Ports |
|:--------|:--------|:------|
| `postgres` | Shared PostgreSQL database | 5432 |
| `admin` | Django Admin web server | 8000 |
| `worker-1` | Daemon for account 1 | VNC 5901, noVNC 6081 |
| `worker-2` | Daemon for account 2 | VNC 5902, noVNC 6082 |
| `worker-3` | Daemon for account 3 | VNC 5903, noVNC 6083 |
| `worker-4` | Daemon for account 4 | VNC 5904, noVNC 6084 |

### Setup Steps

1. **Start the stack:**
   ```bash
   make up
   ```

2. **Create accounts via Django Admin** at `http://localhost:8000/admin/`:
   - Create 4 Django users (usernames must match `LINKEDIN_PROFILE` env vars in `local.yml`)
   - Create 4 LinkedInProfile records (one per user, all `active=True`)
   - Create 4 Campaign records, each with its corresponding user in the M2M

3. **Update `local.yml`** — set `LINKEDIN_PROFILE` for each worker to the Django username of its account:
   ```yaml
   worker-1:
     environment:
       LINKEDIN_PROFILE: john_doe
   worker-2:
     environment:
       LINKEDIN_PROFILE: jane_smith
   # ...
   ```

4. **Restart workers:**
   ```bash
   make stop && make up
   ```

### Data Migration from SQLite

If you have existing data in `db.sqlite3`:

```bash
# Dump from SQLite
DB_ENGINE=django.db.backends.sqlite3 python manage.py dumpdata --natural-foreign --natural-primary -o dump.json

# Start postgres
docker compose -f local.yml up -d postgres
sleep 3

# Load into PostgreSQL
python manage.py migrate --no-input
python manage.py loaddata dump.json
```

### How Isolation Works

- **Process isolation**: Each worker is a separate container (PID, memory, network)
- **Browser isolation**: Each worker has its own Xvfb display and Chromium instance
- **Task queue isolation**: Each daemon only claims tasks for its own campaigns
- **Rate limits**: Per-account (`LinkedInProfile.connect_daily_limit`, etc.)
- **Shared data**: Leads are global (deduplicated by LinkedIn URL); Deals are campaign-scoped
