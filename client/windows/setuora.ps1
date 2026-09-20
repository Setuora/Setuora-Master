[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("menu", "setup", "preflight", "start", "stop", "status", "open", "logs", "update", "update-runtime", "uninstall", "help", "sftp-install", "sftp-add")]
    [string]$Command = "menu",
    [switch]$Elevated,
    [switch]$PauseAfter,
    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$RemainingArguments
)

$ErrorActionPreference = "Stop"
$ApplicationRoot = $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $ApplicationRoot "deploy.py"))) {
    $ApplicationRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
}
$ControllerPath = $PSCommandPath
$ProductName = "Setuora Master"
$portFile = Join-Path $ApplicationRoot 'runtime-port.txt'
$browserPort = '8000'
if (Test-Path -LiteralPath $portFile) {
    $candidate = (Get-Content -LiteralPath $portFile -TotalCount 1).Trim()
    if ($candidate -notmatch '^[0-9]{4,5}$' -or [int]$candidate -lt 1024 -or [int]$candidate -gt 65535) {
        throw "The local browser port file is invalid. Run Setup / repair."
    }
    $browserPort = $candidate
}
$BrowserUrl = "http://127.0.0.1:$browserPort"
Set-Location -LiteralPath $ApplicationRoot

function Test-SetuoraAdministrator {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-SetuoraNative([string]$File, [string[]]$Arguments) {
    # Keep native stderr visible without cutting off the program's diagnostic output.
    $nativePreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $File @Arguments | Out-Host
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $nativePreference
    }
    return [int]$code
}

function Get-SetuoraPython {
    $launchers = @(
        @{ Name = "$ApplicationRoot\.venv\Scripts\python.exe"; Prefix = @() },
        @{ Name = "py"; Prefix = @("-3.11") },
        @{ Name = "py"; Prefix = @("-3") },
        @{ Name = "python"; Prefix = @() },
        @{ Name = "python3"; Prefix = @() },
        @{ Name = "$env:ProgramFiles\Python313\python.exe"; Prefix = @() },
        @{ Name = "$env:ProgramFiles\Python312\python.exe"; Prefix = @() },
        @{ Name = "$env:ProgramFiles\Python311\python.exe"; Prefix = @() }
    )
    foreach ($launcher in $launchers) {
        $name = [string]$launcher["Name"]
        $prefix = [string[]]$launcher["Prefix"]
        if (-not (Get-Command $name -ErrorAction SilentlyContinue)) { continue }
        # A missing launcher version should try the next one on PowerShell 5.1 too.
        $probePreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            & $name @prefix -c "import sys; raise SystemExit(sys.version_info < (3, 11))" 2>$null
            $supported = $LASTEXITCODE -eq 0
        } finally {
            $ErrorActionPreference = $probePreference
        }
        if ($supported) { return @{ Name = $name; Prefix = $prefix } }
    }
    return $null
}

