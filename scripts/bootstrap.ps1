# Praman -- one-time local toolchain bootstrap.
#
#   .\scripts\bootstrap.ps1
#
# Fetches the two binaries the dev loop needs. Both land in tools/, which is
# gitignored -- they are toolchain, not evidence. ASCII-only by policy (see
# dev.ps1 header for why).

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$tools = Join-Path $root 'tools'
New-Item -ItemType Directory -Force -Path $tools | Out-Null

if (-not (Test-Path (Join-Path $tools 'opa.exe'))) {
    Write-Host 'bootstrap: downloading OPA...' -ForegroundColor Cyan
    Invoke-WebRequest -UseBasicParsing `
        -Uri 'https://openpolicyagent.org/downloads/latest/opa_windows_amd64.exe' `
        -OutFile (Join-Path $tools 'opa.exe')
}
& (Join-Path $tools 'opa.exe') version | Select-Object -First 1

if (-not (Test-Path (Join-Path $tools 'cloudflared.exe'))) {
    Write-Host 'bootstrap: downloading cloudflared...' -ForegroundColor Cyan
    Invoke-WebRequest -UseBasicParsing `
        -Uri 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' `
        -OutFile (Join-Path $tools 'cloudflared.exe')
}
& (Join-Path $tools 'cloudflared.exe') --version

Write-Host 'bootstrap: ok. Next: uv sync; .\scripts\dev.ps1' -ForegroundColor Green
