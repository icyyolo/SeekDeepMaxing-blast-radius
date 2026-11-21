#!/usr/bin/env bash
# Run sqlglot's test suite once at the pinned commit with per-test coverage
# contexts and fixture-read tracing. Output lands in $WORK/raw/.
# Usage: tools/collect.sh [path-to-sqlglot-checkout]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SQLGLOT="${1:-$HOME/br-work/sqlglot}"
WORK="$HOME/br-work"
rm -rf "$WORK/raw" && mkdir -p "$WORK/raw"

cp "$HERE/../requirements.lock" "$HERE/requirements.lock"
docker build -q -t br-map -f "$HERE/Dockerfile.map" "$HERE"
rm "$HERE/requirements.lock"

docker run --rm \
  -v "$SQLGLOT":/src:ro -v "$WORK/raw":/out -v "$HERE":/tools:ro \
  -e SKIP_INTEGRATION=0 -e BR_ROOT=/src -e BR_OUT=/out \
  -e PYTHONPATH=/src:/tools -e COVERAGE_FILE=/out/.coverage \
  -w /src br-map \
  python -m pytest tests -q -p br_trace -p no:cacheprovider -n 12 \
    --cov=/src/sqlglot --cov=/src/tests --cov-context=test --cov-report= \
    -o addopts= --timeout=600 \
  > "$WORK/raw/pytest.log" 2>&1 || echo "pytest exit $? (failures expected only if env differs)"
tail -5 "$WORK/raw/pytest.log"
