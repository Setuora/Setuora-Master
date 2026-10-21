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

function Get-SetuoraBrowserUrl {
    $portFile = Join-Path $ApplicationRoot 'runtime-port.txt'
    $browserPort = '8000'
    if (Test-Path -LiteralPath $portFile) {
        $candidate = (Get-Content -LiteralPath $portFile -TotalCount 1).Trim()
        if ($candidate -notmatch '^[0-9]{4,5}$' -or [int]$candidate -lt 1024 -or [int]$candidate -gt 65535) {
            throw "The local browser port file is invalid. Choose Start to repair it."
        }
        $browserPort = $candidate
    }
    return "http://127.0.0.1:$browserPort"
}

$BrowserUrl = Get-SetuoraBrowserUrl
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
        if ($code -eq 3010) { throw "Python installed. Restart Windows, then choose Start again." }
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
        if ($process.ExitCode -eq 3010) { throw "Python installed. Restart Windows, then choose Start again." }
    } finally {
        Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
    }
    if (-not (Get-SetuoraPython)) { throw "Python installed but was not found. Restart Windows and choose Start again." }
}

function Invoke-SetuoraDeployment([string]$Action, [string[]]$ExtraArguments = @()) {
    $python = Get-SetuoraPython
    if (-not $python -and $Action -eq "setup") {
        Install-SetuoraPython
        $python = Get-SetuoraPython
    }
    if (-not $python) { throw "Python 3.11 or newer was not found. Choose Start to install it." }
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
        throw 'The installed Python runtime is missing, so no pre-update backup can be made.'
    }
    Write-Host 'Creating a verified SQLite backup before updating...' -ForegroundColor Cyan
    $code = Invoke-SetuoraNative $python @(
        '-c', 'from app.services.backup import create_scheduled_backup; print(create_scheduled_backup().path)'
    )
    if ($code -ne 0) { throw 'The pre-update database backup failed. Master was left running.' }
}

function Update-SetuoraCheckout {
    # Fast-forward only a clean checkout that has an upstream. When Git is
    # unreachable, the checkout has local changes or its history diverged,
    # warn and let Start run the current commit.
    if (-not (Get-Command "git.exe" -ErrorAction SilentlyContinue)) {
        Write-Warning "Git for Windows is missing; starting the current version without checking for updates."
        return
    }
    $env:GIT_TERMINAL_PROMPT = "0"
    try {
        if (Read-SetuoraGit @("status", "--porcelain", "--untracked-files=all")) {
            Write-Warning "The source checkout has local changes; starting the current version without updating it."
            return
        }
        $branch = Read-SetuoraGit @("branch", "--show-current")
        if (-not $branch) {
            Write-Warning "The checkout is not on a branch; starting the current version without updating it."
            return
        }
        Write-Host "Checking origin/$branch for updates..." -ForegroundColor Cyan
        $null = Read-SetuoraGit @("fetch", "--quiet", "origin")
        $null = Read-SetuoraGit @("rev-parse", "--verify", "refs/remotes/origin/$branch")
        if ((Read-SetuoraGit @("rev-parse", "HEAD")) -eq (Read-SetuoraGit @("rev-parse", "origin/$branch"))) { return }
    } catch {
        Write-Warning "Updates could not be checked ($($_.Exception.Message)). Starting the current version."
        return
    }
    try {
        $null = Read-SetuoraGit @("merge-base", "--is-ancestor", "HEAD", "origin/$branch")
    } catch {
        Write-Warning "This checkout has diverged from origin/$branch; starting the current version without updating it."
        return
    }
    if (Test-Path -LiteralPath (Join-Path $ApplicationRoot ".env")) {
        if (-not (Test-Path -LiteralPath (Join-Path $ApplicationRoot ".venv\Scripts\python.exe"))) {
            # No runtime means no verified backup; repair the current version first.
            Write-Warning "The installed runtime is missing; repairing the current version. Choose Start again afterwards to update."
            return
        }
        $code = Invoke-SetuoraDeployment "preflight"
        if ($code -ne 0) { throw "Configuration check failed; Master was not updated." }
        Backup-SetuoraSource
        $code = Invoke-SetuoraDeployment "stop"
        if ($code -ne 0) { throw "Master could not be stopped for the update; no files were changed." }
    }
    try {
        $null = Read-SetuoraGit @("merge", "--ff-only", "origin/$branch")
    } catch {
        Write-Warning "The update could not be applied ($($_.Exception.Message)). Starting the current version."
    }
}

function Invoke-SetuoraStart {
    # Start installs, repairs or updates this copy in place, then runs and
    # verifies Master.
    if (Test-Path -LiteralPath (Join-Path $ApplicationRoot ".git")) { Update-SetuoraCheckout }
    return Invoke-SetuoraDeployment "setup"
}

function Open-SetuoraBrowser {
    # Start can move Master to another free port, so read the saved port again.
    Start-Process -FilePath (Get-SetuoraBrowserUrl)
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
    Write-Host "Double-click setuora.bat to open the menu: Start, Stop, Uninstall."
    Write-Host "Start installs, repairs or updates this copy, runs it and opens the browser."
    Write-Host "Stop keeps Master stopped, even after Windows restarts, until you choose Start."
    Write-Host "Uninstall saves a verified recovery copy, then removes the application."
    Write-Host "Commands: start, stop, uninstall, setup, status, open, logs, preflight, update, help"
    Write-Host "Every command except status, open and help requests Administrator access."
    Write-Host "Browser: $BrowserUrl"
}

function Invoke-SetuoraCommand([string]$Action, [string[]]$ExtraArguments = @()) {
    if ($Action -in @("setup", "start", "stop", "preflight", "update", "update-runtime", "uninstall", "logs", "sftp-install", "sftp-add")) {
        if (-not (Test-SetuoraAdministrator)) {
            $code = Invoke-SetuoraElevated $Action $ExtraArguments
            # Open the browser on the original user's desktop, not as Administrator.
            if ($Action -eq "start" -and $code -eq 0) { Open-SetuoraBrowser }
            return $code
        }
    }
    switch ($Action) {
        "help" { Show-SetuoraHelp; return 0 }
        "start" {
            $code = Invoke-SetuoraStart
            if ($code -eq 0 -and -not $Elevated) { Open-SetuoraBrowser }
            return $code
        }
        "open" {
            $code = Invoke-SetuoraDeployment "status"
            if ($code -ne 0) { return $code }
            Start-Process -FilePath $BrowserUrl
            return 0
        }
        "update" {
            if (Test-Path -LiteralPath (Join-Path $ApplicationRoot ".git")) { return Invoke-SetuoraStart }
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
        Write-Host ""
        Write-Host "$ProductName controls" -ForegroundColor Cyan
        Write-Host "$ApplicationRoot"
        Write-Host "[1] Start"
        Write-Host "[2] Stop"
        Write-Host "[3] Uninstall (keeps a recovery backup)"
        $selection = Read-Host "Choose (close this window to exit)"
        $action = switch ($selection) {
            "1" { "start" }; "2" { "stop" }; "3" { "uninstall" }; default { "" }
        }
        if (-not $action) { Write-Host "Choose a listed number." -ForegroundColor Yellow; continue }
        try {
            $code = Invoke-SetuoraCommand $action
            if ($action -eq 'uninstall' -and $code -eq 0) { return 0 }
            if ($code -ne 0) { Write-Host "The action did not complete (exit $code). Review the message above; server errors are in $ApplicationRoot\logs." -ForegroundColor Yellow }
        } catch {
            Write-Host $_.Exception.Message -ForegroundColor Red
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
