@echo off
setlocal
cd /d "%~dp0"

set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

if not exist ".env" (
    echo [WARN] No .env found - running with built-in defaults.
    echo        Run setup.bat first for the full one-time configuration.
    echo.
)

echo Starting ResearchForge. Open http://127.0.0.1:8000/ and press Ctrl+C to stop.
echo.
"%PY%" -m app.main %*

endlocal
