@echo off
setlocal
rem Starts iCast without a console window. Run install.bat once first.
set "PY=%LOCALAPPDATA%\iCast\venv\Scripts\pythonw.exe"
if not exist "%PY%" (
  echo iCast is not installed yet. Run install.bat first.
  pause
  exit /b 1
)
pushd "%~dp0" || exit /b 1
start "" "%PY%" -m icast
popd
