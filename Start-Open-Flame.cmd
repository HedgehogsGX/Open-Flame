@echo off
setlocal DisableDelayedExpansion
set "OPEN_FLAME_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%OPEN_FLAME_PYTHON%" (
  echo Open-Flame could not start: the local Python environment is missing.
  echo Run Setup-Open-Flame.cmd or follow README.md, then try again.
  echo No diagnostic was saved because Python could not run.
  pause
  exit /b 2
)
"%OPEN_FLAME_PYTHON%" -I "%~dp0start_open_flame.py" %*
set "OPEN_FLAME_EXIT=%ERRORLEVEL%"
if not "%OPEN_FLAME_EXIT%"=="0" if not "%OPEN_FLAME_EXIT%"=="130" (
  echo Open-Flame stopped with an error. Keep the diagnostic above.
  echo Saved diagnostics, when available: %%LOCALAPPDATA%%\Open-Flame\diagnostics
  pause
)
exit /b %OPEN_FLAME_EXIT%
