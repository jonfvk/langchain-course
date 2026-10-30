#!/bin/sh
set -eu

# An inherited URL override must never supersede sandbox selection.
unset TASTYTRADE_API_URL
export TASTYTRADE_ENV=sandbox
export TASTYTRADE_READ_ONLY=1

SCRIPT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)
PROJECT_ROOT=$(dirname "$SCRIPT_DIR")

exec "$PROJECT_ROOT/.tools/node-v24.21.0-darwin-x64/bin/node" \
  "$PROJECT_ROOT/.tools/tastytrade-mcp/dist/index.js"
