#!/usr/bin/env bash
# Run sqlglot's test suite once with br_cmp.py tracing which comparisons
# would change result if is/==, is not/!=, </<=, >/>= were swapped.
# Output lands in $WORK/cmpraw/. Needs the br-map image (tools/collect.sh).
# Usage: tools/collect_cmp.sh [path-to-sqlglot-checkout]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SQLGLOT="${1:-$HOME/br-work/sqlglot}"
WORK="$HOME/br-work"
rm -rf "$WORK/cmpraw" && mkdir -p "$WORK/cmpraw"

docker run --rm   -v "$SQLGLOT":/src:ro -v "$WORK/cmpraw":/out -v "$HERE":/tools:ro   -e SKIP_INTEGRATION=0 -e BR_ROOT=/src -e BR_OUT=/out -e PYTHONPATH=/src:/tools   -w /src br-map   python -m pytest tests -q -p br_cmp -p br_trace -p no:cacheprovider -n 12     -o addopts= --timeout=1800 --durations=10   > "$WORK/cmpraw/pytest.log" 2>&1 || echo "pytest exit $? (failures expected only if env differs)"
tail -15 "$WORK/cmpraw/pytest.log"
