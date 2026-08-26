# Praman -- local dev loop (no Docker required).
#
#   .\scripts\dev.ps1            start OPA sidecar + FastAPI
#   .\scripts\dev.ps1 -Tunnel    also open a cloudflared HTTPS tunnel for webhooks
#   .\scripts\dev.ps1 -Stop      stop everything
#
# Mirrors docker-compose.yml exactly: OPA on :8181 as a separate process with its
# own lifecycle, FastAPI on :8000. Law #9 -- if OPA is down, the app denies.
#
# NOTE: this file is deliberately ASCII-only. Windows PowerShell 5.1 reads .ps1
# as ANSI unless there is a BOM, so a UTF-8 arrow or dash inside a string
# decodes to a stray smart-quote and produces a parser error.

param(
    [switch]$Tunnel,
    [switch]$Stop
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$opa = Join-Path $root 'tools\opa.exe'
$cfd = Join-Path $root 'tools\cloudflared.exe'

function Stop-Praman {
    foreach ($n in @('opa', 'cloudflared')) {
        Get-Process -Name $n -ErrorAction SilentlyContinue | Stop-Process -Force
    }
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*uvicorn*praman*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Host 'praman: stopped' -ForegroundColor Yellow
}

if ($Stop) { Stop-Praman; exit 0 }

if (-not (Test-Path $opa)) {
    Write-Error 'tools/opa.exe missing. Run: .\scripts\bootstrap.ps1'
}

Stop-Praman

# Build the immutable bundle so OPA reports a real revision (law #6).
& (Join-Path $PSScriptRoot 'build_bundle.ps1')
$rev = (Get-Content (Join-Path $root 'policy\revision\data.json') | ConvertFrom-Json).revision

# The bundle path is passed RELATIVE and the working directory is set instead.
# Start-Process -ArgumentList joins the array on spaces WITHOUT quoting, so an
# absolute path containing a space (this repo lives under "Razorpay buildathon")
# would split into two arguments and OPA would silently fail to load the bundle.
$bundleRel = "dist\bundle-$rev.tar.gz"
if (-not (Test-Path (Join-Path $root $bundleRel))) {
    Write-Error "bundle missing: $bundleRel"
}

# ---- OPA sidecar ----
Write-Host "praman: starting OPA sidecar on :8181 (bundle $rev)" -ForegroundColor Cyan
Start-Process -FilePath $opa -WorkingDirectory $root -ArgumentList @(
    'run', '--server',
    '--addr=127.0.0.1:8181',
    '--log-level=info',
    '--set=decision_logs.console=true',
    '--bundle', $bundleRel
) -WindowStyle Hidden

# Wait for readiness rather than sleeping blindly.
$ready = $false
foreach ($i in 1..40) {
    try {
        $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8181/health' -UseBasicParsing -TimeoutSec 1
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch { Start-Sleep -Milliseconds 250 }
}
if (-not $ready) { Write-Error 'OPA did not become healthy on :8181' }
Write-Host 'praman: OPA healthy' -ForegroundColor Green

# ---- FastAPI ----
$env:OPA_URL = 'http://127.0.0.1:8181'
Write-Host 'praman: starting FastAPI on :8000' -ForegroundColor Cyan
Start-Process -FilePath 'uv' -ArgumentList @(
    'run', 'uvicorn', 'praman.api.app:app',
    '--host', '127.0.0.1', '--port', '8000'
) -WindowStyle Hidden

$appReady = $false
foreach ($i in 1..60) {
    try {
        $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/healthz' -UseBasicParsing -TimeoutSec 1
        if ($r.StatusCode -eq 200) { $appReady = $true; break }
    } catch { Start-Sleep -Milliseconds 500 }
}
if (-not $appReady) { Write-Error 'FastAPI did not become healthy on :8000' }
Write-Host 'praman: app healthy -> http://127.0.0.1:8000' -ForegroundColor Green

# ---- Optional public HTTPS tunnel for Razorpay webhooks ----
if ($Tunnel) {
    if (-not (Test-Path $cfd)) { Write-Error 'tools/cloudflared.exe missing.' }
    Write-Host 'praman: opening cloudflared tunnel (paste the URL into Razorpay webhooks)' -ForegroundColor Cyan
    & $cfd tunnel --url http://127.0.0.1:8000
}
