@echo off
REM Command Prompt entry point. Delegates to dev.ps1, which delegates to dev.sh.
REM -ExecutionPolicy Bypass because a default Windows install blocks .ps1 files.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0dev.ps1" %*
exit /b %ERRORLEVEL%
