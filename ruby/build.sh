#!/usr/bin/env bash
# Build the Ruby port: vendored gems only (the service itself is pure
# stdlib). Installs the sqlite3 driver into ruby/vendor/gems, which
# server.rb puts on Gem's path. Without it the port still runs; the
# sqlite routes answer 503 "SQLite support unavailable" (like the
# other ports when their driver is missing).
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v ruby >/dev/null 2>&1; then
    echo "error: ruby interpreter not found (install ruby3.3+, no root needed)" >&2
    exit 1
fi

ruby -v
gem install --install-dir "$PWD/vendor/gems" --no-document sqlite3
echo "built: ruby/vendor/gems (sqlite3)"
