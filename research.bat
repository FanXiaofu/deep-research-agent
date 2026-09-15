@echo off
setlocal
cd /d "%~dp0"

set VENV_PY=E:\AIProject\environment\deep-research-agent-venv\Scripts\python.exe

if not exist "%VENV_PY%" (
    echo [ERROR] Python venv not found: %VENV_PY%
    pause
    exit /b 1
)

if "%~1"=="" (
    echo Usage:
    echo   research.bat "your research question"
    echo   research.bat "your question" --max-sub 3 --rounds 2
    echo   research.bat --resume
    echo.
    echo Examples:
    echo   research.bat "What is Agentic RAG" --no-review
    echo   research.bat "Milvus vs Qdrant" --max-sub 4 --rounds 2
    pause
    exit /b 1
)

rem Make sure local Ollama is up when .env points at it
findstr /C:"localhost:11434" .env >nul 2>&1
if not errorlevel 1 (
    curl -s -o nul --max-time 4 http://localhost:11434/api/tags 2>nul
    if errorlevel 1 (
        echo Starting "ollama serve"...
        start "ollama" /min ollama serve
        %SystemRoot%\System32\timeout.exe /t 6 /nobreak >nul
    )
)

"%VENV_PY%" cli.py %*

echo.
pause
endlocal
