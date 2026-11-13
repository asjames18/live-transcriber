@echo off
setlocal
rem Live Transcriber uninstaller. Your data is kept unless you choose to delete it.
if exist "%LOCALAPPDATA%\LiveTranscriber\Uninstall-LiveTranscriber.ps1" if "%~1"=="" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%LOCALAPPDATA%\LiveTranscriber\Uninstall-LiveTranscriber.ps1"
  exit /b %errorlevel%
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\Uninstall-LiveTranscriber.ps1" %*
exit /b %errorlevel%
