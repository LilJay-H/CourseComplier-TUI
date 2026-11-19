@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup-coursecompiler.cmd first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" setup_coursecompiler.py --start
if errorlevel 1 pause
