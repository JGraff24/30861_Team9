#!/bin/sh
set -eu
exec uv run --locked python -m app.jobs.worker "$@"
