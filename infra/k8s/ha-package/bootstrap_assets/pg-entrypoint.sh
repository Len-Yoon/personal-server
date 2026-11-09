#!/bin/bash
# No provider access, writer activation, credential printing, or restart loop.
set -euo pipefail
umask 077
case "${HA_PG_NODE:-}" in A|B) ;; *) exit 64 ;; esac
# Legacy schema1 keeps its original A-primary/B-from-A behavior.
HA_PG_PRIMARY_NODE=${HA_PG_PRIMARY_NODE:-A}
HA_PG_SOURCE_NODE=${HA_PG_SOURCE_NODE:-$([ "$HA_PG_NODE" = A ] && printf none || printf A)}
case "$HA_PG_PRIMARY_NODE" in A|B) ;; *) exit 64 ;; esac
if [ "$HA_PG_NODE" = "$HA_PG_PRIMARY_NODE" ]; then
  test "$HA_PG_SOURCE_NODE" = none || exit 64
else
  test "$HA_PG_SOURCE_NODE" = "$HA_PG_PRIMARY_NODE" || exit 64
fi
case "${HA_PG_UID:-}:${HA_PG_GID:-}" in 70:70|999:999) ;; *) exit 64 ;; esac
test "$(id -u):$(id -g)" = "$HA_PG_UID:$HA_PG_GID" || exit 65
test "${PGDATA:-}" = /var/lib/postgresql/data && test -d "$PGDATA" && test ! -L "$PGDATA" || exit 65
test "$(stat -c '%u' "$PGDATA")" = "$HA_PG_UID" || exit 65
# Empty Docker volumes may copy the image's 1777 directory mode before cloning.
chmod 0700 "$PGDATA"
for name in server.crt server.key ca.crt postgres-password repl.pgpass; do
  test -f "/run/ha-pg/$name" && test ! -L "/run/ha-pg/$name" || exit 65
done
# Secrets are mounted read-only, owned by postgres, inaccessible to applications.
test "$(stat -c '%u:%a' /run/ha-pg/server.key)" = "$HA_PG_UID:600" || exit 65
test "$(stat -c '%u:%a' /run/ha-pg/repl.pgpass)" = "$HA_PG_UID:600" || exit 65
if [ "$HA_PG_SOURCE_NODE" != none ] && [ ! -e "$PGDATA/PG_VERSION" ]; then
  test -z "$(find "$PGDATA" -mindepth 1 -maxdepth 1 -print -quit)" || exit 66
  # Both directions are fixed enum values; no caller-supplied DSN is accepted.
  source_host=ha-$(printf '%s' "$HA_PG_SOURCE_NODE" | tr AB ab)
  application_name=ha_$(printf '%s' "$HA_PG_NODE" | tr AB ab)
  pg_basebackup --dbname="host=$source_host port=5432 user=ha_repl sslmode=verify-full sslrootcert=/run/ha-pg/ca.crt passfile=/run/ha-pg/repl.pgpass application_name=$application_name" --pgdata="$PGDATA" --write-recovery-conf --no-slot --wal-method=stream --checkpoint=fast
  exec postgres -D "$PGDATA" -c config_file=/bootstrap/postgresql.conf
fi
if [ ! -e "$PGDATA/PG_VERSION" ]; then
  test -z "$(find "$PGDATA" -mindepth 1 -maxdepth 1 -print -quit)" || exit 66
  initdb -D "$PGDATA" --data-checksums --auth-host=scram-sha-256 --auth-local=peer --pwfile=/run/ha-pg/postgres-password >/dev/null
fi
exec postgres -D "$PGDATA" -c config_file=/bootstrap/postgresql.conf
