#!/bin/sh
set -eu

# Docker named volumes can retain root ownership even though the application
# runs as an unprivileged user. Repair only the dedicated companion volume.
mkdir -p /app/companion-data
chown -R appuser:appuser /app/companion-data

exec gosu appuser "$@"
