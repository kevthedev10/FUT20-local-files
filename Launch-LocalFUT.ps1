[CmdletBinding()]
param(
    [string]$GamePath = "",
    [switch]$NoGameLaunch,
    [switch]$SafeGameLaunch
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = $PSScriptRoot
$SettingsPath = Join-Path $ProjectRoot 'launcher-settings.json'
$RuntimeRoot = Join-Path $ProjectRoot 'runtime'
# The FUT server owns status.json and updates it as FIFA advances through
# Blaze/EASW/FUT states. Keep launcher lifecycle status separate so the
# watcher is not blanked when the game presses Enter and the server records
# its next protocol milestone.
$StatusPath = Join-Path $RuntimeRoot 'launcher-status.json'
$PidPath = Join-Path $RuntimeRoot 'launcher-processes.json'
$SessionPath = Join-Path $RuntimeRoot 'session.json'
$HostsPath = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'
# FIX17: also pin common FUT/utas hostnames to localhost so post-accountinfo
# auth calls cannot escape to live EA servers (which would fail silently).
$RedirectLines = @(
    '127.0.0.1 spring18.gosredirector.ea.com',
    '127.0.0.1 utas.external.s2.fut.ea.com',
    '127.0.0.1 utas.external.s3.fut.ea.com',
    '127.0.0.1 utas.external.fut.ea.com',
    '127.0.0.1 utas.s2.fut.ea.com',
    '127.0.0.1 utas.s3.fut.ea.com',
    '127.0.0.1 utas.fut.ea.com',
    '127.0.0.1 fut.ea.com'
)
$LegacyIdentityRedirectLines = @(
    '127.0.0.1 accounts.ea.com',
    '127.0.0.1 gateway.ea.com',
    '127.0.0.1 origin-a.akamaihd.net',
    '127.0.0.1 eaassets-a.akamaihd.net',
    '127.0.0.1 auth.ea.com',
    '127.0.0.1 signin.ea.com'
)
$RedirectLine = $RedirectLines[0]

function Write-StatusLine([string]$Label, [string]$Value, [ConsoleColor]$Color = [ConsoleColor]::White) {
    Write-Host ('{0,-16} {1}' -f ($Label + ':'), $Value) -ForegroundColor $Color
}

function Save-Status([hashtable]$Status) {
    New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
    $Status.updatedAt = (Get-Date).ToString('o')
    $Status | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $StatusPath -Encoding utf8
}

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-Listener([int]$Port) {
    @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1)
}

function Wait-ForPort([int]$Port, [int]$TimeoutSeconds = 15) {
    $until = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if (Get-Listener $Port) { return $true }
        Start-Sleep -Milliseconds 300
    } while ((Get-Date) -lt $until)
    return $false
}

function Add-RedirectHostsEntry {
    $lines = @(Get-Content -LiteralPath $HostsPath -ErrorAction Stop)
    # Match the FIFA 21 EA-App mode: identity and CDN hosts must remain live
    # so EA App can perform legitimate account authentication with public PKI.
    $lines = @($lines | Where-Object { $LegacyIdentityRedirectLines -notcontains $_ })
    $added = 0
    foreach ($entry in $RedirectLines) {
        if ($lines -notcontains $entry) {
            $lines += $entry
            $added++
        }
    }
    if ($added -gt 0) {
        # Write the complete preserved file once. This avoids partial updates
        # when multiple Add-Content calls race with antivirus/file indexing.
        $lines | Set-Content -LiteralPath $HostsPath -Encoding ascii
    }
    ipconfig /flushdns | Out-Null
    $final = @(Get-Content -LiteralPath $HostsPath -ErrorAction Stop)
    $missing = @($RedirectLines | Where-Object { $final -notcontains $_ })
    if ($missing.Count -gt 0) {
        throw "Hosts redirect verification failed; missing $($missing.Count) required entr$(if($missing.Count -eq 1){'y'}else{'ies' })."
    }
    Write-Host ("Hosts redirect: {0} new entries (FUT/utas + redirector)" -f $added) -ForegroundColor Cyan
    return $added
}

function Invoke-TrustPatch([string]$Python, [string]$PatchScript, [int]$GamePid) {
    $cmd = 'echo.|"{0}" "{1}" --pid {2}' -f $Python, $PatchScript, $GamePid
    $output = & cmd.exe /d /c $cmd 2>&1
    return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = @($output) }
}

