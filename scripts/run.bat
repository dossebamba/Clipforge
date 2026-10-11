@echo off
REM Lance Clipforge (dashboard + worker + surveillance) : http://127.0.0.1:8000
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Environnement .venv introuvable. Lance d'abord : python -m venv .venv ^&^& .venv\Scripts\pip install -e ".[dev]"
  pause
  exit /b 1
)
echo Clipforge demarre sur http://127.0.0.1:8000  (Ctrl+C pour arreter)
".venv\Scripts\python.exe" -m clipforge.cli serve
pause