function Install-SetuoraPython {
    if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq "ARM64") {
        throw "An x64 Windows 10 or 11 computer is required by the current locked runtime."
    }
    $winget = Get-Command "winget.exe" -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host "Installing Python through Windows Package Manager..." -ForegroundColor Cyan
        $code = Invoke-SetuoraNative $winget.Source @(
            "install", "--id", "Python.Python.3.13", "--exact", "--source", "winget",
            "--scope", "machine", "--accept-package-agreements", "--accept-source-agreements",
            "--disable-interactivity"
        )
        if ($code -eq 3010) { throw "Python installed. Restart Windows, then run Setup / repair again." }
        if ($code -eq 0 -and (Get-SetuoraPython)) { return }
        Write-Host "Windows Package Manager did not provide Python. Trying the signed python.org installer..." -ForegroundColor Yellow
    }

    # Keep this fallback on an actively supported Python release with Windows installers.
    # The runtime accepts Python 3.11+ and the locked wheels support Python 3.13.
    $architecture = "amd64"
    $version = "3.13.15"
    $url = "https://www.python.org/ftp/python/$version/python-$version-$architecture.exe"
    $installer = Join-Path ([IO.Path]::GetTempPath()) ("setuora-python-" + [guid]::NewGuid().ToString("N") + ".exe")
    try {
        Write-Host "Downloading the signed Python installer from python.org..." -ForegroundColor Cyan
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing
        $signature = Get-AuthenticodeSignature -LiteralPath $installer
        if ($signature.Status -ne [Management.Automation.SignatureStatus]::Valid -or
            $signature.SignerCertificate.Subject -notmatch "Python Software Foundation") {
            throw "The downloaded Python installer does not have a valid Python Software Foundation signature."
        }
        $process = Start-Process -FilePath $installer -ArgumentList @(
            "/quiet", "InstallAllUsers=1", "PrependPath=1", "Include_test=0"
        ) -Wait -PassThru -WindowStyle Hidden
        if ($process.ExitCode -notin @(0, 3010)) { throw "Python installation failed (exit $($process.ExitCode))." }
        if ($process.ExitCode -eq 3010) { throw "Python installed. Restart Windows, then run Setup / repair again." }
    } finally {
        Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
    }
    if (-not (Get-SetuoraPython)) { throw "Python installed but was not found. Restart Windows and run Setup / repair again." }
}

function Invoke-SetuoraDeployment([string]$Action, [string[]]$ExtraArguments = @()) {
    $python = Get-SetuoraPython
    if (-not $python -and $Action -eq "setup") {
        Install-SetuoraPython
        $python = Get-SetuoraPython
    }
    if (-not $python) { throw "Python 3.11 or newer was not found. Choose Setup / repair first." }
    $arguments = @($python["Prefix"]) + @((Join-Path $ApplicationRoot "deploy.py"), $Action) + $ExtraArguments
    return Invoke-SetuoraNative ([string]$python["Name"]) $arguments
}

function Invoke-SetuoraElevated([string]$Action, [string[]]$ExtraArguments = @()) {
    if ($Elevated) { throw "Administrator access was not granted. Right-click setuora.bat and choose Run as administrator." }
    Write-Host "Approve the Windows security prompt. Complete this action in the new Administrator window." -ForegroundColor Cyan
    # Keep the child console interactive: setup asks for the first administrator password.
    $arguments = '-NoLogo -NoProfile -ExecutionPolicy Bypass -File "' + $ControllerPath + '" ' + $Action + ' -Elevated -PauseAfter'
    foreach ($argument in $ExtraArguments) {
        if ($argument -notmatch '^[A-Za-z0-9_-]+$') { throw "Unsupported elevated command argument." }
        $arguments += ' ' + $argument
    }
    try {
        $process = Start-Process -FilePath "powershell.exe" -ArgumentList $arguments -Verb RunAs -Wait -PassThru
        return [int]$process.ExitCode
    } catch {
        Write-Host "Administrator approval was cancelled or the window could not be opened. No action was completed." -ForegroundColor Yellow
        return 1
    }
}

function Read-SetuoraGit([string[]]$Arguments) {
    $gitPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = & git.exe -C $ApplicationRoot @Arguments 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $gitPreference
    }
    if ($code -ne 0) { throw ("Git failed (exit $code): " + ($output -join [Environment]::NewLine)) }
    return ($output -join [Environment]::NewLine).Trim()
}

function Backup-SetuoraSource {
    $python = Join-Path $ApplicationRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        throw 'The installed Python runtime is missing. Choose Setup / repair before updating.'
    }
    Write-Host 'Creating a verified SQLite backup before updating...' -ForegroundColor Cyan
    $code = Invoke-SetuoraNative $python @(
        '-c', 'from app.services.backup import create_scheduled_backup; print(create_scheduled_backup().path)'
    )
    if ($code -ne 0) { throw 'The pre-update database backup failed. Master was left running.' }
}

