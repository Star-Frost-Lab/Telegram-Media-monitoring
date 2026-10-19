@echo off
rem Fire and forget: server.py runs hidden (pythonw) and opens the page itself once
rem it is listening. Closing any window does not stop it. To stop it, double-click stop.bat.
rem Logs go to _run.log. For another port, call server.py directly instead.
cd /d "%~dp0"
curl -s -o nul --noproxy 127.0.0.1 --max-time 2 http://127.0.0.1:8787/api/status
if errorlevel 1 (start "" pythonw.exe -X utf8 server.py) else (start "" http://127.0.0.1:8787/)
exit /b 0
