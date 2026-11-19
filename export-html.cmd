@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup-coursecompiler.cmd first.
  pause
  exit /b 1
)
if "%~1"=="" (
  echo Drag a Markdown file or lecture folder onto this script.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m coursecompiler export-html "%~1"
pause
