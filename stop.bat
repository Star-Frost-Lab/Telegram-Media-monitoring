@echo off
setlocal
cd /d "%~dp0"

if not exist _server.pid (
  echo Not running: no _server.pid in this folder.
  exit /b 0
)

set /p PID=<_server.pid
if "%PID%"=="" (
  echo _server.pid is empty. Deleting it.
  del _server.pid >nul 2>&1
  exit /b 0
)

taskkill /pid %PID% /f >nul 2>&1
if errorlevel 1 (
  echo Could not stop PID %PID% - it probably already exited.
) else (
  echo Stopped PID %PID%.
)
del _server.pid >nul 2>&1
