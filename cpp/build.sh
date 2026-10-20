#!/bin/bash
# Build the C++ service (vendored deps, no network needed).
set -e
cd "$(dirname "$0")"
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build/
mkdir -p bin
cp build/GnumarusOSBridge bin/GnumarusOSBridge
chmod +x bin/GnumarusOSBridge
echo "built: bin/GnumarusOSBridge"
echo "run:   ./bin/GnumarusOSBridge [--host H] [--port P] [--token T] [--auth|--no-auth]"