function Update-SetuoraSource {
    if (-not (Get-Command "git.exe" -ErrorAction SilentlyContinue)) { throw "Git for Windows is required to update this source checkout." }
    $code = Invoke-SetuoraDeployment "preflight"
    if ($code -ne 0) { return $code }
    $null = Read-SetuoraGit @("rev-parse", "--is-inside-work-tree")
    if (Read-SetuoraGit @("status", "--porcelain", "--untracked-files=all")) {
        throw "The source checkout has local changes. Save or commit them before updating. Nothing has been stopped or overwritten."
    }
    $branch = Read-SetuoraGit @("branch", "--show-current")
    if (-not $branch) { throw "Check out a Git branch before updating; detached HEAD cannot be updated automatically." }
    Write-Host "Checking origin/$branch for updates..." -ForegroundColor Cyan
    $null = Read-SetuoraGit @("fetch", "--quiet", "origin")
    $null = Read-SetuoraGit @("rev-parse", "--verify", "refs/remotes/origin/$branch")
    $null = Read-SetuoraGit @("merge-base", "--is-ancestor", "HEAD", "origin/$branch")
    Backup-SetuoraSource
    $code = Invoke-SetuoraDeployment "stop"
    if ($code -ne 0) { return $code }
    try {
        $null = Read-SetuoraGit @("merge", "--ff-only", "origin/$branch")
    } catch {
        Write-Host "The source update failed. Attempting to restart the previous installation..." -ForegroundColor Yellow
        $null = Invoke-SetuoraDeployment "start"
        throw
    }
    return Invoke-SetuoraDeployment "update"
}

function Install-SetuoraUpdate {
    Add-Type -AssemblyName System.Windows.Forms
    $picker = New-Object System.Windows.Forms.OpenFileDialog
    $picker.Title = "Choose the downloaded $ProductName installer"
    $picker.Filter = "$ProductName installer (Setuora-Master-*-windows.cmd)|Setuora-Master-*-windows.cmd"
    $picker.CheckFileExists = $true
    try {
        if ($picker.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
            Write-Host "Update cancelled. The running application was left unchanged."
            return 0
        }
        $installer = $picker.FileName
    } finally {
        $picker.Dispose()
    }
    if ([IO.Path]::GetFileName($installer) -notmatch '^Setuora-Master-[A-Za-z0-9._-]+-windows\.cmd$') {
        throw "Choose an official $ProductName Windows installer for this edition."
    }
    $process = Start-Process -FilePath $installer -Wait -PassThru
    return [int]$process.ExitCode
}

function Show-SetuoraHelp {
    Write-Host "$ProductName controls"
    Write-Host "Double-click setuora.bat to open the menu."
    Write-Host "Commands: setup, start, stop, status, open, logs, preflight, update, uninstall, help"
    Write-Host "Setup, Start, Stop, Logs, Check configuration, Update and Uninstall request Administrator access."
    Write-Host "Source updates use Git. Installed copies ask you to choose a downloaded installer."
    Write-Host "Browser: $BrowserUrl"
}

