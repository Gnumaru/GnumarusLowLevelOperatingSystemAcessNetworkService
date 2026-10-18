#!/bin/bash
# Build the Rust service (needs network on first run for crates.io).
set -e
cd "$(dirname "$0")"
export PATH="$HOME/.cargo/bin:$PATH"
# NOTE: plain IPv6 is broken in this environment; force IPv4 for cargo.
export CARGO_NET_IPV4_ONLY=true
export CARGO_TARGET_DIR="$HOME/.rust_target_dirs/GnumarusPythonNativeOperationsHttpService"
cargo build --release
BIN="$CARGO_TARGET_DIR/release/gnumarus"
mkdir -p bin
cp "$BIN" bin/gnumarus
echo "built: bin/gnumarus"
echo "run:   ./bin/gnumarus [--host H] [--port P] [--token T] [--auth|--no-auth]"
