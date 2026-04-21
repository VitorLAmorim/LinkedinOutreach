# compose/caddy/Dockerfile — Caddy with the rate_limit plugin built in.
#
# Stock `caddy:2-alpine` does not ship the `rate_limit` directive. xcaddy
# compiles a custom Caddy binary with the community plugin, then we copy
# it into the final runtime image. Keep the two-stage build so the runtime
# image stays small (~40 MB) without the Go toolchain.
#
# Plugin source: https://github.com/mholt/caddy-ratelimit

FROM caddy:2-builder-alpine AS builder

RUN xcaddy build \
    --with github.com/mholt/caddy-ratelimit

FROM caddy:2-alpine

COPY --from=builder /usr/bin/caddy /usr/bin/caddy
