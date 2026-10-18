@echo off
rem Launcher for any service runtime (implementation) - Windows cmd version.
rem     start.bat --runtime <name> [server args...]
rem Only the FIRST --runtime selects the implementation (--runtime=name
rem too); everything else is forwarded verbatim (including --port for the
rem TCP port and --token), and stdin flows straight to the server.
rem If the implementation's artifact is missing, build instructions are
rem printed and the python runtime is started as fallback.
rem NOTE: plain ASCII only in this file (cmd mangles UTF-8); keep it so.
setlocal
cd /d "%~dp0"

set "RUNTIME_NAME="
set "FORWARD="
set "GOT_RUNTIME="

:parse_loop
if "%~1"=="" goto parsed
if "%~1"=="-h" goto help
if "%~1"=="--help" goto help
if "%~1"=="--runtime" goto runtime_flag
set "A=%~1"
if "%A:~0,10%"=="--runtime=" goto runtime_eq
set "FORWARD=%FORWARD% %1"
shift
goto parse_loop

:runtime_flag
if defined GOT_RUNTIME goto fwd_two
if "%~2"=="" goto need_value
set "RUNTIME_NAME=%~2"
set "GOT_RUNTIME=1"
shift
shift
goto parse_loop

:runtime_eq
if defined GOT_RUNTIME goto fwd_one
set "RUNTIME_NAME=%A:~10%"
if "%RUNTIME_NAME%"=="" goto need_value
set "GOT_RUNTIME=1"
shift
goto parse_loop

:fwd_two
set "FORWARD=%FORWARD% %1 %2"
shift
shift
goto parse_loop

:fwd_one
set "FORWARD=%FORWARD% %1"
shift
goto parse_loop

:need_value
echo start.bat: --runtime needs an implementation name>&2
goto usage_exit

:parsed
if "%RUNTIME_NAME%"=="" goto missing_name
if "%RUNTIME_NAME%"=="python" goto run_python
if "%RUNTIME_NAME%"=="kotlin" goto check_kotlin
if "%RUNTIME_NAME%"=="php" goto check_php
if "%RUNTIME_NAME%"=="ruby" goto check_ruby
if "%RUNTIME_NAME%"=="nodejs" goto check_nodejs
if "%RUNTIME_NAME%"=="go" goto check_go
if "%RUNTIME_NAME%"=="dotnet" goto check_dotnet
if "%RUNTIME_NAME%"=="rust" goto check_rust
echo start.bat: unknown runtime '%RUNTIME_NAME%'>&2
goto usage_exit

:missing_name
echo start.bat: missing --runtime ^<name^>>&2
goto usage_exit

:help
echo Usage: start.bat --runtime ^<name^> [server args...]>&2
echo.>&2
echo ^<name^> is one of: python, kotlin, php, ruby, nodejs, go, dotnet, rust>&2
echo Only the first "--runtime" selects the implementation (--runtime=name>&2
echo also works); everything else is forwarded to the server verbatim.>&2
echo Use --port ^<n^> for the TCP port, --token ^<t^> etc.;>&2
echo stdin is passed through untouched.>&2
exit /b 0

:usage_exit
echo Usage: start.bat --runtime ^<name^> [server args...]>&2
exit /b 2

:run_python
where python >nul 2>nul
if errorlevel 1 goto no_python
set "RUN=python python3\server.py"
goto run

:check_kotlin
if exist "kotlin\build\install\gnumarus\bin\gnumarus.bat" goto run_kotlin
set "MISSING=kotlin\build\install\gnumarus\bin\gnumarus.bat"
set "BUILD_HINT=  install Gradle, then run gradle installDist inside kotlin\"
goto fallback

:run_kotlin
set "RUN=kotlin\build\install\gnumarus\bin\gnumarus.bat"
goto run

:check_php
where php >nul 2>nul
if errorlevel 1 goto no_php
if exist "php\server.php" goto run_php
set "MISSING=php\server.php"
set "BUILD_HINT=  (nothing to build: checkout looks incomplete)"
goto fallback

:run_php
set "RUN=php php\server.php"
goto run

:check_ruby
where ruby >nul 2>nul
if errorlevel 1 goto no_ruby
if exist "ruby\vendor\gems" goto run_ruby
set "MISSING=ruby\vendor\gems (sqlite3 driver)"
set "BUILD_HINT=  install Ruby 3.3+, then: gem install --install-dir ruby\vendor\gems sqlite3"
goto fallback

:run_ruby
set "RUN=ruby ruby\server.rb"
goto run

:check_nodejs
where node >nul 2>nul
if errorlevel 1 goto no_node
if exist "nodejs\dist\server.js" goto run_nodejs
set "MISSING=nodejs\dist\server.js"
set "BUILD_HINT=  in nodejs\: npm install, then npm run build"
goto fallback

:run_nodejs
set "RUN=node nodejs\dist\server.js"
goto run

:check_go
if exist "go\gnumarus.exe" goto run_go
set "MISSING=go\gnumarus.exe"
set "BUILD_HINT=  in go\: go build -o gnumarus ."
goto fallback

:run_go
set "RUN=go\gnumarus.exe"
goto run

:check_dotnet
where dotnet >nul 2>nul
if errorlevel 1 goto no_dotnet
if exist "dotnet\publish\gnumarus.dll" goto run_dotnet
set "MISSING=dotnet\publish\gnumarus.dll"
set "BUILD_HINT=  in dotnet\: dotnet publish -c Release -o publish\"
goto fallback

:run_dotnet
set "RUN=dotnet dotnet\publish\gnumarus.dll"
goto run

:check_rust
if exist "rust\bin\gnumarus.exe" goto run_rust
set "MISSING=rust\bin\gnumarus.exe"
set "BUILD_HINT=  in rust\: cargo build --release, then copy target\release\gnumarus.exe to rust\bin\"
goto fallback

:run_rust
set "RUN=rust\bin\gnumarus.exe"
goto run

:no_python
echo start.bat: python not found in PATH>&2
exit /b 1

:no_php
echo start.bat: php not found in PATH>&2
exit /b 1

:no_ruby
echo start.bat: ruby not found in PATH>&2
exit /b 1

:no_node
echo start.bat: node not found in PATH>&2
exit /b 1

:no_dotnet
echo start.bat: dotnet not found in PATH>&2
exit /b 1

:fallback
echo start.bat: cannot start '%RUNTIME_NAME%': missing %MISSING%>&2
echo To build it, run:>&2
echo %BUILD_HINT%>&2
echo Falling back to the python runtime.>&2
where python >nul 2>nul
if errorlevel 1 goto no_python
set "RUN=python python3\server.py"
goto run

:run
%RUN%%FORWARD%
exit /b %errorlevel%
