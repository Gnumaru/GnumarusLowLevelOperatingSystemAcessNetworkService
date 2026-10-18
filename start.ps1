# Launcher for any service runtime (implementation) - PowerShell version.
# Works on stock Windows PowerShell 5.1 (no PS7-only syntax used).
#     powershell -File start.ps1 --runtime <name> [server args...]
# (If execution policy blocks it:
#  powershell -ExecutionPolicy Bypass -File start.ps1 --runtime <name> ...)
# Only the FIRST --runtime selects the implementation (--runtime=name too);
# everything else is forwarded verbatim (arg array, no quote mangling),
# and stdin flows straight to the server. Missing artifact prints build
# instructions and falls back to the python runtime.
# NOTE: plain ASCII only in this file (PS 5.1 reads no-BOM as ANSI).

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function Write-Stderr([string]$Text) {
    [Console]::Error.WriteLine($Text)
}

function Show-Usage {
    Write-Stderr 'Usage: start.ps1 --runtime <name> [server args...]'
    Write-Stderr ''
    Write-Stderr '<name> is one of: python, kotlin, php, ruby, nodejs, go, dotnet, rust'
    Write-Stderr 'The first "--runtime <name>" (or --runtime=<name>) selects the'
    Write-Stderr 'implementation; everything else is forwarded to the server verbatim'
    Write-Stderr '(use --port <n> for the TCP port, --token <t>, etc.). stdin is'
    Write-Stderr 'passed through untouched.'
}

Set-Location -Path $PSScriptRoot

$runtimeName = ''
$forward = New-Object System.Collections.ArrayList
$gotRuntime = $false
$i = 0
while ($i -lt $args.Count) {
    $a = [string]$args[$i]
    if ($a -eq '-h' -or $a -eq '--help') {
        Show-Usage
        exit 0
    } elseif ($a -eq '--runtime') {
        if ($gotRuntime) {
            [void]$forward.Add($a)
            if ($i + 1 -lt $args.Count) {
                [void]$forward.Add([string]$args[$i + 1])
                $i += 1
            }
        } else {
            if ($i + 1 -ge $args.Count) {
                Write-Stderr 'start.ps1: --runtime needs an implementation name'
                Show-Usage
                exit 2
            }
            $runtimeName = [string]$args[$i + 1]
            $gotRuntime = $true
            $i += 1
        }
    } elseif ($a.StartsWith('--runtime=')) {
        if ($gotRuntime) {
            [void]$forward.Add($a)
        } else {
            $v = $a.Substring(10)
            if ($v -eq '') {
                Write-Stderr 'start.ps1: --runtime needs an implementation name'
                Show-Usage
                exit 2
            }
            $runtimeName = $v
            $gotRuntime = $true
        }
    } else {
        [void]$forward.Add($a)
    }
    $i += 1
}

if ($runtimeName -eq '') {
    Write-Stderr 'start.ps1: missing --runtime <name>'
    Show-Usage
    exit 2
}

$exe = ''
$exeArgs = @()
$missing = ''
$hint = ''

switch ($runtimeName) {
    'python' {
        if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
            Write-Stderr 'start.ps1: python not found in PATH'
            exit 1
        }
        $exe = 'python'
        $exeArgs = @('python3/server.py')
    }
    'kotlin' {
        if (-not (Test-Path 'kotlin/build/install/gnumarus/bin/gnumarus.bat')) {
            $missing = 'kotlin\build\install\gnumarus\bin\gnumarus.bat'
            $hint = '  install Gradle, then run gradle installDist inside kotlin\'
        } else {
            $exe = 'kotlin/build/install/gnumarus/bin/gnumarus.bat'
        }
    }
    'php' {
        if (-not (Get-Command php -ErrorAction SilentlyContinue)) {
            Write-Stderr 'start.ps1: php not found in PATH'
            exit 1
        } elseif (-not (Test-Path 'php/server.php')) {
            $missing = 'php\server.php'
            $hint = '  (nothing to build: checkout looks incomplete)'
        } else {
            $exe = 'php'
            $exeArgs = @('php/server.php')
        }
    }
    'ruby' {
        if (-not (Get-Command ruby -ErrorAction SilentlyContinue)) {
            Write-Stderr 'start.ps1: ruby not found in PATH'
            exit 1
        } elseif (-not (Test-Path 'ruby/vendor/gems')) {
            $missing = 'ruby\vendor\gems (sqlite3 driver)'
            $hint = '  install Ruby 3.3+, then: gem install --install-dir ruby\vendor\gems sqlite3'
        } else {
            $exe = 'ruby'
            $exeArgs = @('ruby/server.rb')
        }
    }
    'nodejs' {
        if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
            Write-Stderr 'start.ps1: node not found in PATH'
            exit 1
        } elseif (-not (Test-Path 'nodejs/dist/server.js')) {
            $missing = 'nodejs\dist\server.js'
            $hint = '  in nodejs\: npm install, then npm run build'
        } else {
            $exe = 'node'
            $exeArgs = @('nodejs/dist/server.js')
        }
    }
    'go' {
        if (-not (Test-Path 'go/gnumarus.exe')) {
            $missing = 'go\gnumarus.exe'
            $hint = '  in go\: go build -o gnumarus .'
        } else {
            $exe = 'go/gnumarus.exe'
        }
    }
    'dotnet' {
        if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
            Write-Stderr 'start.ps1: dotnet not found in PATH'
            exit 1
        } elseif (-not (Test-Path 'dotnet/publish/gnumarus.dll')) {
            $missing = 'dotnet\publish\gnumarus.dll'
            $hint = '  in dotnet\: dotnet publish -c Release -o publish\'
        } else {
            $exe = 'dotnet'
            $exeArgs = @('dotnet/publish/gnumarus.dll')
        }
    }
    'rust' {
        if (-not (Test-Path 'rust/bin/gnumarus.exe')) {
            $missing = 'rust\bin\gnumarus.exe'
            $hint = '  in rust\: cargo build --release, then copy target\release\gnumarus.exe to rust\bin\'
        } else {
            $exe = 'rust/bin/gnumarus.exe'
        }
    }
    default {
        Write-Stderr "start.ps1: unknown runtime '$runtimeName'"
        Show-Usage
        exit 2
    }
}

if ($missing -ne '') {
    Write-Stderr "start.ps1: cannot start '$runtimeName': missing $missing"
    Write-Stderr 'To build it, run:'
    Write-Stderr $hint
    Write-Stderr 'Falling back to the python runtime.'
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        Write-Stderr 'start.ps1: python not found in PATH'
        exit 1
    }
    $exe = 'python'
    $exeArgs = @('python3/server.py')
}

$allArgs = $exeArgs + @($forward)
& $exe @allArgs
exit $LASTEXITCODE
