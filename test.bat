@echo off
setlocal
cd /d "%~dp0"

set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

echo ==========================================================
echo  ResearchForge - offline checks (no network required)
echo ==========================================================
echo.

echo === Unit test suite ===
"%PY%" -m unittest discover -s tests -t .
if errorlevel 1 goto :failed

echo.
echo === Offline benchmark / evaluation harness ===
"%PY%" tools\benchmark.py
if errorlevel 1 goto :failed

echo.
echo ==========================================================
echo  All checks passed.
echo  Report: reports\benchmark_report.md
echo ==========================================================
endlocal
exit /b 0

:failed
echo.
echo ==========================================================
echo  One or more checks FAILED - see the output above.
echo ==========================================================
endlocal
exit /b 1
