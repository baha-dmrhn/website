@echo off
setlocal
set "NODE_HOME=%~dp0.tools\node-v24.18.0-win-x64"
if exist "%NODE_HOME%\node.exe" set "PATH=%NODE_HOME%;%PATH%"
where node >nul 2>nul
if errorlevel 1 (
  echo Node.js bulunamadi. Node.js LTS kurup yeniden deneyin.
  pause
  exit /b 1
)
set "NODE_USE_SYSTEM_CA=1"
cd /d "%~dp0"
call npm install || goto :error
if not exist android call npx cap add android || goto :error
call npx cap sync android || goto :error
echo.
echo Android projesi hazirlandi.
pause
exit /b 0
:error
echo.
echo Islem tamamlanamadi. Internet erisimini kontrol edin.
pause
exit /b 1
