@echo off
setlocal EnableExtensions
chcp 65001 >NUL

cd /d "%~dp0"

set "REMOTE=origin"
set "BASE_BRANCH=main"

if "%~1"=="" goto :usage

set "COMMIT_MESSAGE=%~1"
set "PR_TITLE=%~2"
if not defined PR_TITLE set "PR_TITLE=%COMMIT_MESSAGE%"

call :find_git
if errorlevel 1 goto :fail

for /f "delims=" %%U in ('"%GIT_CMD%" remote get-url %REMOTE%') do set "ORIGIN_URL=%%U"
if not defined ORIGIN_URL (
  echo [ERROR] Remote '%REMOTE%' was not found.
  goto :fail
)

call :resolve_repo_slug
if not defined REPO_SLUG (
  echo [ERROR] Could not resolve GitHub repository from origin URL:
  echo         %ORIGIN_URL%
  goto :fail
)

for /f "delims=" %%B in ('"%GIT_CMD%" branch --show-current') do set "CURRENT_BRANCH=%%B"
if not defined CURRENT_BRANCH (
  echo [ERROR] Current branch could not be detected.
  goto :fail
)

echo ==========================================
echo Publish Pull Request
echo ==========================================
echo Repository : %REPO_SLUG%
echo Base       : %BASE_BRANCH%
echo Branch     : %CURRENT_BRANCH%
echo.

echo [INFO] Fetching latest remote refs...
"%GIT_CMD%" fetch %REMOTE%
if errorlevel 1 goto :fail

if /I "%CURRENT_BRANCH%"=="%BASE_BRANCH%" (
  call :make_branch_name
  echo [INFO] Current branch is %BASE_BRANCH%; updating it before creating a work branch...
  "%GIT_CMD%" pull --ff-only %REMOTE% %BASE_BRANCH%
  if errorlevel 1 goto :fail
  echo [INFO] Creating work branch: %CURRENT_BRANCH%
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
  echo [INFO] No local changes to commit. Existing branch commits will be used.
)

echo [INFO] Merging latest %REMOTE%/%BASE_BRANCH% into %CURRENT_BRANCH%...
"%GIT_CMD%" merge --no-edit "%REMOTE%/%BASE_BRANCH%"
if errorlevel 1 (
  echo [ERROR] Merge failed. Resolve conflicts, commit the result, then rerun this batch.
  goto :fail
)

echo [INFO] Pushing branch to %REMOTE%...
"%GIT_CMD%" push -u %REMOTE% "%CURRENT_BRANCH%"
if errorlevel 1 (
  echo [ERROR] Push failed. Check GitHub permissions or fork settings.
  goto :fail
)

call :write_pr_body
call :create_pr
if errorlevel 1 goto :fail

echo.
echo ==========================================
echo Pull request flow completed
echo ==========================================
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

:resolve_repo_slug
set "REPO_SLUG=%ORIGIN_URL%"
set "REPO_SLUG=%REPO_SLUG:https://github.com/=%"
set "REPO_SLUG=%REPO_SLUG:git@github.com:=%"
set "REPO_SLUG=%REPO_SLUG:.git=%"
if "%REPO_SLUG%"=="%ORIGIN_URL%" set "REPO_SLUG="
exit /b 0

:make_branch_name
set "CURRENT_BRANCH=work_%RANDOM%%RANDOM%"
exit /b 0

:has_changes
set "HAS_CHANGES="
for /f "delims=" %%S in ('"%GIT_CMD%" status --porcelain') do set "HAS_CHANGES=1"
exit /b 0

:write_pr_body
set "PR_BODY_FILE=%TEMP%\MeetingMinutesCreationTool_pr_body_%RANDOM%.md"
(
  echo ## 概要
  echo - 変更内容をコミットし、%REMOTE%/%BASE_BRANCH% を取り込んだうえで PR を作成します
  echo - 作業ブランチ: %CURRENT_BRANCH%
  echo.
  echo ## 確認
  echo - 必要な確認内容を PR 上で追記してください
) > "%PR_BODY_FILE%"
exit /b 0

