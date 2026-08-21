@echo off
setlocal
title Setuora Master - Install or Update
set "SETUORA_SELF=%~f0"
net session >nul 2>&1
if not "%ERRORLEVEL%"=="0" (
  echo Administrator access is required. Opening the Windows security prompt...
  powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
echo Setuora Master - Windows install or update
echo.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "& { $ErrorActionPreference = 'Stop'; $self = $env:SETUORA_SELF; $lines = [IO.File]::ReadAllLines($self); $marker = [Array]::IndexOf($lines, '__SETUORA_PAYLOAD_BELOW__'); if ($marker -lt 0) { throw 'The Setuora installer payload is missing or damaged.' }; $parent = Join-Path $env:ProgramData 'Setuora'; $target = Join-Path $parent 'Setuora-Master-windows'; $launcher = Join-Path $target 'setuora.ps1'; $isUpdate = Test-Path (Join-Path $target '.env'); if ($isUpdate) { Write-Host 'Existing installation found. Stopping Setuora before updating...'; & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $launcher stop; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE } }; [IO.Directory]::CreateDirectory($parent) | Out-Null; $zip = Join-Path ([IO.Path]::GetTempPath()) ('setuora-' + [guid]::NewGuid().ToString('N') + '.zip'); try { $encoded = [string]::Concat($lines[($marker + 1)..($lines.Length - 1)]); [IO.File]::WriteAllBytes($zip, [Convert]::FromBase64String($encoded)); Expand-Archive -LiteralPath $zip -DestinationPath $parent -Force } finally { Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue }; Write-Host ('Application files installed in: ' + $target); if ($isUpdate) { & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $launcher preflight; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $launcher update; exit $LASTEXITCODE }; & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $launcher setup; exit $LASTEXITCODE }"
set "SETUORA_EXIT=%ERRORLEVEL%"
echo.
if "%SETUORA_EXIT%"=="0" (
  echo Setuora Master completed successfully.
) else (
  echo Setuora Master did not complete. Review the message above.
)
pause
exit /b %SETUORA_EXIT%
__SETUORA_PAYLOAD_BELOW__
