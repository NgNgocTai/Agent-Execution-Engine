@echo off
setlocal

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0check_all.ps1" %*
exit /b %errorlevel%
