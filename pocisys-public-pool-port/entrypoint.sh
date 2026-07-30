#!/bin/sh
set -eu

# Docker creates a missing bind-mount directory as root. Limit the ownership
# repair to this app's own dashboard data, then run the service unprivileged.
mkdir -p "${DATA_DIR:-/data}"
chown -R 1000:1000 "${DATA_DIR:-/data}"
exec su-exec 1000:1000 "$@"
