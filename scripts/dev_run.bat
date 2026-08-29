@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0dev_run.ps1" %*
set "UVMAPPING_EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %UVMAPPING_EXIT_CODE%