function Invoke-SetuoraCommand([string]$Action, [string[]]$ExtraArguments = @()) {
    if ($Action -in @("setup", "start", "stop", "preflight", "update", "update-runtime", "uninstall", "logs", "sftp-install", "sftp-add")) {
        if (-not (Test-SetuoraAdministrator)) { return Invoke-SetuoraElevated $Action $ExtraArguments }
    }
    switch ($Action) {
        "help" { Show-SetuoraHelp; return 0 }
        "open" {
            $code = Invoke-SetuoraDeployment "status"
            if ($code -ne 0) { return $code }
            Start-Process -FilePath $BrowserUrl
            return 0
        }
        "update" {
            if (Test-Path -LiteralPath (Join-Path $ApplicationRoot ".git")) { return Update-SetuoraSource }
            return Install-SetuoraUpdate
        }
        "update-runtime" { return Invoke-SetuoraDeployment "update" }
        "uninstall" {
            # The deferred cleanup waits for this controller to exit and cannot
            # remove a directory that remains the controller's working directory.
            Set-Location -LiteralPath (Join-Path $env:ProgramData 'Setuora')
            $code = Invoke-SetuoraNative "powershell.exe" @("-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $ApplicationRoot "scripts\windows\uninstall.ps1"), "-Product", "Master")
            if ($code -eq 0) { $script:UninstallSucceeded = $true }
            return $code
        }
        "sftp-install" {
            return Invoke-SetuoraNative "powershell.exe" @("-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $ApplicationRoot "scripts\windows\configure-sftp.ps1"), "-Action", "Install")
        }
        "sftp-add" {
            if ($ExtraArguments.Count -ne 1 -or $ExtraArguments[0] -notmatch '^[A-Za-z0-9_-]+$') { throw "Usage: setuora.ps1 sftp-add FRANCHISE-CODE" }
            return Invoke-SetuoraNative "powershell.exe" @("-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $ApplicationRoot "scripts\windows\configure-sftp.ps1"), "-Action", "AddFranchise", "-FranchiseCode", $ExtraArguments[0])
        }
        default { return Invoke-SetuoraDeployment $Action $ExtraArguments }
    }
}

function Show-SetuoraMenu {
    while ($true) {
        Clear-Host
        Write-Host ""
        Write-Host "  $ProductName" -ForegroundColor Cyan
        Write-Host "  $ApplicationRoot"
        Write-Host ""
        Write-Host "  [1] Open in browser"
        Write-Host "  [2] Start"
        Write-Host "  [3] Stop"
        Write-Host "  [4] Check status"
        Write-Host "  [5] Setup / repair"
        if (Test-Path -LiteralPath (Join-Path $ApplicationRoot ".git")) {
            Write-Host "  [6] Update from Git"
        } else {
            Write-Host "  [6] Install downloaded update"
        }
        Write-Host "  [7] View recent logs"
        Write-Host "  [8] Check configuration"
        Write-Host "  [9] Remove this installation (keep recovery backup)"
        Write-Host "  [0] Exit"
        Write-Host ""
        Write-Host "  Closing this menu leaves Setuora running."
        $selection = Read-Host "Choose an option (0-9)"
        $action = switch ($selection) {
            "1" { "open" }; "2" { "start" }; "3" { "stop" }; "4" { "status" }
            "5" { "setup" }; "6" { "update" }; "7" { "logs" }; "8" { "preflight" }; "9" { "uninstall" }
            "0" { return 0 }
            default { "" }
        }
        if (-not $action) {
            Write-Host "Choose a number from 0 to 9." -ForegroundColor Yellow
        } else {
            try {
                $code = Invoke-SetuoraCommand $action
                if ($action -eq 'uninstall' -and $code -eq 0) { return 0 }
                if ($code -ne 0) { Write-Host "The action did not complete (exit $code). Review the message above; use View recent logs for server errors." -ForegroundColor Yellow }
            } catch {
                Write-Host $_.Exception.Message -ForegroundColor Red
            }
        }
        $null = Read-Host "Press Enter to return to the menu"
    }
}

$exitCode = 1
try {
    if (-not (Test-Path -LiteralPath (Join-Path $ApplicationRoot "deploy.py"))) { throw "This Setuora folder is incomplete. Run the installer again or use a complete source checkout." }
    if ($Command -eq "menu") {
        $exitCode = Show-SetuoraMenu
    } else {
        $exitCode = Invoke-SetuoraCommand $Command $RemainingArguments
    }
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    $exitCode = 1
}
if ($PauseAfter -and -not $script:UninstallSucceeded) { $null = Read-Host "Press Enter to close this Administrator window" }
exit $exitCode
