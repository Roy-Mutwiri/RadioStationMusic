@echo off
rem Trade Fix Radio - launch the station in its own window (console stays open for logs)
cd /d "%~dp0"
".venv\Scripts\python.exe" "desktop\app.py" %*
