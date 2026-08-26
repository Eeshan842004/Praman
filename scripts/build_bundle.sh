#!/usr/bin/env bash
# Praman — build an immutable, committed, replayable policy bundle.
#
# Architectural law #6: every decision records the revision OPA REPORTS.
# For that to mean anything the revision must derive from policy CONTENT, and
# the bundle must be immutable and committed. dist/bundle-*.tar.gz is evidence,
# not a build artifact — `praman verify` replays historical decisions against
# the exact bundle that authorised them (mitigates S4).
#
# POSIX/CI counterpart of scripts/build_bundle.ps1. Both must produce the same
# revision for the same policy content.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

OPA="${OPA_BIN:-opa}"
command -v "$OPA" >/dev/null 2>&1 || OPA="$ROOT/tools/opa.exe"
command -v "$OPA" >/dev/null 2>&1 || { echo "opa not found" >&2; exit 1; }

mkdir -p dist

# Revision = sha256 over policy content, path-sorted for determinism.
# Two deliberate exclusions:
#   revision/data.json -- holds the revision itself; self-referential.
#   *_test.rego -- opa build excludes tests from the bundle, so they are not part
#     of what OPA evaluates. Including them would mint a NEW revision for a
#     test-only edit while runtime behaviour is byte-identical -- exactly the
#     noise law #6 exists to eliminate. The revision identifies the policy that
#     RAN, nothing else.
REV="$(
  find policy -type f \( -name '*.rego' -o -name '*.json' \) \
    ! -path 'policy/revision/data.json' \
    ! -name '*_test.rego' \
    | LC_ALL=C sort \
    | while IFS= read -r f; do printf '%s' "$f"; cat "$f"; done \
    | sha256sum | cut -c1-16
)"

printf '{"revision":"%s"}\n' "$REV" > policy/revision/data.json

"$OPA" build -b policy/ -o "dist/bundle-$REV.tar.gz" --revision "$REV"

echo "pinned $REV  ->  dist/bundle-$REV.tar.gz"
echo "  git add dist/bundle-$REV.tar.gz policy/revision/data.json"
