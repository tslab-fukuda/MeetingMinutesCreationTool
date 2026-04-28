@echo off
setlocal EnableExtensions
chcp 65001 >NUL

cd /d "%~dp0"

set "REMOTE=origin"
set "BASE_BRANCH=main"

if "%~1"=="" goto :usage

set "COMMIT_MESSAGE=%~1"

call :find_git
if errorlevel 1 goto :fail

for /f "delims=" %%B in ('"%GIT_CMD%" branch --show-current') do set "CURRENT_BRANCH=%%B"
if not defined CURRENT_BRANCH (
  echo [ERROR] Current branch could not be detected.
  goto :fail
)

echo ==========================================
echo Push Work Branch
echo ==========================================
echo Base   : %BASE_BRANCH%
echo Branch : %CURRENT_BRANCH%
echo.

echo [INFO] Fetching latest remote refs...
"%GIT_CMD%" fetch %REMOTE%
if errorlevel 1 goto :fail

if /I "%CURRENT_BRANCH%"=="%BASE_BRANCH%" (
  call :make_branch_name
  echo [INFO] Current branch is %BASE_BRANCH%; creating a work branch before committing.
  "%GIT_CMD%" pull --ff-only %REMOTE% %BASE_BRANCH%
  if errorlevel 1 goto :fail
  "%GIT_CMD%" switch -c "%CURRENT_BRANCH%"
  if errorlevel 1 goto :fail
)

call :has_changes
if defined HAS_CHANGES (
  echo [INFO] Staging local changes...
  "%GIT_CMD%" add -A
  if errorlevel 1 goto :fail

  echo [INFO] Creating commit...
  "%GIT_CMD%" commit -m "%COMMIT_MESSAGE%"
  if errorlevel 1 goto :fail
) else (
  echo [INFO] No local changes to commit. Existing branch commits will be pushed.
)

echo [INFO] Merging latest %REMOTE%/%BASE_BRANCH% into %CURRENT_BRANCH%...
"%GIT_CMD%" merge --no-edit "%REMOTE%/%BASE_BRANCH%"
if errorlevel 1 (
  echo [ERROR] Merge failed. Resolve conflicts, commit the result, then rerun this batch.
  goto :fail
)

echo [INFO] Pushing only the work branch to %REMOTE%...
"%GIT_CMD%" push -u %REMOTE% "%CURRENT_BRANCH%"
if errorlevel 1 (
  echo [ERROR] Push failed. Check GitHub permissions or branch settings.
  goto :fail
)

echo.
echo ==========================================
echo Work branch push completed
echo ==========================================
echo.
echo Branch pushed: %CURRENT_BRANCH%
echo Main was not pushed.
echo.
exit /b 0

:find_git
set "GIT_CMD="
where git >NUL 2>&1
if not errorlevel 1 (
  set "GIT_CMD=git"
  exit /b 0
)
if exist "C:\Program Files\Git\cmd\git.exe" (
  set "GIT_CMD=C:\Program Files\Git\cmd\git.exe"
  exit /b 0
)
echo [ERROR] git was not found. Install Git for Windows first.
exit /b 1

:make_branch_name
set "CURRENT_BRANCH=work_%RANDOM%%RANDOM%"
exit /b 0

:has_changes
set "HAS_CHANGES="
for /f "delims=" %%S in ('"%GIT_CMD%" status --porcelain') do set "HAS_CHANGES=1"
exit /b 0

:usage
echo Usage:
echo   push_branch.bat "commit message"
echo.
echo Example:
echo   push_branch.bat "Fix microphone device names"
exit /b 1

:fail
echo.
echo Work branch push did not finish successfully.
exit /b 1
