@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Palworld Caretaker
set "PYTHONPATH=%~dp0src"
where py >nul 2>&1
if not errorlevel 1 (
  py -3 -m palworld_caretaker.windows_launcher
) else (
  python -m palworld_caretaker.windows_launcher
)
if errorlevel 1 (
  echo.
  echo Setup failed. Read the error above, then double-click to retry.
  pause
  exit /b 1
)
exit /b 0