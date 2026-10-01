#!/usr/bin/env bash
# 一条本地命令启动平台
set -e
cd "$(dirname "$0")"
DB="${ESC_DB:-data/escalator.db}"
HOST="${ESC_HOST:-127.0.0.1}"
PORT="${ESC_PORT:-8000}"
exec python3 -m escalator --host "$HOST" --port "$PORT" --db "$DB"
