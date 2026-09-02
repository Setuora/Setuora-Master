@echo off
setlocal
title Setuora Master - Windows
set "SETUORA_DIST=%~dp0dist"
set "SETUORA_INSTALLER="

for /f "delims=" %%F in ('dir /b /a-d /o:-d "%SETUORA_DIST%\Setuora-Master-*-windows.cmd" 2^>nul') do (
    set "SETUORA_INSTALLER=%SETUORA_DIST%\%%F"
    goto found
)

:found
if not defined SETUORA_INSTALLER (
    echo No Windows Setuora installer was found in:
    echo %SETUORA_DIST%
    echo.
    echo Build one with:
    echo python scripts\build_client_packages.py --version VERSION
    pause
    exit /b 1
)

echo Launching: %SETUORA_INSTALLER%
call "%SETUORA_INSTALLER%"
exit /b %ERRORLEVEL%
