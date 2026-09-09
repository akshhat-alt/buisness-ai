# Business AI

An AI assistant trained only on a local business's own content — answers
customers' questions with citations, honestly abstains when it doesn't
know, captures leads, and hands off to a human on WhatsApp when needed.

Built as a separate, isolated project. It reuses proven architectural
patterns from Shri AI (fail-closed tenant isolation, grounded RAG with an
abstention gate, SSRF-safe ingestion, atomic quota enforcement) but has
its own codebase, database, and configuration — no shared runtime state,
no shared secrets, no shared deploy.

## What v1 does

- **Business knowledge ingestion**: a business owner adds their website
  URL or uploads a PDF/text file (FAQ, menu, brochure). Content is
  chunked and embedded into a per-tenant vector store.
- **Customer-facing chat widget**: a real customer — with no login, no
  account — asks a question and gets a grounded answer with a citation,
  or an honest "I don't know, let me connect you with someone" when the
  knowledge base doesn't cover it.
- **Lead capture**: a customer can leave a phone/email; leads are scoped
  per business and visible on the owner's dashboard.
- **Conversation analytics**: total questions, answer rate, buying-intent
  signals, suggested handoffs, and recent knowledge gaps (questions the
  assistant couldn't answer — a prioritized to-do list for what to add
  next).
- **WhatsApp handoff**: when the assistant suggests a human follow-up, the
  customer gets a click-to-chat WhatsApp link to the business's number.
- **Platform admin**: a lightweight surface to activate a newly signed-up
  business (after they've added at least one knowledge source) or
  suspend one.

## What V1.1 adds

- **Owner daily digest email**: a scheduled job (triggered by an external
  cron hitting `POST /api/v1/admin/digest/run`) emails each active
  business's owner a summary of new leads, questions asked/answered, and
  open knowledge gaps in the last 24h. Skipped (not sent) for a business
  with zero activity, so a quiet day doesn't train the owner to ignore it.
  Requires `RESEND_API_KEY`/`DIGEST_FROM_EMAIL` — optional, skipped
  gracefully if unset.
- **Internal staff assistant**: a chat panel on the owner dashboard, same
  `/api/ask` endpoint, for the owner/staff to ask their own knowledge base
  questions — no customer-facing exposure, no new backend surface.
- **Knowledge-gap closer**: each logged gap gets a "Draft Answer with AI"
  button (a safe, placeholder-filled template — never a guessed fact, see
  `generation.draft_faq_answer`) that the owner edits and publishes
  directly into the knowledge base with one click.
- **Real-time dissatisfaction alert**: a customer message showing genuine
  frustration or a complaint emails the owner immediately, not batched
  into tomorrow's digest — loss prevention, not just reporting. Detected
  independently of the grounded-answer path (`generation.
  classify_dissatisfaction`), since a real complaint rarely matches FAQ
  content and would otherwise trip the pre-LLM abstention gate before
  the signal is ever extracted.
- **Owner action-brief**: the digest includes 0-3 LLM-generated, data-
  grounded recommendations (`generation.generate_action_brief`) — e.g.
  flagging a question asked more than once as recurring unmet demand.
  Gated in code (not just prompted) to return nothing rather than padded
  generic advice when the period's activity doesn't support a real
  recommendation.
- **Review requests**: the owner marks a lead as serviced and one click
  sends that customer a review-link request — on WhatsApp if the lead
  came from WhatsApp, by email otherwise.

## What V1.2 adds: WhatsApp as a core channel

A real customer can now message the business's own WhatsApp number
directly and get the exact same grounded, cited answers as the website
widget — same knowledge base, same lead capture, same real-time
complaint alert, same daily digest. See `whatsapp.py` and the
`_process_question` helper in `app.py` (the one pipeline both channels
share) for the implementation, and "WhatsApp setup" below to connect a
number.

- **Inbound**: `POST /api/whatsapp/webhook` receives Meta Cloud API
  events, verifies the `X-Hub-Signature-256` HMAC, routes to the owning
  tenant by `phone_number_id` (never anything client-suppliable), and
  answers through the same RAG pipeline as `/api/ask`.
- **Lead capture, upgraded**: the customer's real, Meta-verified phone
  number is captured as a lead on first contact — no manual form needed,
  unlike the website widget's optional self-typed fields.
- **Complaint alerts, unchanged**: dissatisfaction detection and the
  instant owner email alert are channel-agnostic — they already just
  work over WhatsApp because both channels call the same
  `_process_question` helper.
- **Follow-ups, extended**: `POST /api/leads/{id}/request-review` sends
  over WhatsApp for a WhatsApp-sourced lead, with a clear, honest error
  if Meta's 24-hour customer-service window has closed (sending after
  that requires a pre-approved message template, not built in V1.2).
