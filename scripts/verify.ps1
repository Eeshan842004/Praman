# Praman — attest the committed evidence ledger. One command from a fresh clone.
#
#     .\scripts\verify.ps1
#
# Fetches opa.exe into tools\ if it is missing, then recomputes the hash chain
# AND re-POSTs every recorded policy input to OPA loaded with the exact bundle
# that authorised it. Two independent checks:
#
#     chain   nothing was changed after the fact
#     replay  the record was true when it was written
#
# The second is the one a hash chain cannot do. A writer holding the append path
# can bypass the policy engine and record its own verdict, and the chain stays
# perfect; only re-deriving the decision from the pinned bundle catches that.
#
# Nothing is downloaded outside this repo's own tools\ directory, and no Python
# environment has to be set up by hand — uv builds it on demand.

param(
    [string]$Ledger = 'data/ledger.db'
)

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

$OpaVersion = '1.19.1'

function Say($m)  { Write-Host $m -ForegroundColor Cyan }
function Die($m)  { Write-Host "error: $m" -ForegroundColor Red; exit 1 }

# ── OPA ───────────────────────────────────────────────────────────────────────
$opa = Join-Path (Get-Location) 'tools\opa.exe'
if (-not (Test-Path $opa)) {
    $onPath = Get-Command opa -ErrorAction SilentlyContinue
    if ($onPath) {
        $opa = $onPath.Source
    } else {
        Say "fetching OPA $OpaVersion into tools\ ..."
        New-Item -ItemType Directory -Force -Path 'tools' | Out-Null
        $url = "https://openpolicyagent.org/downloads/v$OpaVersion/opa_windows_amd64.exe"
        try {
            Invoke-WebRequest -Uri $url -OutFile $opa -UseBasicParsing
        } catch {
            Die "could not download OPA. Check network access to openpolicyagent.org."
        }
    }
}
Say "opa: $((& $opa version)[0])"

# ── Python, via uv ────────────────────────────────────────────────────────────
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Die @'
uv is required. Install it with:
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
It is the only prerequisite; it builds the Python environment on demand.
'@
}

if (-not (Test-Path $Ledger)) {
    Die @"
no ledger at $Ledger.
The committed one lives at data/ledger.db. To rebuild it:
    uv run python scripts/regenerate_ledger.py --mode demo
"@
}

# ── Attest ────────────────────────────────────────────────────────────────────
Say "attesting $Ledger ..."
Write-Host ''
# --no-dev: a judge needs the runtime dependencies, not pytest, mypy and
# bandit. Installing the dev group is slower and, on Windows, fails outright
# -- mypy's wheel rewrites a PE trampoline and hits a permissions error.
# `python -m praman.cli`, not the `praman` console script: uv materialises a
# console script as a .exe trampoline, and writing it can fail against
# antivirus or a file lock. Running the module needs no trampoline.
& uv run --no-dev --quiet python -m praman.cli verify --ledger $Ledger --opa $opa --require-replay
exit $LASTEXITCODE
