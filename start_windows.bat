@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\installed-v9.ok" goto run

rem UK Flood Sandbox. OpenSWMM 6.0.0a4 ships Windows installers for Python 3.10 to 3.13 only.
set "PY="
for %%V in (3.13 3.12 3.11 3.10) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
  )
)
if not defined PY (
  echo.
  echo No supported Python found. OpenSWMM needs Python 3.10 to 3.13, 64-bit.
  echo Install Python 3.13 from https://www.python.org/downloads/windows/
  echo It can sit alongside Python 3.14. Then run this file again.
  echo.
  pause
  exit /b 1
)
echo Setting up with %PY% ...
if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv || goto fail
)
".venv\Scripts\python.exe" -m pip install --upgrade pip || goto fail
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto fail
echo ok> ".venv\installed-v9.ok"

:run
".venv\Scripts\python.exe" server.py
pause
exit /b 0

:fail
echo.
echo Setup failed. See the messages above.
pause
exit /b 1
