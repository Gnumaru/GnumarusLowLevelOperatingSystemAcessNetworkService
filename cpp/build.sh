#!/bin/bash
# Build the C++ service (vendored deps, no network needed).
set -e
cd "$(dirname "$0")"
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build/
mkdir -p bin
cp build/gnumarus bin/gnumarus
echo "built: bin/gnumarus"
echo "run:   ./bin/gnumarus [--host H] [--port P] [--token T] [--auth|--no-auth]"
