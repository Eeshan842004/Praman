# Rebuild the evidence ledger. `demo` writes the ~3 MB two-bundle file that is
# committed; `full` writes the complete ~23 MB run behind the reported figures.
#
#     .\scripts\regenerate_ledger.ps1 [demo|full]
param([ValidateSet('demo', 'full')][string]$Mode = 'demo')
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
& uv run python scripts/regenerate_ledger.py --mode $Mode
exit $LASTEXITCODE
