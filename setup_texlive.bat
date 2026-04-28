@echo off
setlocal EnableExtensions
chcp 65001 >NUL

cd /d "%~dp0"

set "TL_YEAR=2026"
set "TL_ROOT=C:\texlive\%TL_YEAR%"
set "TL_BIN=%TL_ROOT%\bin\windows"
set "TL_TMP=%CD%\.tmp_texlive"
set "TL_INSTALLER_ZIP=%TL_TMP%\install-tl.zip"
set "TL_INSTALLER_DIR="

echo ==========================================
echo TeX Live Setup
echo ==========================================
echo.

if exist "%TL_BIN%\latexmk.exe" if exist "%TL_BIN%\uplatex.exe" if exist "%TL_BIN%\dvipdfmx.exe" (
  echo [OK] TeX Live already appears to be installed:
  echo      %TL_BIN%
  goto :post_install
)

where curl >NUL 2>&1
if errorlevel 1 (
  echo [ERROR] curl was not found. Install curl or run on a standard Windows environment.
  goto :fail
)

where tar >NUL 2>&1
if errorlevel 1 (
  echo [ERROR] tar was not found. Install bsdtar/tar or run on a standard Windows environment.
  goto :fail
)

if not exist "%TL_TMP%" mkdir "%TL_TMP%"

echo [INFO] Downloading official TeX Live installer...
curl -L "https://mirror.ctan.org/systems/texlive/tlnet/install-tl.zip" -o "%TL_INSTALLER_ZIP%"
if errorlevel 1 (
  echo [ERROR] Failed to download TeX Live installer.
  goto :fail
)

echo [INFO] Extracting installer...
tar -xf "%TL_INSTALLER_ZIP%" -C "%TL_TMP%"
if errorlevel 1 (
  echo [ERROR] Failed to extract TeX Live installer.
  goto :fail
)

for /d %%D in ("%TL_TMP%\install-tl-*") do (
  set "TL_INSTALLER_DIR=%%~fD"
)

if not defined TL_INSTALLER_DIR (
  echo [ERROR] Could not locate extracted installer directory.
  goto :fail
)

echo [INFO] Running unattended TeX Live install...
call "%TL_INSTALLER_DIR%\install-tl-windows.bat" -profile "%CD%\texlive.profile"
if errorlevel 1 (
  echo [ERROR] TeX Live installation failed.
  goto :fail
)

if not exist "%TL_BIN%\tlmgr.bat" (
  echo [ERROR] tlmgr.bat was not found after TeX Live installation.
  goto :fail
)

echo [INFO] Installing required Japanese TeX packages...
call "%TL_BIN%\tlmgr.bat" install latexmk collection-langjapanese enumitem jsclasses
if errorlevel 1 (
  echo [WARN] tlmgr package installation reported an error.
)

:post_install
if not exist "%TL_BIN%\latexmk.exe" (
  echo [ERROR] latexmk.exe was not found after package installation.
  goto :fail
)

echo [INFO] Refreshing TeX Live formats...
call "%TL_BIN%\fmtutil-sys.exe" --all
if errorlevel 1 (
  echo [WARN] fmtutil-sys reported an error.
)

echo.
echo [INFO] Checking installed TeX commands...
call :check_command "%TL_BIN%\latexmk.exe"
call :check_command "%TL_BIN%\uplatex.exe"
call :check_command "%TL_BIN%\dvipdfmx.exe"

echo.
echo ==========================================
echo TeX Live setup completed
echo ==========================================
echo.
echo If this terminal does not see the commands yet, open a new terminal.
echo The web app can also detect TeX Live under:
echo   %TL_BIN%
echo.
echo Temporary installer files were stored under:
echo   %TL_TMP%
echo You can delete that folder after setup if you want.
echo.
exit /b 0

:check_command
if exist %1 (
  echo [OK] %~nx1
  exit /b 0
)
echo [WARN] %~nx1 was not found.
exit /b 0

:fail
echo.
echo TeX Live setup did not finish successfully.
exit /b 1
