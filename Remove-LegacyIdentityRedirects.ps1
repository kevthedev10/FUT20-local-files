$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $elevated = Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $PSCommandPath)
    ) -Wait -PassThru
    exit $elevated.ExitCode
}
$hostsPath = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'
$remove = @(
    '127.0.0.1 accounts.ea.com',
    '127.0.0.1 gateway.ea.com',
    '127.0.0.1 origin-a.akamaihd.net',
    '127.0.0.1 eaassets-a.akamaihd.net',
    '127.0.0.1 auth.ea.com',
    '127.0.0.1 signin.ea.com'
)
$lines = @(Get-Content -LiteralPath $hostsPath)
$filtered = @($lines | Where-Object { $remove -notcontains $_ })
$filtered | Set-Content -LiteralPath $hostsPath -Encoding ascii
ipconfig /flushdns | Out-Null
Write-Host 'EA identity/CDN hosts now use their live public endpoints.' -ForegroundColor Green