function Resolve-GamePath([string]$ExplicitPath, [object]$Settings) {
    if ($ExplicitPath) { return $ExplicitPath }
    if ($Settings.fifa_exe) { return [string]$Settings.fifa_exe }
    if ($Settings.gamePath) { return [string]$Settings.gamePath }
    $candidates = @(
        'C:\Program Files\EA Games\FIFA 20\FIFA20.exe',
        'C:\Program Files (x86)\Origin Games\FIFA 20\FIFA20.exe',
        'C:\Program Files\Origin Games\FIFA 20\FIFA20.exe',
        'C:\Program Files (x86)\Steam\steamapps\common\FIFA 20\FIFA20.exe'
    )
    return @($candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1)[0]
}

function Test-ControlHealth([object]$Settings) {
    try {
        $hostName = if ($Settings.local_host) { [string]$Settings.local_host } else { '127.0.0.1' }
        $port = if ($Settings.control_port) { [int]$Settings.control_port } else { 47220 }
        $h = Invoke-RestMethod -Uri ("http://{0}:{1}/v1/health" -f $hostName, $port) -TimeoutSec 3
        return ($null -ne $h.ok -and $h.ok -eq $true)
    } catch { return $false }
}

function Bootstrap-LocalSession([object]$Settings) {
    $hostName = if ($Settings.local_host) { [string]$Settings.local_host } else { '127.0.0.1' }
    $port = if ($Settings.control_port) { [int]$Settings.control_port } else { 47220 }
    $body = @{ accountId=[string]$Settings.test_account_id; personaId=[int]$Settings.persona_id; displayName=[string]$Settings.display_name } | ConvertTo-Json -Compress
    return Invoke-RestMethod -Method Post -Uri ("http://{0}:{1}/v1/session/bootstrap" -f $hostName, $port) -ContentType 'application/json' -Body $body -TimeoutSec 5
}

function Test-LocalRuntimeHealth([object]$Settings) {
    try {
        $hostName = if ($Settings.local_host) { [string]$Settings.local_host } else { '127.0.0.1' }
        $futPort = if ($Settings.fut_port) { [int]$Settings.fut_port } else { 8080 }
        $futTlsPort = if ($Settings.fut_tls_port) { [int]$Settings.fut_tls_port } else { 443 }
        $blazePort = if ($Settings.blaze_port) { [int]$Settings.blaze_port } else { 44321 }
        $account = Invoke-RestMethod -Uri ("http://{0}:{1}/ut/game/fifa20/user/accountinfo" -f $hostName, $futPort) -TimeoutSec 3
        # Accountinfo is intentionally a retail pre-auth document in FIX22.
        # Validate its persona/club path rather than the old local-only
        # isUnderAge field, which is not part of the retail response shape.
        $persona = @($account.userAccountInfo.personas) | Select-Object -First 1
        if ($null -eq $account.userAccountInfo -or $null -eq $persona -or @($persona.userClubList).Count -lt 1) { return $false }
        return (Test-NetConnection -ComputerName $hostName -Port $blazePort -WarningAction SilentlyContinue).TcpTestSucceeded -and
            (Test-NetConnection -ComputerName $hostName -Port $futTlsPort -WarningAction SilentlyContinue).TcpTestSucceeded
    } catch {
        return $false
    }
}

function Resolve-ProjectPath([string]$PathText) {
    if (-not $PathText) { return "" }
    if ([System.IO.Path]::IsPathRooted($PathText)) { return $PathText }
    return (Join-Path $ProjectRoot $PathText)
}

function Keep-LocalFUTAlive([string]$Message = 'Local FUT services are running. Leave this window open. Use Stop-LocalFUT.cmd when finished.') {
    Write-Host ''
    Write-Host $Message -ForegroundColor Cyan
    Write-Host 'Press Ctrl+C only if you intentionally want to stop watching this console.' -ForegroundColor DarkGray
    while ($true) {
        Start-Sleep -Seconds 5
        if (Test-Path -LiteralPath $StatusPath) {
            try {
                $current = Get-Content -LiteralPath $StatusPath -Raw | ConvertFrom-Json
                Write-Host (('[{0}] Server={1} Game={2}' -f (Get-Date -Format 'HH:mm:ss'), $current.server, $current.game)) -ForegroundColor DarkGray
            } catch {}
        }
    }
}

