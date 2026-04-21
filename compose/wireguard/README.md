# WireGuard operator VPN

The `wireguard` service in `production.yml` is the **only** path by which
the operator can reach the Django admin panel, worker VNC/noVNC
endpoints, and the Postgres shell. Nothing in those paths is exposed to the
public internet.

We use `lscr.io/linuxserver/wireguard` because it auto-provisions keys and
peer configs on first run — no manual `wg genkey` gymnastics.

## How it works at boot

1. The container binds `51820/udp` on the host (the only port that needs to
   pass the Hetzner Cloud Firewall for operator traffic).
2. It attaches to the `internal` Docker network, where it can resolve
   `admin`, `postgres`, and `worker-pool` via Docker's built-in DNS.
3. On first run it generates a server key pair and `$WG_PEERS` peer
   configs in the named `wireguard-config` volume.
4. It routes the peer traffic through the tunnel and applies
   `ALLOWEDIPS=10.13.13.0/24,172.28.0.0/16` so peers can reach both the WG
   subnet AND the internal Docker network.

## Grabbing a peer config for your laptop

After the first `docker compose -f production.yml up -d`:

```bash
# Print the peer config (copy-paste into WireGuard client)
docker compose -f production.yml exec wireguard show peer1

# OR show a QR code for the mobile app
docker compose -f production.yml exec wireguard show peer1 | qrencode -t ansiutf8
```

On the laptop, install the WireGuard client (`wireguard-tools` on Linux,
the app store on macOS/Windows/iOS/Android), paste or scan the peer config,
and activate the tunnel.

## Verifying the tunnel works

With the tunnel active, open a browser and point it at:

- `http://admin:8000/admin/login/` → Django admin login loads
- `http://worker-pool:6080/vnc.html` → noVNC connects to a pool replica

`admin` and `worker-pool` are Docker-internal DNS names — they resolve via
`PEERDNS=127.0.0.11` (Docker's embedded DNS server) over the tunnel.

A quick CLI smoke test:

```bash
# From the laptop with WG active
curl -v http://admin:8000/admin/login/   # expect 200 + Django HTML
psql -h postgres -U openoutreach         # DB shell (needs POSTGRES_PASSWORD)
```

## Adding more operator peers later

1. Bump `WG_PEERS` in `.env` (e.g. `WG_PEERS=3`).
2. `docker compose -f production.yml up -d wireguard` — the container
   generates new peer configs on restart without touching the existing ones.
3. `docker compose -f production.yml exec wireguard show peer3` — grab the
   new config.

## Retiring peers

Stop the container, delete the relevant `peer<N>` directory inside the
`wireguard-config` volume, restart. There is no built-in revoke command in
the `linuxserver/wireguard` image; manual volume cleanup is the documented
path.

## Why not Tailscale?

Tailscale is simpler to operate (SSO login, magic DNS, no config files),
but it (a) adds a third-party control plane to the trust path and (b)
requires per-machine daemons. WireGuard with self-hosted keys keeps the
trust boundary at the Hetzner VM. If operator count grows past ~5 and the
config-distribution overhead starts to hurt, revisit.