- **Redelivery-safe**: Meta may redeliver the same webhook event; a
  small idempotency table (`WhatsAppInboxStore`) keyed on Meta's own
  message id guarantees a customer is never answered — or billed
  quota — twice for one message.

## What V1.3 adds: five growth automations

All five run as `POST /api/v1/admin/{name}/run`, platform-admin-only,
meant to be hit once a day by an external cron — the exact same shape as
the digest endpoint above. Every one is best-effort per lead/tenant and
reuses the existing WhatsApp send path; nothing here adds a new
dependency or a new channel.

- **Missed-lead re-engagement** (`/reengagement/run`): a lead who left
  contact info is already a buying-intent signal — no analytics join
  needed. Any lead 48h–14 days old with no appointment and no prior
  nudge gets one WhatsApp follow-up. Free to run: the data already
  existed before this feature.
- **Appointment reminders** (`/reminders/run`): the owner sets a lead's
  `appointment_at` from the dashboard (`PUT /api/leads/{id}/appointment`
  — manual for now; live calendar booking is a deliberately deferred,
  much larger feature). A daily run sends a WhatsApp reminder ~24h
  before. Rescheduling automatically clears the old reminder marker.
- **Deposit / payment links** (`POST /api/leads/{id}/deposit-link`):
  generates a Razorpay payment link for the tenant's configured deposit
  amount and sends it over WhatsApp (or email as a fallback). Same
  "bring your own credential" shape as WhatsApp — the deposit lands
  directly in the tenant's own Razorpay account, never Business AI's.
- **Hindi / Hinglish support**: the system prompt now instructs the
  assistant to mirror the customer's language and script — Hindi,
  Hinglish, or English — inside the exact same grounded RAG pipeline.
  No new code path, no translation layer, no schema change.
- **Customer win-back** (`/winback/run`): scoped to WhatsApp-sourced
  leads, since a WhatsApp `session_id` is stable per phone number
  forever (see V1.2) — that Lead row already *is* a durable customer
  record. If a lead's last known appointment is older than the tenant's
  configured (or platform-default) win-back threshold, one WhatsApp
  nudge goes out; rebooking and lapsing again makes them eligible again.

## What v1 deliberately does not do

Not a CRM, not a website builder, not a workflow-automation platform. No
billing. No customer-facing email (only the owner-facing digest above —
no password-reset flow, no email support inbox). No multi-instance/
distributed mode — single SQLite + local Chroma, correct for one Railway
instance. No WhatsApp *outbound* message templates (business-initiated
messages outside the 24-hour customer-service window) — that needs
Meta template approval per tenant, out of scope for V1.2. These are
scoped omissions, not oversights: the goal is the smallest product a
real local business would actually pay for, not "everything a business
could ever want."

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design, module
layout, and the specific patterns reused from Shri AI (and why).

## Local development

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env   # fill in OPENAI_API_KEY, JWT_SECRET_KEY, ADMIN_SECRET
uvicorn business_ai.app:create_app --factory --reload --app-dir src
```

Then open `http://localhost:8000`.

To activate a newly signed-up business as platform admin: sign in at
`/login` with any email-shaped value (e.g. `admin@yourcompany.com`) and
the `ADMIN_SECRET` from your `.env` as the password.