:create_pr
where gh >NUL 2>&1
if not errorlevel 1 (
  echo [INFO] Checking existing pull request with GitHub CLI...
  gh pr view "%CURRENT_BRANCH%" --repo "%REPO_SLUG%" --json url --jq ".url"
  if not errorlevel 1 exit /b 0

  echo [INFO] Creating pull request with GitHub CLI...
  gh pr create --repo "%REPO_SLUG%" --base "%BASE_BRANCH%" --head "%CURRENT_BRANCH%" --title "%PR_TITLE%" --body-file "%PR_BODY_FILE%"
  if not errorlevel 1 exit /b 0
  echo [WARN] GitHub CLI PR creation failed. Falling back to GitHub API...
)

call :create_pr_with_api
exit /b %ERRORLEVEL%

:create_pr_with_api
set "CRED_REQ=%TEMP%\MeetingMinutesCreationTool_git_cred_%RANDOM%.txt"
set "CRED_OUT=%TEMP%\MeetingMinutesCreationTool_git_cred_out_%RANDOM%.txt"
(
  echo protocol=https
  echo host=github.com
  echo.
) > "%CRED_REQ%"

set "GITHUB_TOKEN="
"%GIT_CMD%" credential fill < "%CRED_REQ%" > "%CRED_OUT%"
if errorlevel 1 (
  del "%CRED_REQ%" >NUL 2>&1
  del "%CRED_OUT%" >NUL 2>&1
  echo [ERROR] Failed to read GitHub credential from Git Credential Manager.
  exit /b 1
)

for /f "usebackq tokens=1,* delims==" %%A in ("%CRED_OUT%") do (
  if "%%A"=="password" set "GITHUB_TOKEN=%%B"
)
del "%CRED_REQ%" >NUL 2>&1
del "%CRED_OUT%" >NUL 2>&1

if not defined GITHUB_TOKEN (
  echo [ERROR] GitHub credential was not found.
  echo         Run 'gh auth login' or push once with Git Credential Manager, then rerun.
  exit /b 1
)

if exist "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" (
  set "POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
) else (
  set "POWERSHELL=powershell"
)

echo [INFO] Creating pull request with GitHub API...
"%POWERSHELL%" -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $headers=@{ Authorization=('Bearer ' + $env:GITHUB_TOKEN); Accept='application/vnd.github+json'; 'X-GitHub-Api-Version'='2022-11-28'; 'User-Agent'='publish-pr-bat' }; $owner=$env:REPO_SLUG.Split('/')[0]; $head=[Uri]::EscapeDataString($owner + ':' + $env:CURRENT_BRANCH); $existing=Invoke-RestMethod -Method Get -Uri ('https://api.github.com/repos/' + $env:REPO_SLUG + '/pulls?head=' + $head + '&state=open') -Headers $headers; if ($existing.Count -gt 0) { Write-Output $existing[0].html_url; exit 0 }; $body=@{ title=$env:PR_TITLE; head=$env:CURRENT_BRANCH; base=$env:BASE_BRANCH; body=[IO.File]::ReadAllText($env:PR_BODY_FILE,[Text.Encoding]::UTF8) } | ConvertTo-Json; $res=Invoke-RestMethod -Method Post -Uri ('https://api.github.com/repos/' + $env:REPO_SLUG + '/pulls') -Headers $headers -Body $body -ContentType 'application/json; charset=utf-8'; Write-Output $res.html_url"
if errorlevel 1 (
  echo [ERROR] Pull request creation failed.
  echo         You can create it manually from:
  echo         https://github.com/%REPO_SLUG%/pull/new/%CURRENT_BRANCH%
  exit /b 1
)
exit /b 0

:usage
echo Usage:
echo   publish_pr.bat "commit message" ["pull request title"]
echo.
echo Example:
echo   publish_pr.bat "Add setup automation" "環境構築バッチを追加"
exit /b 1

:fail
echo.
echo Publish pull request flow did not finish successfully.
exit /b 1
