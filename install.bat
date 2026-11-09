@echo off
setlocal
rem iCast installer.
rem Creates a Python environment in %LOCALAPPDATA%\iCast\venv, installs the
rem dependencies and creates "iCast" shortcuts (Desktop + Start menu) that
rem point at this folder. Run it again after moving the folder.

set "APPDIR=%~dp0"
if "%APPDIR:~-1%"=="\" set "APPDIR=%APPDIR:~0,-1%"
set "VENV=%LOCALAPPDATA%\iCast\venv"

where python >nul 2>nul || (
  echo Python was not found. Install Python 3.11 or newer from https://www.python.org/downloads/
  echo ^(tick "Add python.exe to PATH" during setup^) and run install.bat again.
  pause & exit /b 1
)
python -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>nul || (
  echo Python 3.11 or newer is required. Install it from https://www.python.org/downloads/
  echo and run install.bat again.
  pause & exit /b 1
)

if not exist "%VENV%\Scripts\python.exe" (
  echo Creating the Python environment in "%VENV%" ...
  python -m venv --without-pip "%VENV%" || (pause & exit /b 1)
)

echo Installing the dependencies (this can take a few minutes)...
python -m pip --python "%VENV%\Scripts\python.exe" install -q --upgrade pip -r "%APPDIR%\requirements.txt" || (pause & exit /b 1)

echo Creating the shortcuts...
pushd "%~dp0" || exit /b 1
"%VENV%\Scripts\python.exe" -m icast.shortcuts
set "SHORTCUT_CODE=%ERRORLEVEL%"
popd
if not "%SHORTCUT_CODE%"=="0" (
  echo Could not create the shortcuts. You can still start the app with run.bat.
  pause & exit /b 1
)

echo.
echo Done. Start "iCast" from the Desktop or the Start menu.
pause
