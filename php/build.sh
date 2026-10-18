#!/usr/bin/env bash
# Build the PHP port: install composer dependencies into php/vendor/.
set -euo pipefail
cd "$(dirname "$0")"
if ! command -v php >/dev/null 2>&1; then
    echo "error: php interpreter not found (install php8.5-cli)" >&2
    exit 1
fi
if ! command -v composer >/dev/null 2>&1; then
    echo "error: composer not found (install composer, user-level is fine)" >&2
    exit 1
fi
export COMPOSER_ALLOW_SUPERUSER=1
composer install --no-dev --no-interaction --no-progress
echo "built: php/vendor/autoload.php"
