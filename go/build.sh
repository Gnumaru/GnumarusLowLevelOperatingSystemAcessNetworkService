#!/bin/bash
# Build the Go service (needs network on first run for the sqlite module).
set -e
cd "$(dirname "$0")"
export GOFLAGS=-mod=mod
go mod tidy
go build -o gnumarus .
echo "built: gnumarus"
echo "run:   ./gnumarus [--host H] [--port P] [--token T] [--auth|--no-auth]"
