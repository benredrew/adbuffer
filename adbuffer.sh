#!/usr/bin/env bash
# Convenience launcher: ./adbuffer.sh <url-or-file> [options]
exec "$(dirname "$(readlink -f "$0")")/.venv/bin/python" -m adbuffer "$@"
