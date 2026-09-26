#!/usr/bin/env bash
# Lint and unit-test the alert rules in deploy/40-monitoring.yaml with promtool.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PROMTOOL="${PROMTOOL:-promtool}"
python3 - "$HERE" <<'PY'
import sys, yaml
here = sys.argv[1]
docs = [d for d in yaml.safe_load_all(open(f"{here}/deploy/40-monitoring.yaml")) if d and d["kind"] == "PrometheusRule"]
yaml.safe_dump({"groups": docs[0]["spec"]["groups"]}, open(f"{here}/tests/promql/rules.yaml", "w"), sort_keys=False)
PY
"$PROMTOOL" check rules "$HERE/tests/promql/rules.yaml"
"$PROMTOOL" test rules "$HERE/tests/promql/rules.test.yaml"
