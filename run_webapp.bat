@echo off
setlocal EnableExtensions
chcp 65001 >NUL

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Virtual environment was not found.
  echo         Run setup_windows.bat first.
  exit /b 1
)

echo Starting MeetingMinutesCreationTool Web UI...
".venv\Scripts\python.exe" -m uvicorn webapp.server:app --reload
