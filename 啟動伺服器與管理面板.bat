@echo off
setlocal EnableExtensions
title Palworld Caretaker
set "ROOT=%~dp0"
set "CONFIG_DIR=%ROOT%config"
set "PORT=8765"

:: A release archive intentionally contains only safe .example files. Make a
:: private working copy on the first double-click so the Web setup wizard can
:: finish provisioning without asking a new user to run shell commands.
if not exist "%CONFIG_DIR%\" mkdir "%CONFIG_DIR%" >nul 2>&1
if not exist "%CONFIG_DIR%\" (
  echo [ERROR] Could not create %CONFIG_DIR%
  pause
  exit /b 1
)
for %%F in (caretaker.env server.env secrets.env) do (
  if not exist "%CONFIG_DIR%\%%F" (
    if not exist "%CONFIG_DIR%\%%F.example" (
      echo [ERROR] Missing %CONFIG_DIR%\%%F.example
      pause
      exit /b 1
    )
    echo Creating config\%%F from its safe example...
    copy /Y "%CONFIG_DIR%\%%F.example" "%CONFIG_DIR%\%%F" >nul
    if errorlevel 1 (
      echo [ERROR] Could not create %CONFIG_DIR%\%%F
      pause
      exit /b 1
    )
  )
)

:: The first-run wizard persists only within this manager-owned child layer.
if not exist "%CONFIG_DIR%\editable\" mkdir "%CONFIG_DIR%\editable" >nul 2>&1
if not exist "%CONFIG_DIR%\editable\" (
  echo [ERROR] Could not create %CONFIG_DIR%\editable
  pause
  exit /b 1
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

:: Fresh release folders do not have an editable install. Bootstrap it here;
:: this is local-only and does not require the user to understand pip.
%PYTHON% -c "import palworld_caretaker" >nul 2>&1
if errorlevel 1 (
  echo Installing Palworld Caretaker for this Python 3 environment...
  %PYTHON% -m pip install -e "%ROOT%"
  if errorlevel 1 (
    echo [ERROR] Palworld Caretaker could not be installed automatically.
    pause
    exit /b 1
  )
)

:: Starting a second panel is harmless; an existing listener will win. Start
:: it before the service request so first-run configuration is still reachable
:: when a game service needs to be installed or repaired.
start "" /b %PYTHON% -m palworld_caretaker.web --config-dir "%CONFIG_DIR%" --port %PORT%
for /L %%I in (1,1,30) do (
  powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing http://127.0.0.1:%PORT%/healthz -TimeoutSec 2).StatusCode -eq 200 } catch { $false }" | findstr /I "True" >nul
  if not errorlevel 1 goto :panel_ready
  timeout /t 1 /nobreak >nul
)
echo [ERROR] The management panel did not respond within 30 seconds.
echo Check Python and the configuration, then retry.
pause
exit /b 1

:panel_ready
:: The copied template has a local-only initial panel password. Supplying it
:: here avoids an unexplained browser credential prompt before the wizard.
start "" "http://palworld-manager:CHANGE_ME_ADMIN_PASSWORD@127.0.0.1:%PORT%/"

:: The browser-facing panel remains the invoking account. Elevate only the
:: fixed service-control script, after the setup UI is already available.
if exist "%ROOT%scripts\windows\palworld-service.ps1" (
  set "CARETAKER_SERVICE_SCRIPT=%ROOT%scripts\windows\palworld-service.ps1"
  set "CARETAKER_SERVICE_CONFIG_DIR=%CONFIG_DIR%"
  echo Requesting administrator permission to start Palworld...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',$env:CARETAKER_SERVICE_SCRIPT,'-Action','start','-ConfigDir',$env:CARETAKER_SERVICE_CONFIG_DIR) -Verb RunAs -Wait -PassThru; exit $p.ExitCode"
  if errorlevel 1 (
    echo [WARNING] The management panel is running, but PalServer did not start.
    echo Complete the first-run wizard, then check the Windows service name and service log.
  )
)
exit /b 0
