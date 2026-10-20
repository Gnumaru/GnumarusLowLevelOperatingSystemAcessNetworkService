#!/bin/bash
# Build the Kotlin service (needs Gradle 7+ and network on first run for deps).
set -e
cd "$(dirname "$0")"

# Pick a Gradle new enough for the Kotlin DSL plugin block (Gradle 7+).
# Candidates, in order: a local toolchain under ../tmp, sdkman, PATH.
GRADLE=""
for candidate in \
    "../tmp/gradle-9.8.1/bin/gradle" \
    "$HOME/.sdkman/candidates/gradle/current/bin/gradle" \
    "$(command -v gradle 2>/dev/null)"
do
    [ -n "$candidate" ] && [ -x "$candidate" ] || continue
    version=$("$candidate" --version 2>/dev/null | awk '/^Gradle /{print $2; exit}')
    [ -n "$version" ] || continue
    major=${version%%.*}
    if [ "$major" -ge 7 ] 2>/dev/null; then
        GRADLE="$candidate"
        break
    fi
done
if [ -z "$GRADLE" ]; then
    echo "error: no Gradle 7+ found (looked in ../tmp/gradle-9.8.1, sdkman and PATH)" >&2
    echo "hint: download Gradle 9.x into ./tmp/gradle-9.8.1 or install it with sdkman" >&2
    exit 1
fi

"$GRADLE" installDist -q
echo "built: build/install/GnumarusOSBridge/bin/GnumarusOSBridge"
echo "run:   ./build/install/GnumarusOSBridge/bin/GnumarusOSBridge [--host H] [--port P] [--token T] [--auth|--no-auth]"
