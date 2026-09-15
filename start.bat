@echo off
setlocal
cd /d "%~dp0"

set VENV_PY=E:\AIProject\environment\deep-research-agent-venv\Scripts\python.exe

if not exist "%VENV_PY%" (
    echo [ERROR] Python venv not found:
    echo         %VENV_PY%
    echo         Please check the path or recreate the venv.
    pause
    exit /b 1
)

if not exist ".env" (
    echo [ERROR] .env not found. Copy .env.example to .env and fill in your keys.
    pause
    exit /b 1
)

echo ============================================
echo   Deep Research Agent - Web UI
echo ============================================
echo.

rem If .env points at a local Ollama, make sure the service is up
findstr /C:"localhost:11434" .env >nul 2>&1
if not errorlevel 1 (
    echo [1/3] Local Ollama detected in .env, checking service...
    curl -s -o nul --max-time 4 http://localhost:11434/api/tags 2>nul
    if errorlevel 1 (
        echo       Not running. Starting "ollama serve" in a new window...
        start "ollama" /min ollama serve
        %SystemRoot%\System32\timeout.exe /t 6 /nobreak >nul
        echo       Started. First request may be slow while the model loads.
    ) else (
        echo       Already running.
    )
) else (
    echo [1/3] Using a remote LLM API - no local Ollama needed.
)

echo [2/3] Opening http://127.0.0.1:8000 in your browser...
start "" http://127.0.0.1:8000

echo [3/3] Starting web server. Press Ctrl+C in this window to stop.
echo.
"%VENV_PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000

echo.
echo Server stopped.
pause
endlocal
