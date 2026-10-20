$ErrorActionPreference = 'Stop'
$ProjectRoot = $PSScriptRoot
$RuntimeRoot = Join-Path $ProjectRoot 'runtime'
$PidPath = Join-Path $RuntimeRoot 'launcher-processes.json'
$SessionPath = Join-Path $RuntimeRoot 'session.json'
$VendorRoot = Join-Path $ProjectRoot 'runtime-source'
$HostsPath = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'
# FIX17: remove all local FUT/utas redirects we may have installed.
$Patterns = @(
    '^\s*127\.0\.0\.1\s+spring18\.gosredirector\.ea\.com\s*$',
    '^\s*127\.0\.0\.1\s+utas\.external\.s2\.fut\.ea\.com\s*$',
    '^\s*127\.0\.0\.1\s+utas\.external\.s3\.fut\.ea\.com\s*$',
    '^\s*127\.0\.0\.1\s+utas\.external\.fut\.ea\.com\s*$',
    '^\s*127\.0\.0\.1\s+utas\.s2\.fut\.ea\.com\s*$',
    '^\s*127\.0\.0\.1\s+utas\.s3\.fut\.ea\.com\s*$',
    '^\s*127\.0\.0\.1\s+utas\.fut\.ea\.com\s*$',
    '^\s*127\.0\.0\.1\s+fut\.ea\.com\s*$',
    '^\s*127\.0\.0\.1\s+accounts\.ea\.com\s*$',
    '^\s*127\.0\.0\.1\s+gateway\.ea\.com\s*$',
    '^\s*127\.0\.0\.1\s+origin-a\.akamaihd\.net\s*$',
    '^\s*127\.0\.0\.1\s+eaassets-a\.akamaihd\.net\s*$',
    '^\s*127\.0\.0\.1\s+auth\.ea\.com\s*$',
    '^\s*127\.0\.0\.1\s+signin\.ea\.com\s*$'
)
$Pattern = $Patterns[0]

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Administrator)) {
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $PSCommandPath))
    exit 0
}

if (Test-Path -LiteralPath $PidPath) {
    $tracked = Get-Content -LiteralPath $PidPath -Raw | ConvertFrom-Json
    foreach ($name in 'controlPid', 'serverPid', 'redirectorPid') {
        $id = [int]$tracked.$name
        if ($id -gt 0 -and (Get-Process -Id $id -ErrorAction SilentlyContinue)) {
            Stop-Process -Id $id -Force
            Write-Host "Stopped tracked $name process $id."
        }
    }
    Remove-Item -LiteralPath $PidPath -Force
} else {
    Write-Host "No tracked launcher processes were found; checking for this launcher's orphaned local-runtime processes."
}

# A failed start can occur after the services are created but before their PIDs
# are saved. Restrict fallback cleanup to Python processes whose command line
# names one of this project's two runtime entry points.
$ownedScripts = @(
    (Join-Path $ProjectRoot 'localfut20\control_server.py'),
    (Join-Path $ProjectRoot 'localfut20\server.py'),
    (Join-Path $VendorRoot 'redirector_tls.py')
)
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ForEach-Object {
    $commandLine = $_.CommandLine
    $owned = $ownedScripts | Where-Object { $_ -and $_.Length -gt 0 -and $commandLine -and $commandLine.ToLowerInvariant().Contains($_.ToLowerInvariant()) }
    if ($owned) {
        Stop-Process -Id $_.ProcessId -Force
        Write-Host "Stopped orphaned local-runtime process $($_.ProcessId)."
    }
}

$lines = @(Get-Content -LiteralPath $HostsPath -ErrorAction Stop)
$filtered = $lines | Where-Object {
    $line = $_
    -not ($Patterns | Where-Object { $line -match $_ })
}
if ($filtered.Count -ne $lines.Count) {
    $filtered | Set-Content -LiteralPath $HostsPath -Encoding ascii
    ipconfig /flushdns | Out-Null
    Write-Host 'Removed local FUT/utas hosts redirect entries.'
} else {
    Write-Host 'No FIFA/FUT hosts redirect entries were present.'
}

Remove-Item -LiteralPath $SessionPath -Force -ErrorAction SilentlyContinue
