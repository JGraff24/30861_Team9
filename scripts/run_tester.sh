#!/bin/sh
set -eu
exec uv run --locked uvicorn tester.main:create_app --factory --host 127.0.0.1 --port 8001 --no-access-log
