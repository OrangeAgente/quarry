#!/bin/sh
# Runs as root, ensures the bind-mounted data dir is owned by app, drops privileges.
set -e

mkdir -p /app/data
chown -R app:app /app/data 2>/dev/null || true

exec gosu app "$@"
