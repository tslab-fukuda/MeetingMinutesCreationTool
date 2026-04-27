@echo off
setlocal EnableExtensions
chcp 65001 >NUL

cd /d "%~dp0"

echo ==========================================
echo MeetingMinutesCreationTool Setup
echo ==========================================
echo.

call :find_python
if errorlevel 1 (
  echo [INFO] Python 3.11+ was not found.
  where winget >NUL 2>&1
  if errorlevel 1 (
    echo [ERROR] winget was not found. Please install Python 3.11 or newer manually.
    goto :fail
  )

  echo [INFO] Installing Python 3.11 with winget...
  winget install --id Python.Python.3.11 -e --accept-package-agreements --accept-source-agreements
  if errorlevel 1 (
    echo [ERROR] Python installation failed.
    goto :fail
  )

  call :find_python
  if errorlevel 1 (
    echo [ERROR] Python is still not available in this shell.
    echo         Open a new terminal and run this batch file again.
    goto :fail
  )
)

echo [OK] Python command: %PYTHON_CMD%

if not exist ".venv\Scripts\python.exe" (
  echo [INFO] Creating virtual environment...
  %PYTHON_CMD% -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Failed to create virtual environment.
    goto :fail
  )
) else (
  echo [OK] Existing virtual environment found.
)

echo [INFO] Upgrading pip...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 (
  echo [ERROR] Failed to upgrade pip.
  goto :fail
)

echo [INFO] Installing Python dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo [ERROR] Failed to install requirements.txt.
  goto :fail
)

if not exist "recordings" mkdir recordings
if not exist "transcripts" mkdir transcripts

echo.
echo [INFO] Checking external commands...
call :check_command latexmk "TeX compile"
call :check_command uplatex "Japanese TeX compile"
call :check_command dvipdfmx "DVI to PDF conversion"

set "NEED_TEX_SETUP="
where latexmk >NUL 2>&1
if errorlevel 1 set "NEED_TEX_SETUP=1"
where uplatex >NUL 2>&1
if errorlevel 1 set "NEED_TEX_SETUP=1"
where dvipdfmx >NUL 2>&1
if errorlevel 1 set "NEED_TEX_SETUP=1"

if defined NEED_TEX_SETUP (
  echo.
  echo [INFO] TeX commands were not fully available.
  echo [INFO] Running setup_texlive.bat...
  call "%~dp0setup_texlive.bat"
  if errorlevel 1 (
    echo [ERROR] setup_texlive.bat did not finish successfully.
    goto :fail
  )
)

echo.
echo ==========================================
echo Setup completed
echo ==========================================
echo.
echo Start the web UI with:
echo   run_webapp.bat
echo.
echo Or run manually:
echo   .venv\Scripts\python.exe -m uvicorn webapp.server:app --reload
echo.
echo Then open:
echo   http://127.0.0.1:8000
echo.
exit /b 0

:find_python
set "PYTHON_CMD="
py -3.11 -V >NUL 2>&1
if not errorlevel 1 (
  set "PYTHON_CMD=py -3.11"
  exit /b 0
)

python -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)" >NUL 2>&1
if not errorlevel 1 (
  set "PYTHON_CMD=python"
  exit /b 0
)

exit /b 1

:check_command
where %~1 >NUL 2>&1
if errorlevel 1 (
  echo [WARN] %~1 was not found. %~2 may not work.
  exit /b 0
)
echo [OK] %~1
exit /b 0

:fail
echo.
echo Setup did not finish successfully.
exit /b 1
