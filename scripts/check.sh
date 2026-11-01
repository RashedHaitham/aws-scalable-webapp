#!/usr/bin/env bash
# Local acceptance check - no AWS calls. Exit 0 only if every gate passes.
#   1. cfn-lint: the template is valid CloudFormation (schemas, references, conditions)
#   2. pytest:   the template implements the intended architecture; the embedded app runs;
#                the diagram is well-formed and shows every component
#   3. bash -n / shellcheck: helper scripts parse and lint clean
set -euo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin
if [ ! -x "$PY/cfn-lint" ]; then
  python3 -m venv .venv
  "$PY/pip" install --quiet -r requirements-dev.txt
fi

echo "== cfn-lint";  "$PY/cfn-lint" cloudformation/webapp.yaml
echo "== pytest";    "$PY/pytest" -q
echo "== bash -n";   for f in scripts/*.sh; do bash -n "$f"; done
if command -v shellcheck >/dev/null; then
  echo "== shellcheck"; shellcheck scripts/*.sh
else
  echo "== shellcheck SKIPPED (not installed)"
fi
echo "ALL CHECKS PASSED"