function Assert-ProjectFiles([object]$Settings) {
    $required = @(
        'localfut20\server.py',
        'localfut20\control_server.py',
        'localfut20-manifest.json',
        'localfut20\offline_data.py',
        'localfut20\players.json',
        'runtime-source\redirector_tls.py',
        'runtime-source\PATCH_RUNNING_FIFA20_TRUST.py',
        'runtime-source\certs\spring18-cert.pem',
        'runtime-source\certs\spring18-key.pem',
        'runtime-source\gos2015-original-modulus.bin',
        'runtime-source\local-gos2015-modulus.bin'
    )
    $missing = @($required | Where-Object { -not (Test-Path -LiteralPath (Join-Path $ProjectRoot $_) -PathType Leaf) })
    if ($missing) { throw "Project is incomplete. Missing: $($missing -join ', ')" }
    $certPath = Resolve-ProjectPath ([string]$Settings.certificate_path)
    $keyPath = Resolve-ProjectPath ([string]$Settings.certificate_key_path)
    if ($certPath -and -not (Test-Path -LiteralPath $certPath -PathType Leaf)) {
        throw "Configured certificate_path does not exist: $($Settings.certificate_path)"
    }
    if ($keyPath -and -not (Test-Path -LiteralPath $keyPath -PathType Leaf)) {
        throw "Configured certificate_key_path does not exist: $($Settings.certificate_key_path)"
    }
}

$status = @{
    status = 'NOT READY'
    server = 'NOT STARTED'
    redirector = 'NOT STARTED'
    game = 'NOT STARTED'
    trustBridge = 'NOT APPLIED'
    connection = 'NOT STARTED'
    fut = 'NOT VERIFIED'
    detail = @()
}
New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null

if (-not $SafeGameLaunch -and -not $NoGameLaunch -and -not (Test-Administrator)) {
    Write-Host 'Administrator permission is required to update the hosts file and patch the running FIFA20.exe process.' -ForegroundColor Yellow
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $PSCommandPath))
    if ($GamePath) { $argList += @('-GamePath', ('"{0}"' -f $GamePath)) }
    if ($NoGameLaunch) { $argList += '-NoGameLaunch' }
    if ($SafeGameLaunch) { $argList += '-SafeGameLaunch' }
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $argList
    exit 0
}

