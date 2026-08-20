#!/usr/bin/env bash
#
# Is the Azure deployment actually working?
#
# Runbook §6 lists these checks individually. This runs them in one go, in
# increasing order of cost, and prints a summary rather than stopping at the
# first failure — when something is wrong you want the whole picture, not the
# first symptom.
#
#   ./scripts/health-check.sh          the fast checks (about 20 seconds)
#   ./scripts/health-check.sh --full   also runs the 23 live Azure tests
#
# **A note on what "up" means here.** The dashboard returns HTTP 200 even when
# it cannot reach the database — Streamlit serves its shell and then streams an
# error banner over a websocket, where curl cannot see it. That is not a
# hypothetical: both images once ran for hours in exactly that state, reporting
# 200 the whole time. So the HTTP check below is labelled as what it is, and the
# question it cannot answer is answered by the SQL check instead.

set -uo pipefail

SUBSCRIPTION="54d2e1cd-805a-4c5e-ac6f-25932378fcd3"
RESOURCE_GROUP="rg-h1b"
URL="https://h1b-web.calmwave-8f560d92.canadacentral.azurecontainerapps.io"
PYTHON="${PYTHON:-.venv/bin/python}"

full=0
[ "${1:-}" = "--full" ] && full=1

pass=0
fail=0
skip=0

ok()   { printf '  \033[32mOK\033[0m    %s\n' "$1"; pass=$((pass + 1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; fail=$((fail + 1)); }
warn() { printf '  \033[33mSKIP\033[0m  %s\n' "$1"; skip=$((skip + 1)); }

need() { command -v "$1" >/dev/null 2>&1; }

echo
echo "H-1B Tech Salary Explorer — health check"
echo "========================================"

# ---------------------------------------------------------------- local code
echo
echo "Local"
if [ -x "$PYTHON" ]; then
  if "$PYTHON" -m pytest -q -m "not azure" >/tmp/h1b-pytest.log 2>&1; then
    ok "offline tests: $(tail -1 /tmp/h1b-pytest.log)"
  else
    bad "offline tests failed — see /tmp/h1b-pytest.log"
  fi
  "$PYTHON" -m ruff check . >/dev/null 2>&1 && ok "ruff clean" || bad "ruff found problems"
else
  warn "no interpreter at $PYTHON (set PYTHON=... to override)"
fi

# ------------------------------------------------------------------- GitHub
echo
echo "CI/CD"
if need gh; then
  run=$(gh run list --branch main --limit 1 --json conclusion,displayTitle --jq '.[0] | "\(.conclusion // "running")|\(.displayTitle)"' 2>/dev/null)
  case "${run%%|*}" in
    success) ok "last deploy: ${run#*|}" ;;
    running) warn "a deploy is still running: ${run#*|}" ;;
    "")      warn "could not read workflow runs (gh not authenticated?)" ;;
    *)       bad "last deploy ${run%%|*}: ${run#*|}" ;;
  esac
else
  warn "gh not installed"
fi

# -------------------------------------------------------------------- Azure
echo
echo "Azure"
if need az && az account show >/dev/null 2>&1; then
  spend=$(az consumption budget show --budget-name h1b-zero-spend --subscription "$SUBSCRIPTION" \
            --query "currentSpend.amount" -o tsv 2>/dev/null)
  if [ -n "$spend" ]; then
    # Any non-zero spend is a finding: the intended steady state is exactly 0.
    if [ "${spend%.*}" = "0" ]; then ok "spend: $spend CAD"; else bad "spend: $spend CAD — see runbook §8"; fi
  else
    warn "could not read the budget"
  fi

  rev=$(az containerapp revision list -g "$RESOURCE_GROUP" -n h1b-web --subscription "$SUBSCRIPTION" \
          --query "sort_by([], &properties.createdTime)[-1].{name:name,state:properties.provisioningState,active:properties.active}" -o tsv 2>/dev/null)
  case "$rev" in
    *Provisioned*True*) ok "web revision: $(echo "$rev" | cut -f1) provisioned and active" ;;
    "")                 warn "could not read web revisions" ;;
    *)                  bad "web revision not healthy: $rev" ;;
  esac

  job=$(az containerapp job execution list -g "$RESOURCE_GROUP" -n h1b-etl --subscription "$SUBSCRIPTION" \
          --query "sort_by([], &properties.startTime)[-1].{name:name,status:properties.status}" -o tsv 2>/dev/null)
  case "$job" in
    *Succeeded*) ok "last ETL run: $(echo "$job" | cut -f1) succeeded" ;;
    *Running*)   warn "an ETL run is in progress: $(echo "$job" | cut -f1)" ;;
    "")          warn "the ETL job has never run" ;;
    *)           bad "last ETL run: $job" ;;
  esac
else
  warn "az not installed or not logged in — skipping every Azure check"
fi

# ----------------------------------------------------------------- the site
echo
echo "Dashboard"
code=$(curl -s -o /tmp/h1b-page.html -w '%{http_code}' --max-time 90 "$URL/" 2>/dev/null)
if [ "$code" = "200" ] && grep -q '<title>Streamlit</title>' /tmp/h1b-page.html; then
  # Deliberately not called "the dashboard works". It proves the ingress reaches
  # our container and Streamlit served its document. The figures arrive over a
  # websocket and are never in this HTML.
  ok "serving its document (HTTP 200) — does NOT prove the database answered"
else
  bad "the site returned $code, or the body is not the Streamlit app"
fi

# --------------------------------------------------------------- Azure SQL
echo
echo "Azure SQL"
echo "  (a first connection after an idle hour takes ~60s while the serverless"
echo "   database resumes — that is the mechanism keeping cost at zero, not a fault)"
if [ -x "$PYTHON" ]; then
  if "$PYTHON" - <<'PY' 2>/tmp/h1b-sql.log
import sys
from src.db.azure_impl import connect_awake

EXPECTED = {"filings": 850321, "employers": 43573, "titles": 123990,
            "locations": 8570, "occupations": 63, "visa_classes": 4}

cursor = connect_awake(timeout=180, wait=10).cursor()
wrong = []
for table, want in EXPECTED.items():
    cursor.execute(f"SELECT COUNT(*) FROM dbo.{table}")
    got = cursor.fetchone()[0]
    if got != want:
        wrong.append(f"{table} {got:,} != {want:,}")
if wrong:
    print("; ".join(wrong), file=sys.stderr)
    sys.exit(1)
PY
  then
    ok "all six row counts match, from a real query"
  else
    bad "$(cat /tmp/h1b-sql.log | tail -3)"
  fi
else
  warn "no interpreter — skipping"
fi

# ------------------------------------------------------------- the live suite
if [ "$full" = "1" ]; then
  echo
  echo "Live Azure tests (--full)"
  if [ -x "$PYTHON" ]; then
    # REQUIRE_AZURE turns every skip into a failure. Without it this suite can
    # report success while testing nothing at all.
    if REQUIRE_AZURE=1 "$PYTHON" -m pytest -q -m azure >/tmp/h1b-azure.log 2>&1; then
      ok "$(tail -1 /tmp/h1b-azure.log)"
    else
      bad "live tests failed — see /tmp/h1b-azure.log"
    fi
  else
    warn "no interpreter — skipping"
  fi
else
  echo
  echo "  (run with --full to also execute the 23 live Azure tests)"
fi

echo
echo "========================================"
printf '%d passed, %d failed, %d skipped\n\n' "$pass" "$fail" "$skip"
[ "$fail" -eq 0 ]
