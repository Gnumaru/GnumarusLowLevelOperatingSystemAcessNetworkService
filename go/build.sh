#!/bin/bash
# Build the Go service (needs network on first run for the sqlite module).
set -e
cd "$(dirname "$0")"
export GOFLAGS=-mod=mod
go mod tidy
go build -o GnumarusOSBridge .
chmod +x GnumarusOSBridge
echo "built: GnumarusOSBridge"
echo "run:   ./GnumarusOSBridge [--host H] [--port P] [--token T] [--auth|--no-auth]"
