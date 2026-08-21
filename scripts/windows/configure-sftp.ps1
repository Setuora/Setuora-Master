[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Install", "AddFranchise")]
    [string]$Action,

    [string]$ExchangeRoot = "$env:ProgramData\Setuora\sftp",

    [ValidatePattern("^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$")]
    [string]$FranchiseCode
)

$ErrorActionPreference = "Stop"
$groupName = "SetuoraSftpUsers"
$configPath = "$env:ProgramData\ssh\sshd_config"
$managedStart = "# BEGIN SETUORA SFTP"
$managedEnd = "# END SETUORA SFTP"

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Administrator)) {
    $arguments = @(
        "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", ('"' + $PSCommandPath + '"'),
        "-Action", $Action,
        "-ExchangeRoot", ('"' + $ExchangeRoot + '"')
    )
    if ($FranchiseCode) { $arguments += @("-FranchiseCode", $FranchiseCode) }
    $process = Start-Process powershell.exe -Verb RunAs -Wait -PassThru -ArgumentList $arguments
    exit $process.ExitCode
}

function Install-SetuoraSftp {
    $capability = Get-WindowsCapability -Online -Name "OpenSSH.Server~~~~0.0.1.0"
    if ($capability.State -ne "Installed") {
        Add-WindowsCapability -Online -Name "OpenSSH.Server~~~~0.0.1.0" | Out-Null
    }

    New-Item -ItemType Directory -Path (Join-Path $ExchangeRoot "franchises") -Force | Out-Null
    if (-not (Get-LocalGroup -Name $groupName -ErrorAction SilentlyContinue)) {
        New-LocalGroup -Name $groupName -Description "SFTP-only Setuora franchise accounts" | Out-Null
    }

    $rootForSsh = $ExchangeRoot.Replace("\", "/")
    $block = @"
$managedStart
Match Group $groupName
    ChrootDirectory $rootForSsh/franchises/%u
    ForceCommand internal-sftp -d /inbox
    PasswordAuthentication yes
    AllowTcpForwarding no
    PermitTunnel no
    X11Forwarding no
$managedEnd
"@
    $config = Get-Content -LiteralPath $configPath -Raw
    $pattern = "(?ms)^" + [regex]::Escape($managedStart) + ".*?^" + [regex]::Escape($managedEnd) + "\s*"
    $config = [regex]::Replace($config, $pattern, "").TrimEnd() + "`r`n`r`n" + $block + "`r`n"
    $backup = "$configPath.setuora-backup"
    if (-not (Test-Path -LiteralPath $backup)) {
        Copy-Item -LiteralPath $configPath -Destination $backup
    }
    Set-Content -LiteralPath $configPath -Value $config -Encoding ascii

    & "$env:WINDIR\System32\OpenSSH\sshd.exe" -t
    if ($LASTEXITCODE -ne 0) {
        Copy-Item -LiteralPath $backup -Destination $configPath -Force
        throw "OpenSSH rejected the Setuora configuration; the previous file was restored."
    }
    Set-Service -Name sshd -StartupType Automatic
    if ((Get-Service sshd).Status -eq "Running") { Restart-Service sshd } else { Start-Service sshd }
    if (-not (Get-NetFirewallRule -Name "Setuora-SFTP-In-TCP" -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -Name "Setuora-SFTP-In-TCP" -DisplayName "Setuora SFTP Server" `
            -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
    }
    Write-Host "Windows OpenSSH SFTP is ready on TCP 22."
}

function Add-SetuoraFranchise {
    if (-not $FranchiseCode) { throw "-FranchiseCode is required for AddFranchise." }
    Install-SetuoraSftp
    $code = $FranchiseCode.ToUpperInvariant()
    $username = $code.ToLowerInvariant()
    $franchiseRoot = Join-Path (Join-Path $ExchangeRoot "franchises") $code
    foreach ($folder in @("inbox", "outbox", "ack", "processed", "failed")) {
        New-Item -ItemType Directory -Path (Join-Path $franchiseRoot $folder) -Force | Out-Null
    }

    if (-not (Get-LocalUser -Name $username -ErrorAction SilentlyContinue)) {
        $password = Read-Host "Password for SFTP user $username" -AsSecureString
        New-LocalUser -Name $username -Password $password -PasswordNeverExpires `
            -UserMayNotChangePassword -Description "Setuora SFTP franchise $code" | Out-Null
    }
    if (-not (Get-LocalGroupMember -Group $groupName -Member $username -ErrorAction SilentlyContinue)) {
        Add-LocalGroupMember -Group $groupName -Member $username
    }

    & icacls.exe $franchiseRoot /inheritance:r /grant:r `
        "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" "${username}:(RX)" | Out-Null
    foreach ($folder in @("inbox", "ack")) {
        & icacls.exe (Join-Path $franchiseRoot $folder) /inheritance:r /grant:r `
            "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" "${username}:(OI)(CI)M" | Out-Null
    }
    & icacls.exe (Join-Path $franchiseRoot "outbox") /inheritance:r /grant:r `
        "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" "${username}:(OI)(CI)RX" | Out-Null
    foreach ($folder in @("processed", "failed")) {
        & icacls.exe (Join-Path $franchiseRoot $folder) /inheritance:r /grant:r `
            "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" | Out-Null
    }
    Restart-Service sshd
    Write-Host "Franchise $code is ready. SFTP username: $username"
    Write-Host "Upload XML to /inbox and download Setuora XML from /outbox."
}

if ($Action -eq "Install") { Install-SetuoraSftp } else { Add-SetuoraFranchise }
