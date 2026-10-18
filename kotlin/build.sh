#!/bin/bash
# Build the Kotlin service (needs Gradle + network on first run for deps).
set -e
cd "$(dirname "$0")"
if command -v gradle >/dev/null 2>&1; then
    GRADLE=gradle
else
    GRADLE="$HOME/.sdkman/candidates/gradle/current/bin/gradle"
fi
"$GRADLE" installDist -q
echo "built: build/install/gnumarus/bin/gnumarus"
echo "run:   ./build/install/gnumarus/bin/gnumarus [--host H] [--port P] [--token T] [--auth|--no-auth]"
