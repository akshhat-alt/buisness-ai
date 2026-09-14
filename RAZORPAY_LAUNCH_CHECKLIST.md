# Razorpay Launch & Verification Checklist

This checklist provides the exact, step-by-step instructions to configure and verify real customer payment collection and platform subscription activation for Business AI.

> [!IMPORTANT]
> **Scope Distinction**: This checklist covers **Business AI's own platform subscription billing** (Phase 8), which charges business owners to activate and use Business AI. This is distinct from a tenant's own customer deposit links (Phase 18), where tenants bring their own Razorpay credentials from their dashboard.

---

## 1. Railway Environment Configuration

Set the following environment variables in your **Railway Project Settings -> Variables** (or `.env` for local testing).

| Variable Name | Required | Description | Example / Format |
|---|---|---|---|
| `PLATFORM_RAZORPAY_KEY_ID` | Yes | Razorpay API Key ID from your Razorpay Dashboard. | `rzp_test_...` or `rzp_live_...` |
| `PLATFORM_RAZORPAY_KEY_SECRET` | Yes | Razorpay API Key Secret associated with the Key ID. | String (keep confidential) |
| `PLATFORM_RAZORPAY_WEBHOOK_SECRET` | Yes | Shared secret for verifying the `X-Razorpay-Signature` header HMAC-SHA256 digest on incoming webhooks. | Custom 32+ character random string |
| `PLATFORM_SUBSCRIPTION_PRICE_INR` | Yes | Self-serve onboarding price in whole INR. If unset or 0, checkout is disabled and tenants activate for free. | `499`, `999`, etc. |

> [!CAUTION]
> **MANUAL USER ACTION REQUIRED**:
> 1. Confirm that `PLATFORM_RAZORPAY_KEY_ID`, `PLATFORM_RAZORPAY_KEY_SECRET`, `PLATFORM_RAZORPAY_WEBHOOK_SECRET`, and `PLATFORM_SUBSCRIPTION_PRICE_INR` are entered in Railway.
> 2. Ensure `PLATFORM_RAZORPAY_WEBHOOK_SECRET` has no leading/trailing whitespace and matches the Razorpay dashboard configuration byte-for-byte.

---

## 2. Razorpay Dashboard Configuration

Follow these steps in your [Razorpay Dashboard](https://dashboard.razorpay.com/):

### Step 2.1: API Keys
1. In the Razorpay sidebar, go to **Account & Settings** -> **API Keys**.
2. Generate or copy your **Key Id** and **Key Secret**.
3. Copy **Key Id** into `PLATFORM_RAZORPAY_KEY_ID`.
4. Copy **Key Secret** into `PLATFORM_RAZORPAY_KEY_SECRET`.

### Step 2.2: Webhook Registration
1. In the Razorpay sidebar, go to **Account & Settings** -> **Webhooks**.
2. Click **+ Add New Webhook**.
3. Configure the fields:
   - **Webhook URL**:
     ```text
     https://<YOUR_RAILWAY_DOMAIN>/api/webhooks/razorpay
     ```
     *(Exact path registered in `src/business_ai/routers/webhook_routes.py:41`)*
   - **Secret**: Enter the exact same string set in `PLATFORM_RAZORPAY_WEBHOOK_SECRET`.
   - **Alert Email**: Enter your technical or operational email to receive notification if webhooks fail.
   - **Active Events**:
     Select the following under **Payment Link Events**:
     - `payment_link.paid` (**REQUIRED**: Auto-confirms subscription payment and marks tenant `billing_status="paid"`).
     - `payment_link.partially_paid` *(Supported: audit-logged)*
     - `payment_link.expired` *(Supported: audit-logged)*
     - `payment_link.cancelled` *(Supported: audit-logged)*
4. Click **Create Webhook**.

> [!CAUTION]
> **MANUAL USER ACTION REQUIRED**:
> - Verify that the webhook URL uses `https://` and points to `/api/webhooks/razorpay` (do not confuse with tenant deposit webhook `/api/webhooks/razorpay/{tenant_id}`).
> - Verify that `payment_link.paid` is selected.

---

## 3. End-to-End Verification with Smoke Test Script

Once the environment variables and webhook are saved, run the verification smoke-test script from your terminal:

```bash
# Make script executable (if needed)
chmod +x scripts/smoke_test_billing.sh

# Run against production (or staging)
./scripts/smoke_test_billing.sh https://<YOUR_RAILWAY_DOMAIN>
```

### What the Script Verifies:
1. **Tenant Signup**: Creates a throwaway test business (`POST /api/auth/signup`) and receives a JWT owner token.
2. **Knowledge Ingestion**: Ingests a sample knowledge source (`POST /api/knowledge/upload`) to clear the knowledge gate required for activation.
3. **Checkout Link Creation**: Calls `POST /api/tenant/billing/checkout` to generate a live Razorpay Payment Link carrying `reference_id = <tenant_id>`.
4. **Live Payment Waiting**: Prints the payment link. You can open the link in a browser and complete a small payment (in Test Mode with test cards/UPI, or a ₹1 / minimum live payment).
5. **Webhook Confirmation**: Polls `GET /api/tenant?tenant_id=<tenant_id>` every 5 seconds. As soon as Razorpay fires `payment_link.paid`, Business AI verifies the signature, updates `billing_status="paid"`, and records an audit log entry.
6. **Self-Activation**: Automatically attempts `POST /api/tenant/activate` once paid to confirm the tenant can immediately activate.

> [!CAUTION]
> **MANUAL USER ACTION REQUIRED**:
> - Run `scripts/smoke_test_billing.sh` yourself against the target deployment.
> - Open the generated payment link in your browser and complete the payment.
> - Observe the polling loop output confirm `billing_status: paid` and account activation.

---

## 4. Webhook Troubleshooting Guide

If the smoke test script creates the payment link but times out waiting for `billing_status: paid`:

1. **Check Razorpay Webhook Logs**:
   - Go to Razorpay Dashboard -> **Account & Settings** -> **Webhooks**.
   - Click on your registered webhook URL.
   - Inspect the **Webhook Log**. Check the HTTP status returned by Business AI:
     - `401 Unauthorized`: `PLATFORM_RAZORPAY_WEBHOOK_SECRET` in Railway does not match the Secret in Razorpay.
     - `404 Not Found`: Webhook URL is wrong. Ensure path is `/api/webhooks/razorpay`.
     - `200 OK` with `status: ignored`: Event was not `payment_link.paid` or `reference_id` was missing.
2. **Check Railway Application Logs**:
   - Filter logs for `POST /api/webhooks/razorpay`.
   - Look for `"Platform subscription payment auto-confirmed for tenant <tenant_id>"` or warning messages.
