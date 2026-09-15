@echo off
setlocal
cd /d "%~dp0"

if /I "%~1"=="stop" goto :stop
if /I "%~1"=="status" goto :status
if /I "%~1"=="logs" goto :logs

echo ============================================
echo   Langfuse - observability for research agent
echo ============================================
echo.

docker info >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Docker is not running. Please start Docker Desktop first.
    pause
    exit /b 1
)

echo [1/2] Starting containers: postgres, clickhouse, redis, minio, web, worker
docker compose up -d langfuse-web langfuse-worker
if errorlevel 1 (
    echo [ERROR] Failed to start. Run "langfuse.bat logs" to inspect.
    pause
    exit /b 1
)

echo [2/2] Waiting for the web UI to become ready...
%SystemRoot%\System32\timeout.exe /t 15 /nobreak >nul
start "" http://localhost:3000

echo.
echo Done. Langfuse UI: http://localhost:3000
echo   login email: demo@local.dev
echo   password   : see LANGFUSE_INIT_USER_PASSWORD in .env
echo.
echo This runs 6 containers and uses roughly 2-4 GB of RAM.
echo When finished, free the memory with:  langfuse.bat stop
echo Other commands:  langfuse.bat status ^| langfuse.bat logs
echo.
pause
exit /b 0

:stop
echo Stopping Langfuse containers. Data stays in docker volumes.
docker compose stop langfuse-web langfuse-worker clickhouse minio redis postgres
echo Stopped.
pause
exit /b 0

:status
docker compose ps
pause
exit /b 0

:logs
docker compose logs --tail 60 langfuse-web
pause
exit /b 0
