@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ==========================================================
echo  ResearchForge - one-time local setup
echo ==========================================================
echo.

where python >nul 2>nul
if errorlevel 1 goto :no_python

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 goto :old_python

if exist ".venv\Scripts\python.exe" (
    echo [1/4] Reusing the existing virtual environment in .venv
) else (
    echo [1/4] Creating a virtual environment in .venv ...
    python -m venv .venv
    if errorlevel 1 goto :venv_failed
)

echo [2/4] Installing runtime dependencies ...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet
if errorlevel 1 goto :pip_failed

if exist ".env" (
    echo [3/4] Keeping the existing .env
) else (
    echo [3/4] Creating .env from .env.example
    copy /y ".env.example" ".env" >nul
    if errorlevel 1 goto :env_failed
)

echo [4/4] Initialising the SQLite database ...
".venv\Scripts\python.exe" -c "from app.db import get_db; get_db(); print('       database ready')"
if errorlevel 1 goto :db_failed

echo.
echo Setup complete.
echo.
echo   run.bat    start the server at http://127.0.0.1:8000
echo   test.bat   run the unit suite and the offline benchmark harness
echo.
endlocal
exit /b 0

:no_python
echo [ERROR] Python was not found on PATH.
echo         Install Python 3.10+ from https://www.python.org/downloads/
echo         and tick "Add python.exe to PATH" during installation.
endlocal
exit /b 1

:old_python
echo [ERROR] Python 3.10 or newer is required. Found:
python --version
endlocal
exit /b 1

:venv_failed
echo [ERROR] Could not create the virtual environment.
endlocal
exit /b 1

:pip_failed
echo [ERROR] Dependency installation failed. Check your network connection and retry.
endlocal
exit /b 1

:env_failed
echo [ERROR] Could not create .env. Check file permissions in this folder.
endlocal
exit /b 1

:db_failed
echo [ERROR] Database initialisation failed. See the traceback above.
endlocal
exit /b 1
