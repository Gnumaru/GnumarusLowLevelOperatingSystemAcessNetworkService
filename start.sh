#!/bin/bash
# Launcher for any service runtime (implementation):
#     ./start.sh --runtime <name> [server args...]
#
# The first "--runtime <name>" selects the implementation; everything
# else (including --port for the TCP port and --token) is forwarded
# verbatim, and stdin flows straight to the server (never consumed
# here). If the implementation's artifact is missing, build
# instructions are printed and the python runtime is started as
# fallback.
#
# Examples:
#     ./start.sh --runtime python -- --token secret
#     ./start.sh --runtime go --token secret --port 1423
#     echo "token = t" | ./start.sh --runtime rust --port 1423

set -u
cd "$(dirname "$0")"

usage() {
    cat >&2 <<'EOF'
Usage: ./start.sh --runtime <name> [server args...]

<name> is one of: python, kotlin, php, ruby, nodejs, go, dotnet, rust, cpp
The first "--runtime <name>" (or --runtime=<name>) selects the
implementation; everything else is forwarded to the server verbatim
(use --port <n> for the TCP port, --token <t>, etc.). stdin is passed
through untouched.
EOF
}

RUNTIME_NAME=""
FORWARD=()
FOUND_RUNTIME_FLAG=0
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        --runtime)
            if [ "$FOUND_RUNTIME_FLAG" -eq 0 ]; then
                if [ $# -lt 2 ]; then
                    echo "start.sh: --runtime needs an implementation name" >&2
                    usage
                    exit 2
                fi
                RUNTIME_NAME="$2"
                FOUND_RUNTIME_FLAG=1
                shift 2
            else
                FORWARD+=("$1")
                shift
            fi
            ;;
        --runtime=*)
            if [ "$FOUND_RUNTIME_FLAG" -eq 0 ]; then
                RUNTIME_NAME="${1#--runtime=}"
                FOUND_RUNTIME_FLAG=1
                shift
            else
                FORWARD+=("$1")
                shift
            fi
            ;;
        *)
            FORWARD+=("$1")
            shift
            ;;
    esac
done

if [ -z "$RUNTIME_NAME" ]; then
    echo "start.sh: missing --runtime <name>" >&2
    usage
    exit 2
fi

# build instructions shown when an artifact is missing
kotlin_build="  ./kotlin/build.sh   # needs Gradle + network on first run"
nodejs_build="  ./nodejs/build.sh   # runs: npm install + tsc"
go_build="  ./go/build.sh       # needs network on first run (sqlite module)"
dotnet_build="  ./dotnet/build.sh   # needs network on first run (NuGet restore)"
rust_build="  ./rust/build.sh     # needs network on first run (crates.io)"
ruby_build="  ./ruby/build.sh     # installs the sqlite3 gem into ruby/vendor (no root)"
cpp_build="  ./cpp/build.sh       # needs cmake + ninja (vendored deps, no network)"

LAUNCH=()
case "$RUNTIME_NAME" in
    python)
        LAUNCH=(python3 python3/server.py)
        ;;
    kotlin)
        if [ ! -x kotlin/build/install/GnumarusOSBridge/bin/GnumarusOSBridge ]; then
            MISSING="kotlin/build/install/GnumarusOSBridge/bin/GnumarusOSBridge"
            BUILD_HINT="$kotlin_build"
        else
            LAUNCH=(kotlin/build/install/GnumarusOSBridge/bin/GnumarusOSBridge)
        fi
        ;;
    php)
        if ! command -v php >/dev/null 2>&1; then
            MISSING="php interpreter (php)"
            BUILD_HINT="  install php8.5-cli (then: ./php/build.sh)"
        elif [ ! -f php/vendor/autoload.php ]; then
            MISSING="php/vendor/autoload.php"
            BUILD_HINT="  ./php/build.sh (composer install)"
        elif [ ! -f php/server.php ]; then
            MISSING="php/server.php"
            BUILD_HINT="  (nothing to build: checkout looks incomplete)"
        else
            LAUNCH=(php php/server.php)
        fi
        ;;
    ruby)
        if ! command -v ruby >/dev/null 2>&1; then
            MISSING="ruby interpreter (ruby)"
            BUILD_HINT="  install ruby3.3+ (then: ./ruby/build.sh)"
        elif [ ! -f ruby/server.rb ]; then
            MISSING="ruby/server.rb"
            BUILD_HINT="  (nothing to build: checkout looks incomplete)"
        elif [ ! -d ruby/vendor/gems ]; then
            MISSING="ruby/vendor/gems (sqlite3 driver)"
            BUILD_HINT="$ruby_build"
        else
            LAUNCH=(ruby ruby/server.rb)
        fi
        ;;
    nodejs)
        if ! command -v node >/dev/null 2>&1; then
            MISSING="node interpreter (node)"
            BUILD_HINT="  install nodejs (then: $nodejs_build)"
        elif [ ! -f nodejs/dist/server.js ]; then
            MISSING="nodejs/dist/server.js"
            BUILD_HINT="$nodejs_build"
        else
            LAUNCH=(node nodejs/dist/server.js)
        fi
        ;;
    go)
        if [ ! -x go/GnumarusOSBridge ]; then
            MISSING="go/GnumarusOSBridge"
            BUILD_HINT="$go_build"
        else
            LAUNCH=(./go/GnumarusOSBridge)
        fi
        ;;
    dotnet)
        if ! command -v dotnet >/dev/null 2>&1; then
            MISSING="dotnet runtime (dotnet)"
            BUILD_HINT="  install the .NET SDK (then: $dotnet_build)"
        elif [ ! -f dotnet/publish/GnumarusOSBridge.dll ]; then
            MISSING="dotnet/publish/GnumarusOSBridge.dll"
            BUILD_HINT="$dotnet_build"
        else
            LAUNCH=(dotnet dotnet/publish/GnumarusOSBridge.dll)
        fi
        ;;
    rust)
        if [ ! -x rust/bin/GnumarusOSBridge ]; then
            MISSING="rust/bin/GnumarusOSBridge"
            BUILD_HINT="$rust_build"
        else
            LAUNCH=(./rust/bin/GnumarusOSBridge)
        fi
        ;;
    cpp)
        if [ ! -x cpp/bin/GnumarusOSBridge ]; then
            MISSING="cpp/bin/GnumarusOSBridge"
            BUILD_HINT="$cpp_build"
        else
            LAUNCH=(./cpp/bin/GnumarusOSBridge)
        fi
        ;;
    *)
        echo "start.sh: unknown runtime '$RUNTIME_NAME'" >&2
        usage
        exit 2
        ;;
esac

if [ -n "${MISSING:-}" ]; then
    cat >&2 <<EOF
start.sh: cannot start '$RUNTIME_NAME': missing $MISSING
To build it, run:
$BUILD_HINT
Falling back to the python runtime.
EOF
    LAUNCH=(python3 python3/server.py)
fi

exec "${LAUNCH[@]}" "${FORWARD[@]}"
