#!/bin/bash
# Build the Node.js service (needs npm install once for devDeps).
set -e
cd "$(dirname "$0")"
if [ ! -d node_modules ]; then
  npm install
fi
./node_modules/.bin/tsc
echo "built: dist/server.js"
echo "run:   node dist/server.js [--host H] [--port P] [--token T] [--auth|--no-auth]"
