@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PYTHON_MODE="
set "PYTHON_EXE="
set "PYTHON_VERSION="

call :find_python
if defined PYTHON_MODE goto :run_setup

echo Python 3.11 or newer Python 3.x was not found.
where winget >nul 2>&1
if errorlevel 1 (
  echo Course Compiler can install Python automatically when Windows App Installer / winget is available.
  echo Install Python 3.11 from python.org, then run this file again.
  goto :fail
)

echo Installing Python 3.11 with winget...
winget install --id Python.Python.3.11 --exact --accept-package-agreements --accept-source-agreements --disable-interactivity
if errorlevel 1 goto :fail

call :find_python
if not defined PYTHON_MODE (
  if exist "%LocalAppData%\Programs\Python\Python311\python.exe" (
    set "PYTHON_MODE=exe"
    set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python311\python.exe"
  )
)
if not defined PYTHON_MODE (
  echo Python was installed but could not be found in this terminal.
  echo Close this window and run setup-course-compiler.cmd again.
  goto :fail
)

:run_setup
if "%PYTHON_MODE%"=="launcher" (
  py -%PYTHON_VERSION% setup_handouter.py %*
) else (
  "%PYTHON_EXE%" setup_handouter.py %*
)
if errorlevel 1 goto :fail

echo.
echo Course Compiler is ready. Double-click start-course-compiler.cmd to launch it.
pause
exit /b 0

:find_python
set "PYTHON_MODE="
set "PYTHON_EXE="
set "PYTHON_VERSION="
where py >nul 2>&1
if not errorlevel 1 (
  for %%V in (3.14 3.13 3.12 3.11 3) do (
    if not defined PYTHON_MODE (
      py -%%V -c "import sys; raise SystemExit(0 if (3,11) <= sys.version_info[:2] < (4,0) else 1)" >nul 2>&1
      if not errorlevel 1 (
        set "PYTHON_MODE=launcher"
        set "PYTHON_VERSION=%%V"
      )
    )
  )
  if defined PYTHON_MODE goto :eof
)
where python >nul 2>&1
if not errorlevel 1 (
  python -c "import sys; raise SystemExit(0 if (3,11) <= sys.version_info[:2] < (4,0) else 1)" >nul 2>&1
  if not errorlevel 1 (
    for /f "delims=" %%P in ('where python') do if not defined PYTHON_EXE set "PYTHON_EXE=%%P"
    set "PYTHON_MODE=exe"
    goto :eof
  )
)
for %%P in ("%LocalAppData%\Programs\Python\Python314\python.exe" "%LocalAppData%\Programs\Python\Python313\python.exe" "%LocalAppData%\Programs\Python\Python312\python.exe" "%LocalAppData%\Programs\Python\Python311\python.exe") do (
  if exist "%%~P" (
    "%%~P" -c "import sys; raise SystemExit(0 if (3,11) <= sys.version_info[:2] < (4,0) else 1)" >nul 2>&1
    if not errorlevel 1 (
      set "PYTHON_MODE=exe"
      set "PYTHON_EXE=%%~P"
      goto :eof
    )
  )
)
goto :eof

:fail
echo.
echo Setup failed. Fix the message above, then run setup-course-compiler.cmd again.
pause
exit /b 1
