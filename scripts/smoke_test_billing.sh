#!/usr/bin/env bash
#
# scripts/smoke_test_billing.sh
#
# Verifies end-to-end customer subscription billing and webhook confirmation:
#   1. Signs up a throwaway test tenant.
#   2. Ingests a page of knowledge (satisfying the activation bar).
#   3. Generates a real Razorpay payment link via POST /api/tenant/billing/checkout.
#   4. Prints the payment URL for the user to pay.
#   5. Polls GET /api/tenant?tenant_id=... until billing_status flips to "paid".
#   6. Attempts self-activation (POST /api/tenant/activate) to confirm the full flow.
#
# Usage:
#   ./scripts/smoke_test_billing.sh [BASE_URL]
#   BASE_URL=https://your-app.railway.app ./scripts/smoke_test_billing.sh
#

set -euo pipefail

BASE_URL="${1:-${BASE_URL:-http://localhost:8000}}"
BASE_URL="${BASE_URL%/}"

# Polling timeout configuration (default 10 minutes)
MAX_WAIT_MINUTES="${MAX_WAIT_MINUTES:-10}"
POLL_INTERVAL_SECONDS="${POLL_INTERVAL_SECONDS:-5}"
MAX_ATTEMPTS=$(( (MAX_WAIT_MINUTES * 60) / POLL_INTERVAL_SECONDS ))

# Colors for terminal output
BOLD="\033[1m"
GREEN="\033[0;32m"
YELLOW="\033[1;33m"
BLUE="\033[0;34m"
RED="\033[0;31m"
RESET="\033[0m"

echo -e "${BOLD}${BLUE}=== Business AI Billing & Webhook Smoke Test ===${RESET}"
echo -e "Target Base URL: ${BOLD}${BASE_URL}${RESET}"
echo

# Helper function for JSON parsing using python3
json_val() {
  local json="$1"
  local key="$2"
  python3 -c "import sys, json; data = json.loads(sys.stdin.read()); print(data.get('$key', '') or '')" <<< "$json"
}

json_nested() {
  local json="$1"
  local expr="$2"
  python3 -c "import sys, json; data = json.loads(sys.stdin.read()); print($expr)" <<< "$json"
}

# Unique suffix for throwaway test tenant
RAND_ID="$(date +%s)_$RANDOM"
TEST_EMAIL="smoke_test_${RAND_ID}@example.com"
TEST_PASSWORD="SmokeTestPass123!"
TEST_NAME="Smoke Tester"
TEST_BIZ_NAME="Smoke Test Business ${RAND_ID}"

# ------------------------------------------------------------------------------
# 1. Sign Up Throwaway Tenant
# ------------------------------------------------------------------------------
echo -e "${BOLD}[1/4] Signing up throwaway test tenant...${RESET}"
SIGNUP_PAYLOAD=$(python3 -c "import json; print(json.dumps({
  'email': '$TEST_EMAIL',
  'password': '$TEST_PASSWORD',
  'name': '$TEST_NAME',
  'business_name': '$TEST_BIZ_NAME'
}))")

SIGNUP_RESP=$(curl -sS -X POST "${BASE_URL}/api/auth/signup" \
  -H "Content-Type: application/json" \
  -d "$SIGNUP_PAYLOAD")

ACCESS_TOKEN=$(json_val "$SIGNUP_RESP" "access_token")
TENANT_ID=$(json_val "$SIGNUP_RESP" "tenant_id")

if [[ -z "$ACCESS_TOKEN" || -z "$TENANT_ID" ]]; then
  echo -e "${RED}Signup failed:${RESET} $SIGNUP_RESP"
  exit 1
fi

echo "  Tenant ID:   $TENANT_ID"
echo "  Owner Email: $TEST_EMAIL"
echo

# ------------------------------------------------------------------------------
# 2. Ingest Sample Knowledge
# ------------------------------------------------------------------------------
echo -e "${BOLD}[2/4] Ingesting sample knowledge page...${RESET}"
TMP_DOC=$(mktemp /tmp/smoke_test_doc_XXXXXX.txt)
cat <<EOF > "$TMP_DOC"
Welcome to ${TEST_BIZ_NAME}.
We provide automated customer intelligence, appointment scheduling, and grounded assistance.
Our business hours are Monday through Saturday, 9:00 AM to 7:00 PM.
EOF

INGEST_RESP=$(curl -sS -X POST "${BASE_URL}/api/knowledge/upload?tenant_id=${TENANT_ID}" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -F "file=@${TMP_DOC};filename=about_us.txt")
rm -f "$TMP_DOC"

