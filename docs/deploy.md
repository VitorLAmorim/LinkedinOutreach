# Production deployment — Hetzner Cloud

This runbook deploys OpenOutreach to a single Hetzner Cloud VM using
`production.yml`. Only Caddy (`80/tcp`, `443/tcp`) and WireGuard
(`51820/udp`) are ever reachable from the internet; the Django admin,
Postgres, and worker VNC endpoints are either on the internal Docker
network or the WireGuard tunnel only.

See `/home/vitor/.claude/plans/hashed-shimmying-rabbit.md` for the full
architecture rationale — this document is the imperative version.

## 0. Prerequisites on your workstation

- `hcloud` CLI: `brew install hcloud` or [download from GitHub](https://github.com/hetznercloud/cli).
- `hcloud context create <name>` and paste a Hetzner API token scoped
  read+write for your project.
- A domain name you control. It does **not** need to be hosted at Hetzner
  DNS — any provider that supports A records works.
- `wireguard-tools` (Linux/macOS) or the WireGuard app (Windows/iOS/Android)
  for the operator tunnel.

## 1. Create the Hetzner server

```bash
# Pick a starting size. CCX13 is enough for admin + postgres + 1 worker;
# bump to CCX23 once you're running 2–4 pool replicas.
hcloud server create \
  --name openoutreach-prod \
  --image docker-ce \
  --type ccx13 \
  --location nbg1 \
  --ssh-key "your-ssh-key-name" \
  --label env=production \
  --label role=openoutreach
```

`--image docker-ce` selects the Hetzner Apps image that ships Ubuntu with
Docker Engine + Compose v2 pre-installed, so the VM is usable on first
boot with no manual Docker setup.

Note the server's public IPv4 from the output — you'll need it for DNS.

## 2. Attach a Volume for Postgres data

```bash
hcloud volume create \
  --name openoutreach-pgdata \
  --size 10 \
  --server openoutreach-prod \
  --format ext4 \
  --automount
```

SSH into the server and confirm the volume is mounted:

```bash
ssh root@<server-ip>
df -h /mnt/HC_Volume_*
# Bind-mount the Hetzner volume at the path production.yml expects:
mkdir -p /mnt/postgres-data
mount --bind /mnt/HC_Volume_*/. /mnt/postgres-data
# Make it persistent across reboots:
echo "/mnt/HC_Volume_XXXXXXXX /mnt/postgres-data none bind 0 0" >> /etc/fstab
```

Replace `HC_Volume_XXXXXXXX` with the actual directory name from `df -h`.

## 3. Create and attach the Cloud Firewall

```bash
hcloud firewall create \
  --name openoutreach-prod \
  --label env=production

FW_ID=$(hcloud firewall list -o noheader -o columns=id,name | grep openoutreach-prod | awk '{print $1}')

# SSH (restrict to your operator IP in a real deploy)
hcloud firewall add-rule $FW_ID \
  --direction in --protocol tcp --port 22 \
  --source-ips 0.0.0.0/0 --source-ips ::/0 \
  --description "SSH"

# Caddy HTTP (ACME challenge + redirect to 443)
hcloud firewall add-rule $FW_ID \
  --direction in --protocol tcp --port 80 \
  --source-ips 0.0.0.0/0 --source-ips ::/0 \
  --description "Caddy ACME + HTTP→HTTPS"

# Caddy HTTPS
hcloud firewall add-rule $FW_ID \
  --direction in --protocol tcp --port 443 \
  --source-ips 0.0.0.0/0 --source-ips ::/0 \
  --description "Caddy HTTPS"

# WireGuard (stateful — return traffic auto-allowed)
hcloud firewall add-rule $FW_ID \
  --direction in --protocol udp --port 51820 \
  --source-ips 0.0.0.0/0 --source-ips ::/0 \
  --description "WireGuard operator VPN"

hcloud firewall apply-to-resource $FW_ID --type server --server openoutreach-prod
```

No other inbound rules. Outbound defaults to allow-all (which is what we
need for ACME, LinkedIn, residential proxies, LLM APIs, Docker Hub).

## 4. Create the DNS record

At your DNS provider, create an A record pointing at the server's IPv4.
For example `openoutreach.example.com` → `1.2.3.4`.

Verify propagation from your workstation:

```bash
dig +short openoutreach.example.com
```

Caddy's ACME HTTP-01 challenge will fail if DNS is wrong, so get this right
before moving on.

## 5. Clone the repo on the server

```bash
ssh root@<server-ip>
cd /opt
git clone https://github.com/eracle/OpenOutreach.git openoutreach
cd openoutreach
```

## 6. Bootstrap `.env`

```bash
cp .env.copy .env
chmod 600 .env

# Generate every secret. Paste each output into the corresponding var in .env.
openssl rand -hex 32   # → API_KEY
openssl rand -hex 32   # → WEBHOOK_SECRET (only if WEBHOOK_URL is set)
openssl rand -hex 32   # → SECRET_KEY
openssl rand -hex 32   # → POSTGRES_PASSWORD

nano .env
```

Fill in at minimum:

- `API_KEY` — random 32-byte hex
- `SECRET_KEY` — random 32-byte hex (MUST differ from the dev default)
- `DJANGO_ALLOWED_HOSTS=openoutreach.example.com`
- `DJANGO_CSRF_TRUSTED_ORIGINS=https://openoutreach.example.com`
- `POSTGRES_PASSWORD` — random 32-byte hex
- `CADDY_HOSTNAME=openoutreach.example.com`
- `CADDY_ACME_EMAIL=you@example.com`
- `CADDY_API_ALLOWLIST=<crm-ip>/32 <mcp-ip>/32` — whitespace-separated
- `WG_SERVER_URL=openoutreach.example.com`
- `WG_PEERS=1`

Leave `WEBHOOK_URL` / `WEBHOOK_SECRET` blank for now unless you have a
receiver ready.

## 7. First boot

```bash
docker compose -f production.yml up -d
docker compose -f production.yml logs -f caddy
```

Watch Caddy's log for the ACME provisioning message:

```
certificate obtained successfully for openoutreach.example.com
```

If ACME fails, the usual culprits are DNS not resolving, port 80 not open,
or `CADDY_HOSTNAME` not matching the DNS record. Fix and restart caddy:
`docker compose -f production.yml restart caddy`.

## 8. Enroll your WireGuard peer

```bash
# Print peer config (copy-paste into the WireGuard client)
docker compose -f production.yml exec wireguard show peer1

# OR render a QR code for the mobile app
docker compose -f production.yml exec wireguard show peer1 | qrencode -t ansiutf8
```

Import on your laptop, activate the tunnel, and confirm you can reach
`http://admin:8000/admin/login/` over the tunnel. See
`compose/wireguard/README.md` for more detail.

## 9. Create a Django superuser

With the WG tunnel active, from your laptop or from an SSH session on the
host:

```bash
docker compose -f production.yml exec admin \
  .venv/bin/python manage.py createsuperuser
```

Then log in at `http://admin:8000/admin/` via the tunnel and create your
first `LinkedInAccount` and `Campaign` rows.

## 10. Scale workers

```bash
docker compose -f production.yml up -d --scale worker-pool=3
```

Worker replicas claim eligible accounts from the pool automatically (see
CLAUDE.md → worker pool section).

## 11. Verification checklist

**From an untrusted network (phone on mobile data, NOT on the WG tunnel):**

1. `curl -v https://openoutreach.example.com/admin/login/` → expect `404`
   from Caddy, no Django login HTML. ✅
2. `curl -v https://openoutreach.example.com/` → expect `404` from Caddy. ✅
3. `curl -v https://openoutreach.example.com/api/accounts/` → expect `403`
   from Caddy (IP not allowlisted). NOT `401` or `503`. ✅
4. `nmap -p 5432,5440,8000,5900,6080 openoutreach.example.com` → every
   port `filtered` or `closed`. ✅
5. On the host: `docker compose -f production.yml ps` — the "Ports"
   column is empty for `admin`, `postgres`, and `worker-pool`. ✅

**From an allowlisted CRM IP:**

6. `curl -H "Authorization: Bearer $API_KEY" https://openoutreach.example.com/api/accounts/`
   → `200 OK`. ✅
7. `curl -H "Authorization: Bearer wrong" https://openoutreach.example.com/api/accounts/`
   → `401` from Django. ✅

**From your laptop with WG connected:**

8. Browser → `http://admin:8000/admin/login/` → Django login form. ✅
9. Browser → `http://worker-pool:6080/vnc.html` → noVNC. ✅
10. `psql -h postgres -U openoutreach` → DB shell. ✅

If 1–5 fail, the deployment has a leak. If 6–10 fail, the happy path is
broken but nothing is leaking. Do not declare the deploy done until all
ten pass.

## 12. Day-two operations

### Adding a CRM allowlist entry

1. Edit `.env` → update `CADDY_API_ALLOWLIST` with the new CIDR
   (whitespace-separated).
2. `docker compose -f production.yml up -d caddy` — Caddy picks up the
   new env var on restart. Alternatively, for a zero-downtime reload:
   `docker compose -f production.yml exec caddy caddy reload --config /etc/caddy/Caddyfile`.
3. Re-run checklist item 6 from the new IP.

### Rotating the API key

1. `openssl rand -hex 32` → paste into `.env` as the new `API_KEY`.
2. `docker compose -f production.yml up -d admin` — Django picks up the
   new value on restart.
3. Update every consumer (CRM, MCP host) with the new bearer token.
4. Old token is now invalid.

### Upgrading the deployment

```bash
ssh root@<server-ip>
cd /opt/openoutreach
git pull
docker compose -f production.yml build
docker compose -f production.yml up -d
```

Postgres data lives on the Hetzner Volume and survives the rebuild.

### Rotating the server

1. `hcloud server create --name openoutreach-prod-new ...` (new VM).
2. `hcloud volume detach openoutreach-pgdata`.
3. `hcloud volume attach openoutreach-pgdata --server openoutreach-prod-new`.
4. Re-apply firewall, re-clone repo, re-copy `.env`, `docker compose up -d`.
5. Update DNS A record to the new IP (ACME will re-issue on first request).
6. Delete the old server.

No `pg_dump`, no data migration — the volume carries the database.

## 13. What this runbook does NOT cover

- `django-axes`, `django-otp`, renamed admin URL → not needed while `/admin/`
  is WG-only. Revisit if/when the admin panel becomes internet-reachable.
- Gunicorn in front of Django → future phase (the dev runserver is
  currently the entrypoint and works fine behind Caddy for a single-tenant
  deploy).
- CI/CD auto-deploy → future phase. For now, `git pull && compose up -d`
  on the host is deliberate.
- Backups beyond Hetzner Volume snapshots → future phase. For now, enable
  weekly automatic snapshots on the volume in the Hetzner console.
- Observability (Prometheus, Grafana, Loki) → future phase.

Each of these gets its own plan when the need is concrete.
