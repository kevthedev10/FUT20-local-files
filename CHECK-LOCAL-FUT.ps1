$ErrorActionPreference = 'Stop'
$script:CheckFailed = $false
$ProjectRoot = $PSScriptRoot
Write-Host 'FIFA 20 Local FUT - Preflight Check' -ForegroundColor Cyan
Write-Host '===================================='

$required = @(
  'Launch-LocalFUT.cmd','Stop-LocalFUT.cmd','launcher-settings.json',
  'localfut20\server.py','localfut20\control_server.py','localfut20\config_loader.py',
  'localfut20\offline_data.py','localfut20\players.json',
  'runtime-source\redirector_tls.py','runtime-source\PATCH_RUNNING_FIFA20_TRUST.py',
  'runtime-source\certs\spring18-cert.pem','runtime-source\certs\spring18-key.pem',
  'runtime-source\gos2015-original-modulus.bin','runtime-source\local-gos2015-modulus.bin'
)
foreach ($item in $required) {
  if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot $item) -PathType Leaf)) {
    Write-Host "[FAIL] Missing project file: $item" -ForegroundColor Red; $script:CheckFailed=$true
  }
}
Write-Host '[OK] Project files present.' -ForegroundColor Green

$py = Get-Command py -ErrorAction SilentlyContinue
if (-not $py) { Write-Host '[FAIL] Python launcher "py.exe" not found.' -ForegroundColor Red; $script:CheckFailed=$true }
$pyVersion = (& $py.Source -3 --version 2>&1 | Out-String).Trim()
if ($pyVersion -notmatch 'Python 3\.(10|11|12|13|14)') {
  Write-Host "[FAIL] Python 3.10+ is required. Detected: $pyVersion" -ForegroundColor Red; $script:CheckFailed=$true
}
Write-Host "[OK] $pyVersion" -ForegroundColor Green

& $py.Source -3 -c 'import cryptography; print("cryptography", cryptography.__version__)' 2>$null
if ($LASTEXITCODE -ne 0) { Write-Host '[FAIL] cryptography is missing. Run: py -3 -m pip install -r requirements.txt' -ForegroundColor Red; $script:CheckFailed=$true }
Write-Host '[OK] cryptography installed.' -ForegroundColor Green

& $py.Source -3 -c 'import PIL; print("Pillow", PIL.__version__)' 2>$null
if ($LASTEXITCODE -ne 0) { Write-Host '[FAIL] Pillow is missing. Run: py -3 -m pip install -r requirements.txt' -ForegroundColor Red; $script:CheckFailed=$true }
Write-Host '[OK] Pillow installed.' -ForegroundColor Green

$settings = Get-Content (Join-Path $ProjectRoot 'launcher-settings.json') -Raw | ConvertFrom-Json
$hostName = if ($settings.local_host) { [string]$settings.local_host } else { '127.0.0.1' }
$futTlsPort = if ($settings.fut_tls_port) { [int]$settings.fut_tls_port } else { 443 }
$ports = @(
  @{n='Control'; p=[int]$settings.control_port},
  @{n='FUT'; p=[int]$settings.fut_port},
  @{n='FUT TLS'; p=$futTlsPort},
  @{n='Blaze'; p=[int]$settings.blaze_port},
  @{n='Redirector'; p=[int]$settings.redirector_port},
  @{n='EASW'; p=[int]$settings.easw_port}
)
function Test-PortInUse([int]$port) {
  # Use netstat instead of Get-NetTCPConnection so this works on Windows 7+
  # and on PowerShell installations without the NetTCPIP module.
  $lines = @(netstat.exe -ano -p tcp 2>$null)
  foreach ($line in $lines) {
    if ($line -match "\s(?:0\.0\.0\.0|127\.0\.0\.1|\[::\]|::):$port\s+.*\bLISTENING\b\s+(\d+)\s*$") {
      return [int]$Matches[1]
    }
  }
  return 0
}

foreach ($entry in $ports) {
  $ownerPid = Test-PortInUse $entry.p
  if ($ownerPid -gt 0) {
    Write-Host "[WARN] $($entry.n) port $($entry.p) is already in use by PID $ownerPid." -ForegroundColor Yellow
  } else {
    Write-Host "[OK] $($entry.n) port $($entry.p) is free." -ForegroundColor Green
  }
}

$exe = [string]$settings.fifa_exe
if ($exe -and (Test-Path -LiteralPath $exe -PathType Leaf)) {
  Write-Host "[OK] FIFA20.exe: $exe" -ForegroundColor Green
} elseif ($exe) {
  Write-Host "[WARN] Configured FIFA20.exe was not found: $exe" -ForegroundColor Yellow
} else {
  $candidates = @(
    'C:\Program Files\EA Games\FIFA 20\FIFA20.exe',
    'C:\Program Files (x86)\Origin Games\FIFA 20\FIFA20.exe',
    'C:\Program Files\Origin Games\FIFA 20\FIFA20.exe',
    'C:\Program Files (x86)\Steam\steamapps\common\FIFA 20\FIFA20.exe'
  )
  $found = @($candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
  if ($found.Count -gt 0) {
    Write-Host '[OK] Auto-detected FIFA20.exe:' -ForegroundColor Green
    $found | ForEach-Object { Write-Host "     $_" }
  } else {
    Write-Host '[WARN] FIFA20.exe was not auto-detected. Set fifa_exe in launcher-settings.json or use -GamePath.' -ForegroundColor Yellow
  }
}

Write-Host ''
if ($script:CheckFailed) { Write-Host 'Preflight check FAILED. The window will stay open.' -ForegroundColor Red } else { Write-Host 'Preflight check PASSED. The window will stay open.' -ForegroundColor Green }
Write-Host ''
Read-Host 'Press ENTER to close this check window' | Out-Null
