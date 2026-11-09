@echo off
rem USB control helper. Usage:
rem   control.bat doctor            check everything USB control needs
rem   control.bat reveal-dev-mode   show the Developer Mode switch on the iPhone
rem   control.bat get-wda           download WebDriverAgent as an .ipa for Sideloadly
rem   control.bat start             start WebDriverAgent and keep it running (Ctrl+C stops)
setlocal
set "PY=%LOCALAPPDATA%\iCast\venv\Scripts\python.exe"
if not exist "%PY%" (
  echo iCast is not installed yet. Run install.bat first.
  exit /b 1
)
pushd "%~dp0"
if "%~1"=="" (
  "%PY%" -m icast.control --help
) else (
  "%PY%" -m icast.control %*
)
set "CODE=%ERRORLEVEL%"
popd
exit /b %CODE%
