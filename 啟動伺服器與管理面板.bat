@echo off
setlocal EnableExtensions
title Palworld Caretaker
set "ROOT=%~dp0"
set "CONFIG_DIR=%ROOT%config"
set "PORT=8765"

if not exist "%CONFIG_DIR%\caretaker.env" (
  echo [ERROR] Missing %CONFIG_DIR%\caretaker.env
  echo Copy the example configuration, then run this file again.
  pause
  exit /b 1
)

:: The browser-facing panel must remain the invoking (non-administrator)
:: account.  Elevate only the fixed service-control script.  Passing both
:: paths through the inherited environment keeps an arbitrary batch filename
:: out of the PowerShell command text and out of a quoted ArgumentList.
if not exist "%CONFIG_DIR%\server.env" (
  echo [ERROR] Missing %CONFIG_DIR%\server.env
  pause
  exit /b 1
)

:: The first-run wizard persists only within this manager-owned child layer.
:: Create it before either the PowerShell or Python loaders are started.
if not exist "%CONFIG_DIR%\editable\" mkdir "%CONFIG_DIR%\editable" >nul 2>&1
if not exist "%CONFIG_DIR%\editable\" (
  echo [ERROR] Could not create %CONFIG_DIR%\editable
  pause
  exit /b 1
)

:: The browser-facing panel remains the invoking (non-administrator) account.
:: Elevate only the fixed service-control script, with paths passed through the
:: inherited environment rather than inserted into the PowerShell command.
if exist "%ROOT%scripts\windows\palworld-service.ps1" (
  set "CARETAKER_SERVICE_SCRIPT=%ROOT%scripts\windows\palworld-service.ps1"
  set "CARETAKER_SERVICE_CONFIG_DIR=%CONFIG_DIR%"
  echo Requesting administrator permission to start Palworld...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',$env:CARETAKER_SERVICE_SCRIPT,'-Action','start','-ConfigDir',$env:CARETAKER_SERVICE_CONFIG_DIR) -Verb RunAs -Wait -PassThru; exit $p.ExitCode"
  if errorlevel 1 (
    echo [ERROR] PalServer could not be started. Check the service name and Windows service log.
    pause
    exit /b 1
  )
)

set "PYTHON="
where py >nul 2>&1 && set "PYTHON=py -3"
if not defined PYTHON (
  where python >nul 2>&1 && set "PYTHON=python"
)
if not defined PYTHON (
  echo [ERROR] Python 3 was not found. Install Python 3.10 or later, then retry.
  pause
  exit /b 1
)
%PYTHON% -c "import palworld_caretaker" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Palworld Caretaker is not installed in this Python environment.
  echo Run: python -m pip install .
  pause
  exit /b 1
)

:: Starting a second panel is harmless; an existing listener will win and the
:: health loop below opens that one instead.
start "" /b %PYTHON% -m palworld_caretaker.web --config-dir "%CONFIG_DIR%" --port %PORT%
for /L %%I in (1,1,30) do (
  powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing http://127.0.0.1:%PORT%/healthz -TimeoutSec 2).StatusCode -eq 200 } catch { $false }" | findstr /I "True" >nul
  if not errorlevel 1 (
    start "" "http://127.0.0.1:%PORT%/"
    exit /b 0
  )
  timeout /t 1 /nobreak >nul
)
echo [ERROR] The management panel did not respond within 30 seconds.
echo Check Python and the configuration, then retry.
pause
exit /b 1
