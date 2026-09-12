FROM caddy:2.11.4-builder@sha256:403d237d0bb16d2e62b1f93ca9ebb4953ecbb78aee5986765713a39f9263a5b4 AS builder

RUN xcaddy build \
    --with github.com/caddy-dns/cloudflare@a8737d095ad5a48ca031cea6ab704057dbc2d250

FROM caddy:2.11.4@sha256:13ba145cba2f3e28fa801994876e4c086d1b95d5aa2a520a734765ffb6b12017

RUN addgroup -S -g 10001 app \
    && adduser -S -D -H -u 10001 -G app app \
    && mkdir -p /data /config \
    && chown -R 10001:10001 /data /config /etc/caddy

COPY --from=builder --chown=10001:10001 /usr/bin/caddy /usr/bin/caddy
COPY --chown=10001:10001 Caddyfile /etc/caddy/Caddyfile

USER 10001:10001
