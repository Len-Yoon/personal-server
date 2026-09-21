FROM caddy:2.11.4-builder@sha256:401121e61853cb9c7df83cba43e532e49a68e30f32921c7bdcb7a4912168c067 AS builder

RUN xcaddy build \
    --with github.com/caddy-dns/cloudflare@a8737d095ad5a48ca031cea6ab704057dbc2d250

FROM caddy:2.11.4@sha256:14a9c00d4e833ebc2b65d36515b37bde3b73f0b323a2663aaafc88953d8c4e3f

RUN addgroup -S -g 10001 app \
    && adduser -S -D -H -u 10001 -G app app \
    && mkdir -p /data /config \
    && chown -R 10001:10001 /data /config /etc/caddy

COPY --from=builder --chown=10001:10001 /usr/bin/caddy /usr/bin/caddy
COPY --chown=10001:10001 Caddyfile /etc/caddy/Caddyfile

USER 10001:10001
