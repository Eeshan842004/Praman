# Praman — build an immutable, committed, replayable policy bundle.
#
# Architectural law #6: every decision records the revision OPA REPORTS.
# For that to mean anything, the revision must be derived from the policy
# CONTENT and the bundle must be immutable and committed. `dist/bundle-*.tar.gz`
# is evidence, not a build artifact — `praman verify` replays historical
# decisions against the exact bundle that authorised them (mitigates S4).

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$opa = Join-Path $root 'tools\opa.exe'
if (-not (Test-Path $opa)) { Write-Error 'tools/opa.exe missing.' }

New-Item -ItemType Directory -Force -Path (Join-Path $root 'dist') | Out-Null

# ── Revision = sha256 over policy content, sorted for determinism ────────────
# Two deliberate exclusions:
#   revision/data.json -- contains the revision itself; including it would be
#     self-referential and the hash would never stabilise.
#   *_test.rego -- opa build excludes tests from the bundle, so they are not part
#     of what OPA evaluates. Including them would mint a NEW revision for a
#     test-only edit while runtime behaviour is byte-identical, which is exactly
#     the noise law #6 exists to eliminate. The revision must identify the policy
#     that RAN, nothing else.
#
# MUST agree byte-for-byte with build_bundle.sh, which CI runs. Two things make
# that true and are easy to get wrong:
#   * relative paths carry NO leading slash ("policy/retry.rego"), matching
#     find(1) output;
#   * sorting is ORDINAL, not culture-aware, matching `LC_ALL=C sort`.
$prefix = $root.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
$rels = [System.Collections.Generic.List[string]]::new()
Get-ChildItem -Path (Join-Path $root 'policy') -Recurse -File |
    Where-Object { $_.FullName -notmatch 'revision[\\/]data\.json$' } |
    Where-Object { $_.Name -notmatch '_test\.rego$' } |
    Where-Object { $_.Extension -in '.rego', '.json' } |
    ForEach-Object { $rels.Add($_.FullName.Substring($prefix.Length).Replace('\', '/')) }
$rels.Sort([System.StringComparer]::Ordinal)

$sha = [System.Security.Cryptography.SHA256]::Create()
$buf = [System.IO.MemoryStream]::new()
foreach ($rel in $rels) {
    $nameBytes = [System.Text.Encoding]::UTF8.GetBytes($rel)
    $buf.Write($nameBytes, 0, $nameBytes.Length)
    $content = [System.IO.File]::ReadAllBytes((Join-Path $root $rel))
    $buf.Write($content, 0, $content.Length)
}
$buf.Position = 0
$rev = ([System.BitConverter]::ToString($sha.ComputeHash($buf)) -replace '-', '').ToLower().Substring(0, 16)
$buf.Dispose(); $sha.Dispose()

# ── Write the revision OPA will echo back via data.revision.revision ─────────
$revPath = Join-Path $root 'policy\revision\data.json'
$json = @{ revision = $rev } | ConvertTo-Json -Compress
[System.IO.File]::WriteAllText($revPath, $json + "`n")

# ── Build ────────────────────────────────────────────────────────────────────
$out = Join-Path $root "dist\bundle-$rev.tar.gz"
& $opa build -b policy/ -o $out --revision $rev
if ($LASTEXITCODE -ne 0) { Write-Error "opa build failed ($LASTEXITCODE)" }

Write-Host "pinned $rev  ->  dist/bundle-$rev.tar.gz" -ForegroundColor Green
Write-Host "  git add dist/bundle-$rev.tar.gz policy/revision/data.json" -ForegroundColor DarkGray
