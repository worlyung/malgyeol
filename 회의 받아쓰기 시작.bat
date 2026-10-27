@echo off
rem ---------------------------------------------------------------
rem  One-click start. First run installs everything (10-30 min) and
rem  makes a desktop shortcut; later runs just open the program.
rem  (Keep this file ASCII-only: cmd misreads non-ASCII lines.)
rem ---------------------------------------------------------------
chcp 65001 > nul
setlocal
cd /d "%~dp0"

rem Development: use .venv only if its Python really runs
rem (the files can remain after the base Python it points to was removed).
if exist ".venv\Scripts\pythonw.exe" (
  ".venv\Scripts\python.exe" --version > nul 2>&1
  if not errorlevel 1 (
    start "" ".venv\Scripts\pythonw.exe" -m app.gui
    exit /b 0
  )
  echo [!] The .venv folder exists but its Python does not run - the base Python may have been removed.
  echo     Using the bundled Python instead. To fix .venv, delete the .venv folder and create it again:
  echo       python -m venv .venv
  echo       .venv\Scripts\python.exe -m pip install -r requirements.txt
  echo.
)

if exist "python\.ready" (
  "python\python.exe" --version > nul 2>&1
  if not errorlevel 1 (
    if not exist "python\.shortcut" powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\make_shortcut.ps1"
    start "" "python\pythonw.exe" -m app.gui
    exit /b 0
  )
  echo [!] The bundled Python in the python folder does not run. Installing it again...
  del "python\.ready" > nul 2>&1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\first_run.ps1"
if errorlevel 1 pause
exit /b 0
