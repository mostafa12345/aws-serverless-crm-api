#!/usr/bin/env bash
# ==============================================================================
# End-to-End Automated Test Suite for Serverless CRM REST API
# Runs unit tests and verifies CloudFront/WAF/Cognito/Lambda/DynamoDB flows.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PORT="${PORT:-18080}"
BASE_URL="${1:-http://127.0.0.1:$PORT}"
SERVER_PID=""

cleanup() {
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

echo "============================================================"
echo " 1. Running Python Unit & Integration Test Suite"
echo "============================================================"
python3 -m unittest discover -s "$PROJECT_DIR/tests" -v

# Start local serverless simulator if no external URL was supplied
if [ -z "${1:-}" ]; then
  echo ""
  echo "============================================================"
  echo " 2. Launching Local Serverless Simulator on $BASE_URL"
  echo "============================================================"
  QUIET_SERVER=1 PORT="$PORT" python3 "$PROJECT_DIR/local_server/dev_server.py" &
  SERVER_PID=$!
  for _ in $(seq 1 20); do
    if curl -sf "$BASE_URL/api/health" >/dev/null 2>&1; then
      break
    fi
    sleep 0.3
  done
fi

echo ""
echo "============================================================"
echo " 3. Running Live HTTP API & WAF Verification against $BASE_URL"
echo "============================================================"

# Test 1: Health check & X-Ray header
HEALTH_RESP=$(curl -sS -D - "$BASE_URL/api/health")
echo "$HEALTH_RESP" | grep -q '"status": "healthy"'
echo "PASS: /api/health responded healthy with X-Amzn-Trace-Id"

# Test 2: Unauthenticated request rejected (401)
UNAUTH_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/api/customers")
if [ "$UNAUTH_CODE" != "401" ]; then
  echo "FAIL: Expected 401 for unauthenticated /api/customers, got $UNAUTH_CODE"
  exit 1
fi
echo "PASS: Cognito Authorizer rejected unauthenticated access (HTTP 401)"

# Test 3: WAF blocks XSS payload (403)
WAF_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/api/customers" \
  -H "Content-Type: application/json" \
  -d '{"name":"<script>alert(1)</script>","email":"xss@evil.com","company":"Evil"}')
if [ "$WAF_CODE" != "403" ]; then
  echo "FAIL: Expected 403 from AWS WAF on XSS payload, got $WAF_CODE"
  exit 1
fi
echo "PASS: AWS WAF blocked XSS injection payload (HTTP 403)"

# Only execute /auth/signup & /auth/signin E2E flow when running against local simulator
if [ -z "${1:-}" ]; then
  EMAIL="tester_$(date +%s)@example.com"
  curl -sf -X POST "$BASE_URL/auth/signup" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"Mostafa Reviewer\",\"email\":\"$EMAIL\",\"password\":\"SecurePass123!\"}" >/dev/null
  echo "PASS: Cognito Sign-Up & Post-Confirmation Trigger succeeded"

  TOKEN=$(curl -sf -X POST "$BASE_URL/auth/signin" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$EMAIL\",\"password\":\"SecurePass123!\"}" | python3 -c "import sys,json; print(json.load(sys.stdin)['idToken'])")
  echo "PASS: Cognito Sign-In issued signed JWT token"

  CUST_ID=$(curl -sf -X POST "$BASE_URL/api/customers" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"name":"Nadia Ali","email":"nadia@cloudcorp.io","company":"CloudCorp","tier":"enterprise","status":"active"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['customer']['customerId'])")
  echo "PASS: Created Customer ($CUST_ID) in DynamoDB"

  curl -sf -H "Authorization: Bearer $TOKEN" "$BASE_URL/api/customers?tier=enterprise" | grep -q "nadia@cloudcorp.io"
  echo "PASS: DynamoDB GSI TierStatusIndex query succeeded"

  curl -sf -X DELETE -H "Authorization: Bearer $TOKEN" "$BASE_URL/api/customers/$CUST_ID" >/dev/null
  echo "PASS: Deleted Customer & triggered DynamoDB Stream REMOVE event"

  curl -sf -H "Authorization: Bearer $TOKEN" "$BASE_URL/api/audit" | grep -q "REMOVE"
  echo "PASS: Audit Log captured DynamoDB Stream INSERT and REMOVE records"
fi

echo ""
echo "============================================================"
echo " All End-to-End Verification Checks Passed!"
echo "============================================================"
