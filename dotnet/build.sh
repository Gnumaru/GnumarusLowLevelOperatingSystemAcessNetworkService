#!/bin/bash
# Build the .NET service (needs network on first run for NuGet restore).
set -e
cd "$(dirname "$0")"
# NOTE: plain IPv6 is broken in this environment; force IPv4 for restore.
export DOTNET_SYSTEM_NET_DISABLEIPV6=1
dotnet publish -c Release -o publish/
echo "built: publish/GnumarusOSBridge.dll"
echo "run:   dotnet publish/GnumarusOSBridge.dll [--host H] [--port P] [--token T] [--auth|--no-auth]"
