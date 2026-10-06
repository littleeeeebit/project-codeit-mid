#!/bin/sh
# Linux counterpart of start-postgresql.ps1: the pinned server on loopback port 55432. RFP_DATABASE_DSN is left alone.
set -eu
repo=$(cd "$(dirname "$0")/.." && pwd)
secrets="$repo/.runtime/postgresql.env"
if [ ! -f "$secrets" ]; then
    mkdir -p "$(dirname "$secrets")"
    (umask 077 && printf 'BIDMATE_POSTGRES_PASSWORD=%s\n' "$(od -An -tx1 -N32 /dev/urandom | tr -d ' \n')" > "$secrets")
fi
docker compose --project-directory "$repo" --env-file "$secrets" -f "$repo/compose.postgresql.yaml" up -d --wait \
    || { echo 'PostgreSQL startup failed; check Docker and port 55432.' >&2; exit 1; }
echo 'PostgreSQL is ready on port 55432. No application database was selected; RFP_DATABASE_DSN is unchanged.'