To send the owner digest daily in production, schedule an external cron
(Railway's Cron Jobs, or a scheduled GitHub Actions workflow) to call
`POST /api/v1/admin/digest/run` once a day with a platform-admin bearer
token — there's no in-process scheduler in this app.

## WhatsApp setup

Business AI runs **one shared Meta App** (and one webhook URL) for every
tenant; each business connects **their own** WhatsApp Business phone
number. Nothing here requires Meta's heavier "Tech Provider / Embedded
Signup" approval — this is the standard Cloud API developer flow anyone
can complete in ~10 minutes.

**Platform operator, once (this deployment):**
1. Create a Meta App at [developers.facebook.com/apps](https://developers.facebook.com/apps)
   (type: Business) and add the **WhatsApp** product.
2. Set `WHATSAPP_APP_SECRET` (from the app's Basic Settings) and
   `WHATSAPP_VERIFY_TOKEN` (any random string you choose) in this
   deployment's environment.
3. In the app's WhatsApp → Configuration screen, set the webhook URL to
   `https://<your-domain>/api/whatsapp/webhook` and the verify token to
   the same `WHATSAPP_VERIFY_TOKEN` value, then subscribe to the
   `messages` field.

**Each tenant (business owner), from their own dashboard:**
1. In their own Meta for Developers account (or the platform operator's,
   for a pilot customer without one yet), add a phone number under the
   same WhatsApp product and generate a **permanent access token** for
   it (System User token with `whatsapp_business_messaging` permission).
2. Paste the number's **Phone Number ID** and the **access token** into
   Business AI's dashboard → WhatsApp Assistant section.
3. Message that WhatsApp number from any phone — the assistant replies
   using the business's own knowledge base within seconds.

A new Meta test number can only message pre-verified numbers and is
capped at low volume — fine for a pilot, not for launch. Moving a
number to production requires Meta Business verification, which is the
business owner's own step, outside this app.

## Tests

```bash
pytest
```

89 tests covering the full HTTP lifecycle (signup → ingest → activate →
grounded ask → quota → leads → analytics → tenant isolation), the
knowledge-gap closer (draft → publish → gap resolves → assistant answers
from the new FAQ entry), owner digest (sends only to active tenants with
activity, includes the action brief, skips gracefully when
unconfigured), the real-time dissatisfaction alert and review-request
flows (including tenant isolation), the WhatsApp channel (webhook
signature verification, tenant routing by phone_number_id, redelivery
idempotency, shared RAG/lead/dissatisfaction pipeline reuse, the
24-hour-window follow-up error path), the five growth automations
(missed-lead re-engagement windows, appointment reminders + reschedule
resetting the reminder marker, deposit links checking deliverability
before creating a Razorpay link, the language-mirroring system-prompt
contract, and win-back's per-tenant threshold + re-lapse eligibility),
the `authorize()` chokepoint, chunking edge cases, usage-limiter
behavior, and the SSRF guard. All offline — no OpenAI cost, no real Meta
or Razorpay API calls — using a deterministic fake generator, a
word-overlap fake embedding provider that still exercises the real
evidence-gate confidence threshold, and fake WhatsApp/Razorpay clients
that capture calls instead of hitting real APIs.

## Known limitations (v1, honestly stated)

- **Quota/rate-limiting is per session_id, not per verified identity.** A
  customer can technically get a fresh quota by starting a new chat
  session. This mirrors the accepted risk profile of the pattern it's
  adapted from — it's an abuse deterrent, not a hard billing cap.
- **No password-reset flow.** V1.1 wires up transactional email for the
  owner digest, but a reset-token flow is a distinct feature (token
  generation/expiry, a reset-password page) not yet built.
- **Single Railway instance.** SQLite + local Chroma. Correct for the
  current scale; would need real infra work (managed Postgres, a hosted
  vector DB, horizontal scaling) before this could serve many
  high-traffic tenants concurrently.
- **A tenant's WhatsApp access token is stored in plaintext in
  `tenants.db`**, the same trust model as the rest of this app's SQLite
  data (filesystem/volume-level protection, no application-level
  encryption anywhere yet). This is a new class of risk versus everything
  else stored there — it's a real third-party bearer credential, not
  app-internal state — and is the first thing to harden (e.g. field-level
  encryption keyed by a platform secret, or a real secrets manager)
  before onboarding tenants beyond a small, trusted pilot group.
- **`find_by_whatsapp_phone_number_id` is a linear scan** over all
  tenants. Correct and simple at pilot-phase tenant counts; would need a
  real index if tenant volume ever made this a bottleneck.
- **No outbound message templates.** A follow-up sent more than 24 hours
  after the customer's last WhatsApp message fails with a clear error
  instead of sending — Meta requires a pre-approved template for that,
  which needs per-tenant setup in Meta Business Manager, not built here.
- **A tenant's Razorpay secret is stored in plaintext**, same trust
  model/limitation as the WhatsApp access token above.
- **No payment-status webhook.** A deposit link is sent and tracked as
  "sent", not "paid" — confirming an actual payment would need a
  Razorpay webhook (signature verification, order reconciliation), which
  is real additional scope not built for V1.3. The owner checks payment
  status in their own Razorpay dashboard for now.
- **Appointments are single-timezone (IST), entered manually.** There's
  no per-tenant timezone config and no live calendar/slot booking yet —
  the owner types a date/time into the dashboard, which is treated as
  the business's own IST wall-clock time. Correct for the current
  India-only pilot; would need real work to serve other timezones.
- **Win-back only recognizes WhatsApp-sourced customers.** A WhatsApp
  `session_id` is stable per phone number forever, so that Lead row
  already acts as a durable customer record; a website-chat
  `session_id` is random per page load, so there's no reliable way yet
  to recognize the same person returning to the site across visits.
- **Missed-lead re-engagement and appointment reminders assume WhatsApp
  is connected.** A tenant without a connected WhatsApp number is
  skipped (reported, not silently dropped) rather than falling back to
  email — email nudges for these two specifically weren't built, since
  WhatsApp's open rate is the entire premise of the feature.