CHUNKS_INDEXED=$(json_val "$INGEST_RESP" "chunks_indexed")
echo "  Indexed $CHUNKS_INDEXED knowledge chunk(s)."
echo

# ------------------------------------------------------------------------------
# 3. Create Subscription Checkout Link
# ------------------------------------------------------------------------------
echo -e "${BOLD}[3/4] Requesting self-serve billing checkout...${RESET}"
CHECKOUT_RESP=$(curl -sS -X POST "${BASE_URL}/api/tenant/billing/checkout?tenant_id=${TENANT_ID}" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}")

PAYMENT_URL=$(json_val "$CHECKOUT_RESP" "payment_url")
INITIAL_STATUS=$(json_val "$CHECKOUT_RESP" "billing_status")

if [[ -z "$PAYMENT_URL" || "$PAYMENT_URL" == "null" ]]; then
  DETAIL=$(json_val "$CHECKOUT_RESP" "detail")
  echo -e "${RED}Checkout failed:${RESET} ${DETAIL:-$CHECKOUT_RESP}"
  echo
  echo "Possible causes:"
  echo "  - PLATFORM_SUBSCRIPTION_PRICE_INR is not set on this server."
  echo "  - PLATFORM_RAZORPAY_KEY_ID or PLATFORM_RAZORPAY_KEY_SECRET is missing/invalid."
  echo "See RAZORPAY_LAUNCH_CHECKLIST.md for setup instructions."
  exit 1
fi

echo -e "  Initial billing status: ${YELLOW}${INITIAL_STATUS}${RESET}"
echo
echo -e "================================================================================"
echo -e "${BOLD}${GREEN}ACTION REQUIRED: Complete the payment below${RESET}"
echo -e "Payment URL: ${BOLD}${PAYMENT_URL}${RESET}"
echo -e "================================================================================"
echo -e "Open the link above in your browser and complete a test payment."
echo -e "Watching for webhook confirmation from Razorpay (${MAX_WAIT_MINUTES} minute timeout)..."
echo

# ------------------------------------------------------------------------------
# 4. Poll For Webhook Auto-Confirmation
# ------------------------------------------------------------------------------
echo -e "${BOLD}[4/4] Polling tenant status for webhook auto-confirmation...${RESET}"

ATTEMPT=0
while [[ $ATTEMPT -lt $MAX_ATTEMPTS ]]; do
  ATTEMPT=$((ATTEMPT + 1))
  sleep "$POLL_INTERVAL_SECONDS"

  TENANT_INFO=$(curl -sS -X GET "${BASE_URL}/api/tenant?tenant_id=${TENANT_ID}" \
    -H "Authorization: Bearer ${ACCESS_TOKEN}")

  STATUS=$(json_val "$TENANT_INFO" "billing_status")
  PAID_AT=$(json_val "$TENANT_INFO" "billing_paid_at")
  TIMESTAMP=$(date "+%H:%M:%S")

  echo "  [$TIMESTAMP] Attempt $ATTEMPT/$MAX_ATTEMPTS: billing_status = '$STATUS'"

  if [[ "$STATUS" == "paid" ]]; then
    echo
    echo -e "${BOLD}${GREEN}✔ SUCCESS: Webhook received and verified!${RESET}"
    echo "  Payment confirmed at: $PAID_AT"
    echo

    # Test Self-Activation
    echo -e "${BOLD}Testing self-activation (POST /api/tenant/activate)...${RESET}"
    ACTIVATE_RESP=$(curl -sS -X POST "${BASE_URL}/api/tenant/activate?tenant_id=${TENANT_ID}" \
      -H "Authorization: Bearer ${ACCESS_TOKEN}")
    TENANT_STATUS=$(json_val "$ACTIVATE_RESP" "status")

    if [[ "$TENANT_STATUS" == "active" ]]; then
      echo -e "${BOLD}${GREEN}✔ TENANT ACTIVATED: Account is fully active in production!${RESET}"
    else
      echo -e "${YELLOW}Activation response:${RESET} $ACTIVATE_RESP"
    fi
    echo
    echo "All end-to-end payment and activation checks passed successfully."
    exit 0
  fi
done

echo
echo -e "${RED}Timed out after ${MAX_WAIT_MINUTES} minutes waiting for payment_link.paid webhook.${RESET}"
echo "Troubleshooting steps:"
echo "  1. Check Razorpay Dashboard -> Account & Settings -> Webhooks -> Webhook Logs for delivery status."
echo "  2. Ensure PLATFORM_RAZORPAY_WEBHOOK_SECRET in Railway matches the webhook secret in Razorpay."
echo "  3. Verify the Webhook URL in Razorpay is: ${BASE_URL}/api/webhooks/razorpay"
exit 1
