#!/usr/bin/env bash
# Rebuild the evidence ledger. `demo` writes the ~3 MB two-bundle file that is
# committed; `full` writes the complete ~23 MB run behind the reported figures.
#
#     ./scripts/regenerate_ledger.sh [demo|full]
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec uv run python scripts/regenerate_ledger.py --mode "${1:-demo}"
