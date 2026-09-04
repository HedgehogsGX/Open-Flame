@echo off
setlocal DisableDelayedExpansion
rem Installed commands come from PATH, not an unrelated working directory.
set "NoDefaultCurrentDirectoryInExePath=1"
rem Only use installed runtimes, even when Python manager enables auto-install.
set "PYTHON_MANAGER_AUTOMATIC_INSTALL=false"
set "PYLAUNCHER_ALLOW_INSTALL="
set "PYLAUNCHER_ALWAYS_INSTALL="
set "PYLAUNCHER_DRYRUN="
set "PYLAUNCHER_DEBUG="
set "PYMANAGER_VERBOSE="
set "PYMANAGER_DEBUG="

py.exe -3 -I -c "import os,struct,sys; sys.exit(0 if sys.version_info >= (3,12) and struct.calcsize('P') == 8 and sys.implementation.name == 'cpython' and os.name == 'nt' and sys.platform == 'win32' else 2)" >nul 2>&1
if "%ERRORLEVEL%"=="0" (
  set "OPEN_FLAME_SETUP_PYTHON=py.exe"
  set "OPEN_FLAME_SETUP_SELECTOR=-3"
  goto run_setup
)
python.exe -I -c "import os,struct,sys; sys.exit(0 if sys.version_info >= (3,12) and struct.calcsize('P') == 8 and sys.implementation.name == 'cpython' and os.name == 'nt' and sys.platform == 'win32' else 2)" >nul 2>&1
if "%ERRORLEVEL%"=="0" (
  set "OPEN_FLAME_SETUP_PYTHON=python.exe"
  set "OPEN_FLAME_SETUP_SELECTOR="
  goto run_setup
)
echo Open-Flame setup requires an installed 64-bit CPython 3.12 or newer for Windows.
echo Follow README.md, or run setup_open_flame.py with an explicit supported Python executable.
echo No diagnostic was saved because a supported Python could not run.
if /i not "%~1"=="--help" if /i not "%~1"=="-h" pause
exit /b 2

:run_setup
"%OPEN_FLAME_SETUP_PYTHON%" %OPEN_FLAME_SETUP_SELECTOR% -I "%~dp0setup_open_flame.py" %*
set "OPEN_FLAME_SETUP_EXIT=%ERRORLEVEL%"
if "%OPEN_FLAME_SETUP_EXIT%"=="130" exit /b 130
if "%OPEN_FLAME_SETUP_EXIT%"=="0" (
  if "%~1"=="" (
    echo Setup command finished. Review the result above.
    pause
  )
  exit /b 0
)
echo Open-Flame setup stopped with an error. Keep the diagnostic above.
if /i not "%~1"=="--help" if /i not "%~1"=="-h" pause
exit /b %OPEN_FLAME_SETUP_EXIT%
