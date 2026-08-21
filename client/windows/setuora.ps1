[CmdletBinding()]
param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet("setup", "preflight", "start", "stop", "status", "logs", "update", "sftp-install", "sftp-add")]
    [string]$Command,

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$RemainingArguments
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

if ($Command -eq "sftp-install" -or $Command -eq "sftp-add") {
    $script = Join-Path $PSScriptRoot "scripts\windows\configure-sftp.ps1"
    if ($Command -eq "sftp-install") {
        & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $script -Action Install
    } else {
        if (-not $RemainingArguments -or $RemainingArguments.Count -ne 1) {
            throw "Usage: setuora.ps1 sftp-add FRANCHISE-CODE"
        }
        & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $script `
            -Action AddFranchise -FranchiseCode $RemainingArguments[0]
    }
    exit $LASTEXITCODE
}

function Get-SetuoraPython {
    $launchers = @(
        @{ Name = "py"; Prefix = @("-3") },
        @{ Name = "python"; Prefix = @() },
        @{ Name = "python3"; Prefix = @() }
    )

    foreach ($launcher in $launchers) {
        $name = [string]$launcher["Name"]
        $prefix = [string[]]$launcher["Prefix"]
        if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
            continue
        }

        & $name @prefix -c `
            "import sys; raise SystemExit(sys.version_info < (3, 11))" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return @{ Name = $name; Prefix = $prefix }
        }
    }

    throw @"
Python 3.11 or newer is required.
Install it from https://www.python.org/downloads/windows/ and select
'Add python.exe to PATH', then run this launcher again.
"@
}

$python = Get-SetuoraPython
$pythonName = [string]$python["Name"]
$arguments = @($python["Prefix"]) + @("$PSScriptRoot\deploy.py", $Command)
if ($RemainingArguments) {
    $arguments += $RemainingArguments
}

& $pythonName @arguments
exit $LASTEXITCODE
