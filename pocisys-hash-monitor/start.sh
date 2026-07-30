#!/bin/sh
set -eu

export PYTHONPATH="/app"
exec python /app/main.py
