#!/usr/bin/env bash
# Praman — attest the committed evidence ledger. One command from a fresh clone.
#
#     ./scripts/verify.sh
#
# Fetches the OPA binary into tools/ if it is missing, then recomputes the hash
# chain AND re-POSTs every recorded policy input to OPA loaded with the exact
# bundle that authorised it. Two independent checks:
#
#     chain   nothing was changed after the fact
#     replay  the record was true when it was written
#
# The second is the one a hash chain cannot do. A writer holding the append path
# can bypass the policy engine and record its own verdict, and the chain stays
# perfect; only re-deriving the decision from the pinned bundle catches that.
#
# Nothing is downloaded outside this repo's own tools/ directory, and no Python
# environment has to be set up by hand -- uv builds it on demand.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

OPA_VERSION="1.19.1"
LEDGER="${1:-data/ledger.db}"

say() { printf '\033[36m%s\033[0m\n' "$*"; }
die() { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

# ── OPA ───────────────────────────────────────────────────────────────────────
if [ -x "tools/opa" ]; then
  OPA="tools/opa"
elif command -v opa >/dev/null 2>&1; then
  OPA="$(command -v opa)"
else
  case "$(uname -s)" in
    Linux)  os=linux ;;
    Darwin) os=darwin ;;
    *)      die "unsupported OS $(uname -s). Use scripts/verify.ps1 on Windows." ;;
  esac
  case "$(uname -m)" in
    x86_64|amd64) arch=amd64 ;;
    arm64|aarch64) arch=arm64 ;;
    *) die "unsupported architecture $(uname -m)" ;;
  esac
  # The linux builds are published as static binaries under a different name.
  asset="opa_${os}_${arch}"
  [ "$os" = "linux" ] && asset="${asset}_static"

  say "fetching OPA ${OPA_VERSION} into tools/ ..."
  mkdir -p tools
  curl -fsSL -o tools/opa "https://openpolicyagent.org/downloads/v${OPA_VERSION}/${asset}" \
    || die "could not download OPA. Check network access to openpolicyagent.org."
  chmod +x tools/opa
  OPA="tools/opa"
fi
say "opa: $("$OPA" version | head -1)"

# ── Python, via uv ────────────────────────────────────────────────────────────
command -v uv >/dev/null 2>&1 || die "uv is required. Install it with:
    curl -LsSf https://astral.sh/uv/install.sh | sh
It is the only prerequisite; it builds the Python environment on demand."

[ -f "$LEDGER" ] || die "no ledger at $LEDGER.
The committed one lives at data/ledger.db. To rebuild it:
    uv run python scripts/regenerate_ledger.py --mode demo"

# ── Attest ────────────────────────────────────────────────────────────────────
say "attesting $LEDGER ..."
echo
exec uv run --quiet praman verify --ledger "$LEDGER" --opa "$OPA" --require-replay
