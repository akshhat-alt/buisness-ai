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

## What V1.4 adds: WhatsApp as a true AI employee

Built directly from "The Employee Handbook" plan (research-first, then
implementation) — the two fast, fully-buildable wins, a live-validated
Hindi fix, and the testable half of the onboarding recommendation.

- **Owner WhatsApp alerts**: the real-time complaint alert and the daily
  digest are now also pushed to the owner's own WhatsApp (`TenantConfig.
  owner_whatsapp_number`), alongside the guaranteed email — neither
  channel gates the other. A business-initiated message outside Meta's
  24-hour customer-service window simply fails silently on the WhatsApp
  side (expected, not a bug); email stays authoritative.
- **"What needs my attention today?"**: any message from the owner's
  configured number, sent to the tenant's own WhatsApp line, gets an
  instant live status pull instead of the customer RAG pipeline — same
  content as the digest, computed on demand, no LLM call (so it's fast
  and free), no keyword parsing (any message means the same thing, since
  that number is for the owner, not customers). Doesn't create a lead or
  spend quota.
- **Hindi/Hinglish, live-validated**: ran real Hindi (Devanagari),
  Hinglish, and English questions against a real ingested knowledge base
  through the real OpenAI API (not a mock) before touching any code.
  Finding: Hinglish already retrieves fine; a Devanagari query scored
  0.12-0.21 pack_confidence against clearly-relevant English content
  (below the 0.35 abstention threshold every time), while the identical
  question translated to English scored 0.28-0.44 — a consistent
  +0.12-0.22 lift, confirmed on four separate questions before building
  anything. `generation.translate_to_english_for_retrieval` now
  normalizes a Devanagari query for the embedding lookup only; the
  original text still goes to the generation model, so the reply still
  mirrors the customer's Hindi. Also fixed the two fixed, pre-LLM
  fallback strings (the abstention message, WhatsApp's quota-busy
  message) that used to stay English even in an otherwise-Hindi
  conversation.
- **WhatsApp Embedded Signup — backend only**: `MetaEmbeddedSignupClient`
  (OAuth code exchange) and `POST /api/tenant/whatsapp/embedded-signup`
  are built and tested, gated behind `WHATSAPP_APP_ID` being configured.
  The frontend "Connect WhatsApp" button is deliberately NOT built yet —
  it needs a real Meta App ID and an approved Tech Provider registration
  to be testable at all, and shipping an untestable OAuth popup flow
  isn't "production-ready." The manual per-tenant connection flow (V1.2)
  remains the only active path until that registration completes.

### An unplanned but important finding

Validating Hindi retrieval surfaced something bigger: **even English
queries abstained more than expected** against a realistic multi-topic
knowledge base — e.g. "Where can I park?" scored 0.262 against content
that directly answers it, below the 0.35 threshold. This is a
general retrieval-confidence calibration question, not specific to
language, and out of scope for this pass — flagged here as a real
finding worth its own investigation, not quietly fixed alongside an
unrelated feature.

## What V1.5 adds: billing and self-serve activation

Two onboarding gaps closed, from the site/onboarding review: there was
no way to collect Business AI's own subscription revenue inside the
product, and every single new tenant needed a platform admin to
personally click "Activate."

- **Billing**: `POST /api/v1/admin/tenants/{id}/billing-link` generates
  a Razorpay payment link — using Business AI's *own* Razorpay account
  (`PLATFORM_RAZORPAY_KEY_ID/SECRET`, distinct from a tenant's own
  `razorpay_key_id/secret`, which collects deposits from *that tenant's*
  customers) — and emails it to the owner. Same "send a link, confirm
  manually" shape as every other payment feature here: there's no
  webhook, so `POST .../mark-paid` is how you record that you checked
  your own Razorpay dashboard and got paid. A tenant with no price ever
  set on them is completely unaffected — this is opt-in per tenant, not
  a blanket paywall.
- **Self-serve activation**: `POST /api/tenant/activate` lets the owner
  activate their own tenant once they've cleared the exact same bar an
  admin already had to check — a knowledge source ingested, and paid if
  a price was set. Admin activation still works unchanged for anyone who
  wants to do it by hand; `suspend` remains the way to pull back a
  tenant that shouldn't have gone live. The dashboard now shows an
  "Activate Now" button directly to the owner instead of a silent wait,
  and the admin panel gained inline billing controls (send a link, mark
  paid) per tenant.

## What V1.6 adds: the admin WhatsApp bot (Phase 0 — employee coordination)

The first piece of a second, structurally distinct bot on the *same*
WhatsApp number: everything above is for a business's *customers*; this
is for the business's *owner, managers, and employees* to run the
business itself. A message from a number on the tenant's employee
roster is routed here instead of the customer RAG pipeline — no lead is
created, no question quota is spent, and the customer-facing assistant
is completely unaffected either way.

- **Employee roster**: `POST/GET /api/employees`, `PUT
  /api/employees/{id}` (owner-only — `MANAGE_EMPLOYEES`) manage a
  tenant-scoped roster of WhatsApp numbers, each with a name and a role
  (owner/manager/staff). A tenant's existing `owner_whatsapp_number` is
  auto-registered into the roster on first use, so nothing breaks for a
  tenant that predates this feature.
- **New role: `manager`** — day-to-day operating power (assign/approve
  tasks, view reports and tasks) without owner-only levers (knowledge
  base, assistant config, billing, the roster itself). `authorize()`'s
  role lookup was also hardened here: an unrecognized role now gets zero
  actions (fail-closed), instead of silently falling back to staff-level
  access.
- **WhatsApp task commands** (deterministic keyword parsing, no LLM
  cost): `assign <task> to <name> [by <date>]`, `start/done/blocked/
  cancel <task id> [reason]`, `approve/reject <task id> [reason]`,
  `reassign <task id> to <name>`, plus `today`/`status`, `tasks`, `my
  tasks`, and `overdue` — the last of these names who owns each overdue
  item, not just a count. A task marked `approval_required` (settable
  via the API/store today; no WhatsApp grammar for it yet) routes
  through an `awaiting_approval` state instead of closing immediately.
- **Audit log**: every roster change and task assignment/approval/
  rejection writes an immutable row (`AuditLogStore`) — who did what,
  to what, and when.

## What v1 deliberately does not do

Not a CRM, not a website builder, not a workflow-automation platform. No
customer-facing email (only the owner-facing digest above — no
password-reset flow, no email support inbox). No multi-instance/
distributed mode — single SQLite + local Chroma, correct for one Railway
instance. No WhatsApp *outbound* message templates (business-initiated
messages outside the 24-hour customer-service window) — that needs
Meta template approval per tenant, out of scope for V1.2. No recurring/
auto-charging subscriptions (V1.5's billing is request-and-confirm, not
Razorpay Subscriptions with auto-debit) — see Known Limitations. These
are scoped omissions, not oversights: the goal is the smallest product a
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

173 tests covering the full HTTP lifecycle (signup → ingest → activate →
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
the WhatsApp AI-employee upgrades (owner WhatsApp push not gating email,
the owner status-pull command including phone-number normalization,
Devanagari query translation wiring with a fail-open fallback, and
Embedded Signup's token exchange including tenant isolation), the
`authorize()` chokepoint, chunking edge cases, usage-limiter behavior,
and the SSRF guard. All offline — no OpenAI cost, no real Meta or
Razorpay API calls — using a deterministic fake generator, a word-overlap
fake embedding provider that still exercises the real evidence-gate
confidence threshold, and fake WhatsApp/Razorpay/Meta-signup clients that
capture calls instead of hitting real APIs. The Hindi/Hinglish retrieval
claim itself (the +0.12-0.22 confidence lift) was validated separately,
live, against the real OpenAI API — see the V1.4 section above; that run
is not part of the committed test suite since it costs real API money.
V1.5 adds billing (link creation, manual paid-confirmation, the shared
activation-blocker gate) and self-serve activation (knowledge + payment
preconditions, tenant isolation, suspended/already-active rejection,
graceful no-op when nobody's configured to be notified). V1.6 adds the
admin bot's employee roster (tenant isolation, upsert-by-number,
deactivation, owner-bootstrap), the fail-closed `authorize()` regression
test and the new `manager` role's exact permission boundary, and the
full WhatsApp task-command grammar (assignment + notification,
role-gated status updates, the approval/reject round trip, reassignment,
and the `today`/`tasks`/`my tasks`/`overdue` views scoped correctly by
role) — all against the same fake-WhatsApp-client harness as V1.2's tests.

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
- **Owner WhatsApp alerts are also 24-hour-window-limited.** A push to
  the owner's own number can fail the same Meta rule a customer message
  can, if they haven't messaged the business's line recently. Email is
  the guaranteed channel; WhatsApp is a bonus fast path, not a
  replacement for it — by design, not an oversight.
- **WhatsApp Embedded Signup has no frontend yet.** The backend token
  exchange is built and tested, but the actual "Connect WhatsApp" button
  needs a real `WHATSAPP_APP_ID` and an approved Meta Tech Provider
  registration to be testable at all — building the popup flow against
  credentials that don't exist yet isn't "production-ready," it's
  guesswork. The manual per-tenant connection flow remains the only
  active path until that registration completes.
- **The Devanagari-to-English retrieval fix does not fully close the
  confidence gap for every question** — it recovers most of it (see the
  V1.4 section above), but a couple of translated queries still landed
  under the 0.35 threshold in live testing, consistent with the separate,
  unrelated finding that some *English* questions do too. Fixing that
  underlying threshold/scoring calibration is flagged, not fixed here.
- **Billing is request-and-confirm, not automated recurring billing.**
  There's no Razorpay Subscriptions integration, no auto-charge, no
  dunning, no invoice history — you send a link when payment is due and
  mark it paid once you've checked your own Razorpay dashboard. Fine for
  a handful of early customers; a real subscription-billing system is
  separate, larger scope if this needs to run unattended at volume.
- **Self-activation checks knowledge + payment, nothing else.** It
  doesn't re-verify WhatsApp is connected, doesn't sanity-check the
  knowledge base's quality, and doesn't require the owner to have tested
  their assistant first. A business could self-activate having only
  ingested one thin page. This mirrors exactly what the admin's own
  activate button already allowed — self-service didn't lower the bar,
  it just removed the requirement that a human click it.
- **The admin bot's task commands are deterministic keyword parsing, not
  natural language.** "assign restock shelf 3 to Ravi by 2027-01-15T18:00"
  works; "hey can Ravi handle the shelf thing sometime tomorrow" doesn't
  yet — a later phase swaps the parser for an LLM intent classifier
  without changing any of the underlying handlers. Due dates likewise
  need a structured `YYYY-MM-DD[ HH:MM]`, not free text like "tomorrow."
- **No dashboard UI for the roster or tasks yet** — `/api/employees` and
  `/api/tasks` exist and are tested, but there's no admin-panel screen
  for them today; management happens over WhatsApp or direct API calls.
- **`approval_required` tasks can't be created via the WhatsApp grammar
  yet** — only through the API/store directly. The `awaiting_approval`
  → `approve`/`reject` round trip itself is fully wired and tested once
  such a task exists.
- **No proactive/scheduled admin-bot pushes yet** (a morning briefing, an
  overdue-task nudge nobody asked for) — every reply today is triggered
  by an inbound message, same 24-hour-window constraint as the rest of
  this app's WhatsApp sends. Scheduled pushes need outbound message
  templates, which aren't built (see above).