try {
    if (-not (Test-Path -LiteralPath $SettingsPath -PathType Leaf)) { throw "Missing launcher-settings.json at $SettingsPath" }
    $settings = Get-Content -LiteralPath $SettingsPath -Raw | ConvertFrom-Json
    Assert-ProjectFiles $settings

    $python = Get-Command py -ErrorAction Stop
    & $python.Source -3 -c 'import cryptography' 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency missing: cryptography. Install it with: py -m pip install cryptography>=42' }
    & $python.Source -3 -c 'import PIL' 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency missing: Pillow. Install dependencies with: py -3 -m pip install -r requirements.txt' }

    & $python.Source -3 (Join-Path $ProjectRoot 'localfut20\offline_data.py') | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Database initialization failed.' }

    $futPort = if ($settings.fut_port) { [int]$settings.fut_port } else { 8080 }
    $futTlsPort = if ($settings.fut_tls_port) { [int]$settings.fut_tls_port } else { 443 }
    $blazePort = if ($settings.blaze_port) { [int]$settings.blaze_port } else { 44321 }
    $redirectorPort = if ($settings.redirector_port) { [int]$settings.redirector_port } else { 42230 }
    $controlPort = if ($settings.control_port) { [int]$settings.control_port } else { 47220 }
    # accounts.ea.com and utas.* are pinned to localhost.  If 443 is owned by
    # another process the native client fails its next hop before any HTTP log,
    # so treat this as a required local FUT port.
    $portsToCheck = @($futPort, $futTlsPort, $blazePort, $controlPort)
    if (-not $SafeGameLaunch) { $portsToCheck += $redirectorPort }

    $occupied = @()
    foreach ($port in $portsToCheck) {
        $listener = Get-Listener $port
        if ($listener) { $occupied += ("{0} (PID {1})" -f $port, $listener.OwningProcess) }
    }
    if ($occupied) {
        if (-not (Test-LocalRuntimeHealth $settings) -or -not (Test-ControlHealth $settings)) {
            throw "Required local port(s) already in use: $($occupied -join ', '). Run Stop-LocalFUT.cmd or CHECK_PORTS.cmd."
        }
        $status.server = "READY (reused existing FUT/Blaze services)"
        $status.redirector = if ($SafeGameLaunch) { 'SKIPPED by -SafeGameLaunch' } else { 'READY or already running' }
        Write-StatusLine 'Server' 'Reusing already-running healthy local services' Green
        $status.detail += 'Reused existing healthy local services.'
    } else {
        $serverLog = Join-Path $RuntimeRoot 'server.log'
        $controlLog = Join-Path $RuntimeRoot 'control.log'
        $controlError = Join-Path $RuntimeRoot 'control.error.log'
        $control = Start-Process -FilePath $python.Source -ArgumentList @('-3', ('"{0}"' -f (Join-Path $ProjectRoot 'localfut20\control_server.py'))) -WorkingDirectory $ProjectRoot -RedirectStandardOutput $controlLog -RedirectStandardError $controlError -PassThru
        if (-not (Wait-ForPort $controlPort)) {
            if (-not $control.HasExited) { Stop-Process -Id $control.Id -Force }
            throw "Control service did not become ready on port $controlPort. See $controlError"
        }
        $serverError = Join-Path $RuntimeRoot 'server.error.log'
        $server = Start-Process -FilePath $python.Source -ArgumentList @('-3', ('"{0}"' -f (Join-Path $ProjectRoot 'localfut20\server.py'))) -WorkingDirectory $ProjectRoot -RedirectStandardOutput $serverLog -RedirectStandardError $serverError -PassThru
        if (-not (Wait-ForPort $blazePort) -or -not (Wait-ForPort $futPort) -or -not (Wait-ForPort $futTlsPort)) {
            if (-not $server.HasExited) { Stop-Process -Id $server.Id -Force }
            throw "FUT/Blaze server did not become ready on ports $blazePort, $futPort, and required FUT TLS port $futTlsPort. See $serverError"
        }
        $status.server = "READY (Blaze $blazePort, FUT $futPort, FUT TLS $futTlsPort)"
        Write-StatusLine 'Server' $status.server Green

        $redirector = $null
        if (-not $SafeGameLaunch) {
            $redirectorLog = Join-Path $RuntimeRoot 'redirector.log'
            $redirectorError = Join-Path $RuntimeRoot 'redirector.error.log'
            $redirector = Start-Process -FilePath $python.Source -ArgumentList @('-3', ('"{0}"' -f (Join-Path $ProjectRoot 'runtime-source\redirector_tls.py'))) -WorkingDirectory $ProjectRoot -RedirectStandardOutput $redirectorLog -RedirectStandardError $redirectorError -PassThru
            if (-not (Wait-ForPort $redirectorPort)) {
                if (-not $redirector.HasExited) { Stop-Process -Id $redirector.Id -Force }
                if (-not $server.HasExited) { Stop-Process -Id $server.Id -Force }
                throw "TLS redirector did not become ready on port $redirectorPort. See $redirectorError"
            }
            $status.redirector = "READY (TLS redirector $redirectorPort)"
            Write-StatusLine 'Redirector' $status.redirector Green
            Add-RedirectHostsEntry | Out-Null
            $status.connection = 'HOSTS REDIRECT INSTALLED; waiting for FIFA20 process and trust bridge'
        } else {
            $status.redirector = 'SKIPPED by -SafeGameLaunch'
        }

        @{ controlPid = $control.Id; serverPid = $server.Id; redirectorPid = if ($redirector) { $redirector.Id } else { $null }; projectRoot = $ProjectRoot } |
            ConvertTo-Json | Set-Content -LiteralPath $PidPath -Encoding utf8
    }

    if ($NoGameLaunch) {
        $status.status = 'LOCAL SERVICES READY'
        $status.game = 'SKIPPED by -NoGameLaunch'
        $status.connection = 'LOCAL SERVICES READY; hosts redirect not installed'
        Save-Status $status
        Write-StatusLine 'Game' $status.game Yellow
        Write-StatusLine 'Connection' $status.connection Yellow
        Write-Host "Status file: $StatusPath"
        exit 0
    }

    $resolvedGame = Resolve-GamePath $GamePath $settings
    if (-not $resolvedGame -or -not (Test-Path -LiteralPath $resolvedGame -PathType Leaf)) {
        $status.status = 'LOCAL SERVICES READY'
        $status.game = 'NOT FOUND'
        $status.connection = 'WAITING; hosts redirect not installed'
        $status.detail += 'Set fifa_exe in launcher-settings.json or pass -GamePath with the full FIFA20.exe path.'
        Save-Status $status
        Write-StatusLine 'Game' $status.game Red
        Write-StatusLine 'Connection' $status.connection Yellow
        Write-Host 'Missing prerequisite: FIFA20.exe path. Set fifa_exe in launcher-settings.json.' -ForegroundColor Red
        exit 0
    }

    $session = Bootstrap-LocalSession $settings
    if ($null -eq $session.session.sessionId) { throw 'Local session bootstrap failed.' }
    $status.detail += ('Aurora-style session enrolled: ' + [string]$session.session.sessionId)
    $status.connection = 'SESSION ENROLLED; waiting for FIFA20 process'
    Save-Status $status

    $fifa = Get-Process -Name FIFA20 -ErrorAction SilentlyContinue | Sort-Object StartTime -Descending | Select-Object -First 1
    if (-not $fifa) {
        $game = Start-Process -FilePath $resolvedGame -WorkingDirectory (Split-Path -Path $resolvedGame -Parent) -PassThru
    }
    $deadline = (Get-Date).AddSeconds(120)
    do {
        $fifa = Get-Process -Name FIFA20 -ErrorAction SilentlyContinue | Sort-Object StartTime -Descending | Select-Object -First 1
        if ($fifa) { break }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    if (-not $fifa) { throw "FIFA20.exe did not remain running within 120 seconds after launching $resolvedGame" }
    $status.game = "RUNNING (PID $($fifa.Id))"
    Write-StatusLine 'Game' $status.game Green

    if ($SafeGameLaunch) {
        $status.status = 'LOCAL SERVICES READY'
        $status.trustBridge = 'SKIPPED by -SafeGameLaunch'
        $status.connection = 'LOCAL SERVICES READY; stopped before TLS redirector, trust patch, and hosts routing'
        Save-Status $status
        Write-StatusLine 'Trust bridge' $status.trustBridge Yellow
        Write-StatusLine 'Connection' $status.connection Yellow
        Keep-LocalFUTAlive
        exit 0
    }

    $delay = [Math]::Max(0, [int]$settings.gameReadyDelaySeconds)
    if ($delay -gt 0) { Start-Sleep -Seconds $delay }
    # Re-resolve after the launch hand-off. EA may terminate the bootstrap
    # process and leave a short gap before the long-lived game appears.
    $replacementDeadline = (Get-Date).AddSeconds(120)
    do {
        $fifa = Get-Process -Name FIFA20 -ErrorAction SilentlyContinue | Sort-Object StartTime -Descending | Select-Object -First 1
        if ($fifa) { break }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $replacementDeadline)
    if (-not $fifa) { throw 'FIFA20.exe replacement process did not appear within 120 seconds.' }
    $status.game = "RUNNING (PID $($fifa.Id))"
    $patch = Invoke-TrustPatch $python.Source (Join-Path $ProjectRoot 'runtime-source\PATCH_RUNNING_FIFA20_TRUST.py') $fifa.Id
    $patch.Output | Set-Content -LiteralPath (Join-Path $RuntimeRoot 'trust-patch.log') -Encoding utf8
    if ($patch.Output -match '\[SUCCESS\] Patched') {
        $status.trustBridge = 'PATCHED (running process only)'
        # Idempotent second pass verifies the exact entries still exist after
        # the game process has been patched.
        Add-RedirectHostsEntry | Out-Null
        $status.status = 'FIFA CONNECTED SETUP READY'
        $status.connection = 'READY; hosts redirect installed and required local ports accessible'
        Write-StatusLine 'Trust bridge' $status.trustBridge Green
        Write-StatusLine 'Connection' $status.connection Green
    } else {
        $status.trustBridge = 'FAILED; see runtime\trust-patch.log'
        $status.connection = 'NOT REDIRECTED; hosts file was left unchanged'
        $status.detail += 'The exact trust-patch output is saved in runtime\trust-patch.log.'
        Write-StatusLine 'Trust bridge' $status.trustBridge Red
        Write-StatusLine 'Connection' $status.connection Red
    }
    Save-Status $status
    Write-Host "Status file: $StatusPath"
    Keep-LocalFUTAlive
} catch {
    $status.status = 'FAILED'
    $status.server = if ($status.server -eq 'NOT STARTED') { 'FAILED' } else { $status.server }
    $status.connection = 'NOT READY'
    $status.detail += $_.Exception.Message
    Save-Status $status
    Write-StatusLine 'ERROR' $_.Exception.Message Red
    Write-Host "Status file: $StatusPath"
    exit 1
}
