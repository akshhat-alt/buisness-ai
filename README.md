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

## What V1.7 adds: employee feedback + sentiment/theme classification

- **`feedback <what's going on>`**: any roster member (owner, manager, or
  staff) can report a concern, complaint, or suggestion in plain
  language over WhatsApp. `generation.classify_feedback_sentiment` — a
  dedicated structured-output call, separate from the customer-facing
  answer schema — classifies it into `sentiment` (positive/neutral/
  negative), a fixed `theme` taxonomy (software/tools, scheduling,
  equipment/supplies, pay, etc. — bounded, not free text, so results
  aggregate cleanly instead of fuzzy-matching drifting phrasing),
  `urgency`, a `root_cause_hint`, and a `suggested_action`. Live-
  validated against the real OpenAI API on 8 realistic employee
  messages before landing (correct theme on all 8, sensible sentiment/
  urgency even on subtler cases — a scheduling *suggestion* stayed
  neutral rather than reading as a complaint).
- **`feedback themes`** (owner/manager only — `VIEW_FEEDBACK`): a
  WhatsApp summary of recurring themes with report counts and how many
  were negative. `GET /api/feedback` / `POST /api/feedback/{id}/resolve`
  expose the same data to the dashboard.
- **Submitting needs no permission check; viewing the aggregate does.**
  Anyone on the roster can report an issue; only an owner or manager can
  see the rolled-up pattern — deliberately never surfaced as a per-
  employee sentiment score (see `FeedbackStore.summarize_by_theme`'s
  docstring).

## What V1.8 adds: proactive alerts, business scorecard, WhatsApp automation

Phase 1 + Phase 2 combined into one sprint, prioritized by business value
rather than built as sequential milestones — reusing the digest, alert,
roster, and classification infrastructure that already existed rather
than standing up parallel systems.

- **Recurring issues feed the owner's daily digest, not just a WhatsApp
  command.** `admin_run_digest` now pulls overdue tasks (named by
  employee, via `EmployeeStore`) and feedback themes that have crossed a
  repeat threshold (`RECURRING_FEEDBACK_THRESHOLD = 3`) into the SAME
  email/WhatsApp send as leads/analytics — one daily message covering
  the whole business, not a second inbox to check.
- **The LLM action-brief is now genuinely cross-functional.**
  `generate_action_brief` takes the same overdue-task and recurring-
  feedback data and writes it into the SAME structured-output call —
  live-validated against the real OpenAI API to confirm it surfaces
  operational signals (not just sales/lead advice) without inventing
  any name, number, or task not actually in the data.
- **Instant alert for urgent feedback**: `classify_feedback_sentiment`
  returning `urgency: "high"` fires the same "don't wait for tomorrow"
  treatment as the customer dissatisfaction alert (`render_
  urgent_feedback_alert`), to every owner/manager, over email and
  WhatsApp independently.
- **Management notifications now reach the whole roster, not one
  scalar number.** `_notify_management_whatsapp` replaces the old
  single-owner-number push — every `owner`/`manager` row gets the
  digest and both instant alerts, best-effort per recipient.
- **WhatsApp message templates** (`WhatsAppClient.send_template`,
  `TenantConfig.admin_notify_template_name`): when a free-text push
  fails because nobody on the roster has messaged the business's line in
  the last 24h, and the tenant has an approved template configured, a
  short fixed fallback notice goes out instead of silently dropping the
  notification. Real content still only ever reaches someone inside the
  live conversation window — Meta doesn't allow arbitrary text in an
  approved template without pre-registered variables, which isn't built.
- **`scorecard` / `health`** (owner/manager, WhatsApp command and
  `GET /api/business-health`): one transparent, component-based snapshot
  — task completion count, named overdue work, customer questions/
  answer rate, complaints, buying interest, recurring feedback — never
  collapsed into a single opaque score, and every number traces back to
  a real store query.

## What V1.9 adds: NL commands, SOP conversion, escalation, verified outcomes

- **Natural-language employee commands**: deterministic keyword parsing
  (`app.py`'s `_try_deterministic_admin_command`) is tried first — free,
  instant, exact — and only a message matching nothing pays for one
  fallback LLM call (`generation.classify_employee_message`). Free-form
  phrasing ("hey can ravi handle the shelf restock tomorrow", "just
  finished the shelf restock", "how are we doing this week") now routes
  to the exact same handlers as the typed commands, by construction: the
  classifier only extracts intent + slots, then its output is replayed
  through the deterministic function itself — no permission check,
  lookup, or notification exists in two places. Live-validated against
  the real OpenAI API across all 5 intents, including an adversarial
  "assign nothing to nobody" case that correctly classified as `other`
  rather than hallucinating a fake assignment. That validation caught and
  fixed a real prompt bug: asking for "YYYY-MM-DD **or**
  YYYY-MM-DDTHH:MM" made the model reproducibly emit a corrupted date
  string; simplified to date-only, re-validated clean.
- **SOP / action conversion** (`memory.py`, owner-only —
  `approve sop <theme>: <note text>`, `GET/POST /api/sops`): turns a
  recurring feedback theme into a short, owner-approved workaround note.
  The next employee reporting that same theme gets it echoed back
  ("We know about this — current guidance: ...") in the exact same
  acknowledgment their `feedback` message already gets — no new command
  for employees to learn. `feedback themes` and the scorecard both show
  SOP coverage (e.g. "1/2 have approved guidance").
- **Proactive task escalation** (`POST /api/v1/admin/task-escalation/run`,
  platform-admin, externally cron'd — meant to be polled more often than
  the once-daily digest): a task overdue by more than 48h gets an instant
  alert to management instead of waiting for tomorrow's summary.
  Deduped per-task via the same `reminder_sent_at` marker convention
  `TaskStore` already used for lead reminders.
- **Verified customer outcomes**: `TaskStore.customer_facing_lead_id`
  (present since Phase 0, unused until now) links a task to the lead/
  conversation it came from — settable via the WhatsApp `assign ... for
  lead <id>` clause or the new `POST /api/tasks`. Completing (or
  approving) such a task fires a best-effort WhatsApp ping back to that
  SAME customer asking if their issue was actually resolved — fire-and-
  forget, same honest "no webhook, no reconciliation" shape as every
  other one-way send in this app, never blocking task completion.

## What V1.10 adds: business memory, timeline, and the lead-to-revenue loop

Phase 3 (business memory/intelligence) + Phase 4 (lead/revenue journey)
together — every piece below reads from stores that already existed
(LeadStore, AnalyticsStore, AuditLogStore, FeedbackStore) rather than
introducing a parallel ledger or event system. Live-verified end-to-end
against a real running server: a real customer message classified for
buying intent by the real OpenAI API, captured as a lead, flagged as a
missed opportunity, confirmed paid over WhatsApp, and reflected correctly
in both the scorecard and the timeline.

- **Real, owner-confirmed revenue — never estimated, never synced.**
  `Lead` gained `deposit_paid_at`/`deposit_paid_amount_inr` and
  `appointment_outcome`/`appointment_outcome_at` — the same "request-
  and-confirm, no webhook" honesty already used for platform billing,
  now at the customer level. A deposit LINK being sent, or an
  appointment being SET, still never counts as revenue on its own;
  `mark paid <lead id> [<amount>]` and `mark completed/no-show/cancelled
  <lead id>` (WhatsApp) or `POST /api/leads/{id}/deposit-paid` /
  `.../appointment-outcome` (API) are the only actions that do.
- **`lead_stage()`** (`leads.py`): a pure, deterministic function —
  new → engaged → awaiting_payment → converted, or lost/reengaged —
  derived entirely from fields already on the row. No LLM, no invented
  status.
- **Missed-opportunity detection**: `AnalyticsStore.
  session_ids_with_buying_intent`, joined against LeadStore by
  `session_id`, finds leads whose conversation showed real buying intent
  but who never booked — a stronger, more specific signal than generic
  re-engagement, surfaced by name in the scorecard and digest.
- **Business scorecard, extended with real conversion/revenue figures**
  and deterministic window-over-window trends (`▲/▼ N%` vs the equal-
  length period immediately before — comparison, not prediction, no
  LLM) for leads, task completions, conversions, revenue, and feedback
  volume.
- **Business timeline** (`timeline` command, `GET /api/timeline`): a
  chronological read over the EXISTING audit log, not a new event
  store — task lifecycle events that weren't previously audited
  (start/blocked/cancel) were filled in so the timeline is actually
  complete, but no new write-path was added anywhere.
- **SOP draft assist** (`suggest sop <theme>`, owner-only): a new LLM
  method, `generation.draft_sop_note`, drafts a short guidance note
  grounded ONLY in the actual employee-reported texts for that theme —
  never auto-approved, always a starting point for `approve sop` to
  edit and confirm. Live-validated against the real API, including a
  deliberately vague case (confirmed it says "more information is
  needed" rather than inventing a specific cause).

## What V1.11 adds: a dashboard for the admin bot (owner-only)

Everything V1.6–V1.10 built was WhatsApp/API-only until now. New "Team &
Tasks" and "Business Health" sections on the owner dashboard, reading the
same endpoints the WhatsApp commands use — `GET /api/employees`,
`/api/tasks`, `/api/business-health`, `/api/timeline`, plus `POST
/api/employees`, `/api/tasks`, `/api/leads/{id}/deposit-paid`,
`/api/leads/{id}/appointment-outcome`, and `/api/sops` for the small set
of write actions exposed here (add employee, assign task, confirm a
payment/outcome, save SOP guidance). No new backend logic — every number
and action was already real. `GET /api/leads` now also returns each
lead's `stage` (`leads.lead_stage()`), rendered as a pill with a "Mark
Paid"/"Completed"/"No-show"/"Cancelled" action inline. Hidden entirely
for non-owner dashboard logins, since the underlying endpoints are
owner/manager-gated and a staff view would just be a wall of 403s.
Live-verified in a real browser: added an employee, assigned a task, and
confirmed a ₹250 payment — the lead's stage flipped to "Converted," the
scorecard and timeline updated correctly, all in one session with zero
console errors.

## What V1.12 adds: the Automation Engine + Owner Command Center (Phase 6 + 7)

**Automation Engine** (`src/business_ai/automation.py`): owner-configured
trigger → condition → action rules (`AutomationRuleStore`) plus their
execution history (`AutomationRunStore`), evaluated by a new
`POST /api/v1/admin/automation/run` cron endpoint — the same
external-cron-hits-an-endpoint convention as every other periodic job in
this codebase, not a new in-process scheduler. Four trigger types, each a
pure, deterministic read of data that already exists (no new signal
invented, no existing store duplicated): a task overdue by an
owner-chosen number of hours, negative feedback unresolved for that long,
a feedback theme repeated at least N times, or a lead's deposit still
unpaid a chosen number of hours after its appointment. Two action types,
both reusing existing, already-tested capabilities: notify the owner/
manager roster on WhatsApp, or create a follow-up task (assigned to the
tenant's owner employee by default). Dedup/retry follows the same
existence-based idiom as `WhatsAppInboxStore.claim()`/
`TaskStore.reminder_sent_at`: a successful run row means "never fire
again for this target" (except a recurring-feedback-theme rule, which is
allowed to refire only when the count has grown since its last alert); a
failed run leaves no such row, so the next cron tick retries
automatically, capped at `MAX_ATTEMPTS` (5) before giving up and
surfacing a `given_up` row in execution history rather than retrying
forever. Every fired action also writes an `AuditLogStore` entry, reusing
the same audit backbone as every other admin-bot state change — no new
event-sourcing system. Owner-only **kill switch**
(`TenantConfig.automation_enabled`, default `True`) instantly stops every
rule for a tenant without touching individual rules' own enabled flags;
checked fail-closed at the top of the cron run, right after the
tenant-must-be-ACTIVE check. Two new `TenantAction`s
(`MANAGE_AUTOMATION` owner-only, `VIEW_AUTOMATION` owner+manager) follow
the same fail-closed `ROLE_ACTIONS` map as every other permission in this
app. New dashboard "Automation" section: kill switch toggle, a rules
table (create/enable/disable/delete), and an execution-history table
showing every run's status (`success`/`failed`/`given_up`) — all reading
the same API a script or future integration would use.

**Owner Command Center** (`GET /api/command-center`, new dashboard
"Command Center" section): a pure reorganization of signals that already
exist — the same `_business_health_snapshot` the Business Health section
reads, plus the automation engine's own execution history — around the
questions an owner actually asks: what needs my attention, where's the
revenue opportunity, what's operationally broken, is it trending up or
down, what should I do next, and what has automation already handled for
me. "Recommended actions" are computed **deterministically** from the
snapshot (no LLM call on this path, unlike the digest email's
`generate_action_brief`) — opening the dashboard never waits on, or
costs, an OpenAI call. "Handled automatically" is a direct, visible link
back to Phase 6: every successful automation run appears here as
something the owner didn't have to do themselves.

Both phases are gated the same as Business Health/Feedback
(`VIEW_FEEDBACK`/new `VIEW_AUTOMATION`/`MANAGE_AUTOMATION`), tenant-scoped
identically to every other store in this app, and covered by 20 new
tests (`tests/test_automation.py`, `tests/test_command_center.py`) plus
2 new RBAC tests in `tests/test_tenant_authorization.py` — rule CRUD,
tenant isolation, the kill switch, all four triggers firing for real
(including the recurring-theme refire-on-growth case and the confirmed-
payment case that stops a deposit-unpaid rule from re-firing), dedup
across repeated cron runs, and the full retry → give-up sequence. Live-
verified against a real running server: created two rules (notify-owner
and create-task) against a backdated overdue task, ran the cron endpoint
twice, and confirmed in a real browser session that the Command Center,
Automation rules table, and execution-history table all rendered the
correct real data with zero console errors — including the notify-owner
rule genuinely failing (no WhatsApp configured for the smoke tenant) and
the create-task rule genuinely succeeding, both visible with the right
status pill.

## What V1.13 adds: self-serve onboarding (Phase 8)

A guided setup wizard (`static/onboarding.html`, served at `/onboarding` —
new signups land here instead of the full dashboard) walking a new owner
through seven steps, each backed by the same APIs the full dashboard
already used: Plan & Payment, Connect WhatsApp, Add Knowledge, Add Your
Team, Set Up Automation, Test Your Assistant, Activate. Steps navigate
freely (no fake "locked until you finish step N" gating that doesn't
reflect a real backend constraint) — the two genuine constraints
(knowledge required, payment required if priced) are enforced for real
by the existing `_activation_blocker`, and the wizard's own Activate step
just calls it honestly rather than re-implementing the check.

**Self-serve plan & payment** (`GET /api/platform/plan`,
`POST /api/tenant/billing/checkout`, `POST /api/webhooks/razorpay`): the
platform subscription price is a single operator-configured value
(`PLATFORM_SUBSCRIPTION_PRICE_INR`) — never invented by this app — and
unset means the payment step is skipped entirely, identical to every
tenant's behavior before this feature existed. When set, the owner
generates their own real Razorpay payment link (at the configured price
ONLY — a caller can never smuggle in their own amount) instead of
waiting on an admin to send one. New: a real, HMAC-signature-verified
Razorpay webhook (`verify_razorpay_webhook_signature`, same shape as the
existing WhatsApp webhook verifier) auto-confirms `billing_status=paid`
the moment Razorpay reports a payment link paid, correlated back to the
tenant via a `reference_id` now set on every platform billing link
(self-serve AND admin-sent). This turns "one-click activation" into a
real end-to-end capability for a paying customer — no admin in the loop
required — while the admin `mark-paid` fallback still works unchanged
for any deployment that hasn't configured the webhook secret (fails
closed: no secret configured means every webhook call is rejected, never
silently trusted).

**WhatsApp Embedded Signup — the frontend, finally.** The backend
(`MetaEmbeddedSignupClient`, the code-exchange route) was built and
tested since V1.4 but had no UI. The wizard now loads Meta's JS SDK,
launches `FB.login` with a `config_id` (new `WHATSAPP_CONFIG_ID` setting,
required alongside `WHATSAPP_APP_ID` for the capability flag to report
`available: true`), listens for the `WA_EMBEDDED_SIGNUP` `postMessage`
Meta's popup sends back with the new `phone_number_id`, and completes
the connection via the existing endpoint. Falls back to the manual
Phone-Number-ID/access-token fields (unchanged) whenever the capability
flag is `false` — never shows a button that would fail on click.

**Pre-launch AI test — a real architectural fix, not just a UI page.**
Live-verifying the wizard's "Test Your Assistant" step surfaced a real
bug: `authorize()`'s lifecycle gate blocked `QUERY_ASSISTANT` for
anything but an ACTIVE tenant, including the tenant's own owner — so a
brand-new signup literally could not test their own assistant before
activating. Fixed with one narrow, deliberate exception in
`tenant.py::authorize()`: the tenant's own owner/manager (never staff,
never a different tenant, never an anonymous caller) may query their own
assistant while PROVISIONING — and *never* while SUSPENDED, since that
status means something is deliberately wrong. This is the one non-UI
backend change this phase's live verification forced — found and fixed
before calling the feature done, exactly the discipline this project
holds itself to.

**Admin provisioning/control**: `GET /api/v1/admin/tenants` now returns
an `onboarding` block per tenant (knowledge sources, WhatsApp connected,
employee count, automation rule count) computed from existing stores —
no new state — so the admin panel can show a stuck self-serve signup's
real progress at a glance instead of just status/billing badges.

Live-verified end to end against a real running server and a real
browser session: signed up a fresh tenant, generated a self-serve
checkout link, confirmed payment via an actually-HMAC-signed webhook
POST (not a mocked call), watched the UI transition to "Payment received"
in real time, added a real knowledge source (real fetch + real OpenAI
embedding), added a real employee, created a real automation rule, asked
the real, not-yet-activated assistant a real question (triggering the
authorize() fix above), and self-activated — landing on the real
dashboard with the test question correctly surfaced as an open knowledge
gap. Separately confirmed the fail-closed path: with a price configured
but no real Razorpay credentials, "Pay Now" surfaces "Payment isn't
configured on this deployment yet" rather than a broken button or a
fake success state. 25 new tests across
`tests/test_billing_and_activation.py` (self-serve checkout, webhook
signature verification, idempotent redelivery, unknown-tenant/unpaid/
irrelevant-event handling), `tests/test_tenant_authorization.py` (the
provisioning-preview exception's exact boundaries), `tests/test_whatsapp.py`
(the extended capability flag), and `tests/test_admin_bot.py` (the
onboarding-progress view).

## What V1.14 adds: platform foundation, observability & security (Phase 9)

A pure-foundation phase — no owner-facing feature, every existing
behavior preserved and regression-tested — that the next several phases
(dependency intelligence, self-evolution, financial truth, multi-
location) all needed to build on safely.

**`app.py` split into domain routers.** The single 3,376-line file that
had accumulated every route across 8 phases is now an orchestrator: it
builds `Services`, the FastAPI app and its middleware, one shared
`RouteContext`, and calls `register_*(app, svc, ctx)` from 13 new modules
under `src/business_ai/routers/` (auth, customer chat, the admin
WhatsApp bot + core grounded-answer/business-health/automation-firing
engine, leads, knowledge, tenant settings, team, feedback, automation,
insights, webhooks, platform admin, static pages). Every route's
behavior is byte-for-byte identical — this was a mechanical extraction,
not a rewrite — verified by the full 315-test suite passing unchanged
plus a live multi-router smoke pass. Shared cross-cutting helpers
(auth resolution, `_process_question`, business-health snapshotting,
automation firing) live in `routers/admin_bot.py` and are exposed to
every other router via `ctx` (`routing_context.py`) — a plain attribute
bag, not FastAPI's dependency-injection machinery, chosen specifically
because it required touching each route's *body* not its *signature*,
the lowest-risk way to split 100+ closures across files without
rewriting how each one is called.

**Structured logging + request tracing** (`observability.py`): every log
line anywhere in the app — a route handler, a helper three calls deep in
the admin bot — is one JSON object carrying the SAME `request_id` for a
request's whole lifetime, via a contextvar `RequestContextMiddleware`
sets once per request. The response echoes the id back
(`X-Request-ID`), and one structured access-log line (method, path,
status, duration, tenant_id) is emitted per request. Deliberately not a
full OpenTelemetry/APM integration — that's real infrastructure (a
collector, an exporter) this phase doesn't need to justify yet; this is
the free, dependency-light half of observability.

**WhatsApp/Razorpay secrets encrypted at rest** (`secrets_vault.py`):
Fernet-encrypted, keyed by a new `SECRET_ENCRYPTION_KEY` setting.
Backward compatible by construction — unset, every tenant's secrets stay
plaintext exactly as before this existed (`validate_environment` flags
this as a warning, deliberately NEVER an error that would block startup,
unlike JWT_SECRET_KEY/ADMIN_SECRET). Migration is lazy (a tenant's
secrets get encrypted the next time their config is written) plus a
one-time tool, `scripts/rotate_secrets.py`, that also doubles as key-
rotation support (`--old-key`/`--new-key`, dry-run by default).

**A shared SQLite storage abstraction** (`storage.py`): the exact eight
lines of connection-management boilerplate 13 different stores had each
independently hand-rolled (thread lock, WAL pragma, busy timeout,
`sqlite3.Row` factory) are now one `SqliteStore` base class every store
inherits — zero behavior change (same SQLite, same pragmas, same
locking), verified by the full suite. `usage_limiter.py` deliberately
does NOT use it — it tunes a shorter timeout and skips the busy-timeout
pragma as a real, intentional hot-path difference this extraction must
never quietly erase.

**Tenant data export/deletion** (`tenant_data.py`,
`GET /api/tenant/export` / `POST /api/tenant/delete`): every tenant-
scoped record across every store, as one JSON export (connection secrets
redacted — they're credentials, not the owner's data); deletion is real,
irreversible, and gated by typing the business's current name exactly
(the same confirmation bar every serious platform holds a destructive
action to), removing rows from all 13 SQLite stores plus the vector
index plus the tenant's own config row last.

**IP + tenant rate limiting** (`rate_limiting.py`): a broad, in-memory,
fixed-window abuse guard layered in FRONT of the existing per-(tenant,
session) question quota — which structurally cannot notice one IP
minting new sessions to dodge its own limit, or cap a tenant's TOTAL
request volume across every session at once. Defaults are deliberately
generous (120/min per IP, 300/min per tenant — a real dashboard page
load alone fires a dozen-plus calls) since this is an abuse guard, not a
tight quota.

**Security/dependency scanning**: a new `pip-audit` CI job
(`.github/workflows/security.yml`, weekly + every push/PR) scans
`requirements.txt` against the PyPA/OSV advisory databases. Found and
fixed real vulnerabilities in `pypdf` (5.9 → 6.16, clean) and the new
`cryptography` dependency (→ 50.x, clean) as part of this phase. Four
`chromadb` advisories remain, explicitly ignored with documented
reasoning in the workflow file: all four are in chromadb's HTTP *server*
mode (unauthenticated API access, RBAC bypass) — this app only ever uses
`chromadb.PersistentClient`, an in-process client with no network
listener, so none of the four are reachable in how this app actually
uses the dependency.

Live-verified end to end against a real running server: connected a real
WhatsApp token through a tenant with `SECRET_ENCRYPTION_KEY` set,
confirmed the on-disk row contains no plaintext (only `enc:v1:...`),
confirmed `GET /api/tenant` still returns the correct decrypted plaintext
to an authorized owner, confirmed `GET /api/tenant/export` redacts it,
and confirmed every response carries a working `X-Request-ID` with a
matching structured JSON log line. 47 new tests across
`tests/test_observability.py`, `tests/test_secrets_vault.py`,
`tests/test_storage.py`, `tests/test_tenant_data_export_and_delete.py`,
`tests/test_rate_limiting.py`, and `tests/test_config_validation.py` —
361 total, all green.

## What V1.15 adds: Business Dependency Intelligence (Phase 10)

The owner-facing answer to "what breaks if my one experienced person
goes on leave tomorrow" — computed, not guessed, from data the app
already has (tasks, employees, SOPs, leads), with zero new
source-of-truth store.

**Dependency graph** (`dependency_graph.py`): a "process" is a recurring
task type — task titles grouped by exact normalized match (case,
punctuation, whitespace), any title appearing 2+ times. Each process
gets a **bus factor**: how many distinct employees have ever completed
it (falling back to who's been assigned it, if nobody's finished one
yet). A bus factor of 1 is a single point of failure, flagged as a
**high**-severity risk regardless of team size — a one-person team where
only that one person can do something is exactly the risk this feature
exists to surface, not a false positive to suppress. Two more risk
types, gated behind a minimum roster of 3 (below that, "one person does
most of the work" is trivially true and not a meaningful finding):
**workload concentration** (one employee holding the outsized majority
of currently-open tasks) and **knowledge concentration** (one employee
as the sole author across a business's approved SOP notes). A fourth,
**customer concentration**, flags a lead whose every interaction has
gone through a single employee.

**"What breaks if X is unavailable?" simulation**
(`simulate_employee_unavailable`): pure deterministic graph traversal
for one named employee — their own open tasks, which processes would
become bus-factor-zero (orphaned) without them, which customers only
they've ever spoken to, and which SOPs only they've authored. Never a
prediction, never LLM-generated — the same inputs always produce the
same answer, which is the point when an owner is deciding whether
someone can actually take next week off.

**Owner-facing Business Map** (new dashboard section, `#business-map` in
`static/dashboard.html`): the risk list, the process/bus-factor table,
workload/knowledge concentration cards, sole-contact customers, and a
"simulate this employee being unavailable" picker — all reading from two
new `GET /api/dependency/map` / `GET /api/dependency/simulate` routes,
gated by the same `VIEW_FEEDBACK` action already used for other
owner/manager-only aggregate views (nothing here is more sensitive than
feedback themes; it doesn't need a new permission).

**Proactive dependency detection**
(`POST /api/v1/admin/dependency-scan/run`, platform_admin-only, meant
for the same external daily cron as every other `/admin/*/run` job):
scans every active tenant's snapshot, and for any **high**-severity risk
(bus-factor-1 processes only — concentration risk stays dashboard-only,
not urgent enough to interrupt an owner over WhatsApp) sends one
WhatsApp alert naming the specific process at risk. Dedup reuses
`AuditLogStore` exactly like every other proactive job in this
codebase (WhatsApp inbox claims, task reminder markers, automation run
history) — a stable `target_id` per risk (e.g.
`process:restock shelf 3`) is recorded once flagged and not re-sent for
7 days (`DEPENDENCY_RISK_RENOTIFY_HOURS`), so an unresolved risk doesn't
mean a daily repeat of the same message forever; a genuinely new risk
(a different process going bus-factor-1) still notifies immediately.

Live-verified end to end against a real running server: created an
employee with 3 completed instances of one task title, confirmed
`GET /api/dependency/map` correctly reported it as a bus-factor-1 high
risk, confirmed `GET /api/dependency/simulate` for that employee listed
the process as one that would become orphaned, confirmed the dashboard's
new Business Map section rendered the same data with zero console
errors, then ran the proactive scan cron twice — the first run notified
and wrote an audit-log entry naming the process, the second run
correctly deduped it (no new WhatsApp send, no new audit row). 30 new
tests across `tests/test_dependency_graph.py` (18),
`tests/test_dependency_routes.py` (6), and
`tests/test_dependency_scan_cron.py` (6) — 391 total, all green.

## What V1.16 adds: Self-Evolution Infrastructure (Phase 11)

A safety-first pipeline (`evolution.py`) that lets Business AI propose
small, reviewable improvements to a tenant's customer assistant — never
anything it can just go make happen. The hard boundary, enforced in
code, not just in this description: self-evolution can NEVER touch
Python source, SQL schema, infrastructure, secrets, money/payment logic,
or a tenant's account/activation status. It can only create and, once an
owner approves, activate a version of one whitelisted, plain-text
config: the customer assistant's tone-guidance string, appended to the
end of its system prompt as supplementary guidance that can never
override the grounding/citation/dissatisfaction rules above it.

**observe -> failure detection**: a pure read model over
`AnalyticsStore` — the only signal actually about the customer
assistant's own behavior (employee feedback in `feedback.py` is about
internal operations, a different thing, and isn't consulted here). If a
tenant's dissatisfaction rate over the last two weeks crosses 20% (with
at least 10 real questions logged, so a bad afternoon doesn't look like
a trend), that's a failure signal.

**proposal -> evaluation/versioning**: a new `EvolutionVersionStore`
holds every version of a tenant's assistant-tone config, immutable once
created, full lineage preserved (`parent_version_id`, `activated_at`,
`deactivated_at` — nothing is ever deleted by activation or rollback).
On a failure signal, `generate_behavior_proposal` deterministically (no
LLM call — 100% reproducible) drafts a candidate version and an
`EvolutionProposalStore` row, at most one in flight per tenant at a time.

**sandbox testing**: before an owner ever sees a proposal,
`run_sandbox_evaluation` shadow-replays a small sample (5) of the
tenant's own recently-answered real questions through the REAL
retrieval+generation pipeline twice — once with the current tone, once
with the candidate — comparing outcomes. Neither run is ever shown to a
customer or logged as a real conversation; this is pure evaluation. Any
regression (an answer that flips from answered to abstained, or newly
shows dissatisfaction) fails the sandbox outright, recorded in a new
`EvolutionEvaluationStore`.

**gated promotion**: `POST /api/evolution/proposals/{id}/approve` is the
ONLY code path that ever activates a version for real customers, and it
flatly refuses anything that hasn't passed sandbox evaluation — verified
live (see below) against a real proposal that failed sandbox and was
correctly refused. Owners can reject a proposal, or manually roll back
to any prior version at any time, independent of automatic monitoring.

**monitoring + automatic rollback**: once active for 24+ hours,
`POST /api/v1/admin/evolution-monitor/run` (the same external-cron
convention as every other admin `/run` endpoint) compares a promoted
version's post-activation dissatisfaction rate against its own
pre-activation baseline window — deliberately LLM-free so this safety
net keeps working during an OpenAI outage — and automatically rolls back
on a real regression, notifying the owner over WhatsApp. One tenant's
rollback failing (a real edge case a live smoke test surfaced) is
isolated so it can never abort the cron run for every other tenant.

**kill switch + audit trail**: `TenantConfig.evolution_enabled` defaults
to **False** (opt-in, unlike automation's default-on kill switch — this
touches live customer-assistant behavior, automation doesn't) and gates
both crons entirely. Every state change — proposal created, sandbox
evaluated, approved, rejected, promoted, rolled back manually or
automatically — writes an immutable `AuditLogStore` row. A new
owner-only `MANAGE_EVOLUTION` action gates every route; managers and
staff have no visibility into evolution at all.

**owner-facing UI**: a new "Self-Evolution" dashboard section — the kill
switch, pending proposals with approve/reject buttons, and a full
version history table with a "Restore" action on any prior version.

Live-verified end to end against a real running server with a real
OpenAI key (not the test suite's fake generator): drove real
dissatisfaction into a tenant's conversation history, ran the scan cron
with the kill switch off (correctly skipped) and on (correctly proposed
and sandbox-evaluated a real candidate, which genuinely failed sandbox
evaluation against real model output and was correctly refused at
approval); separately approved a second, sandbox-passed candidate,
confirmed it reached a real customer's `/api/ask` call end to end, then
seeded a real regression and confirmed the monitoring cron automatically
rolled it back and wrote the full audit trail; also confirmed directly
against the live database that a SQL/code-injection-shaped tone string
is rejected at the storage layer regardless of caller. 42 new tests
across `tests/test_evolution.py` (26), `tests/test_evolution_routes.py`
(8), and `tests/test_evolution_cron.py` (8, including one added after
the live smoke test surfaced the per-tenant rollback isolation gap
above) — 433 total, all green.

## What V1.17 adds: Financial Truth Layer (Phase 12)

A manual sales/expense/collection ledger (`metrics.py`,
`BusinessMetricStore`) — Business AI has no live POS/accounting
integration, so every response this layer produces is explicitly
labeled `"source": "manual"`, never implied as a live sync.

**Logging** happens only over the admin WhatsApp bot's deterministic
`log sale/expense/collection <amount> [note]` grammar (e.g. `log sale
1500 haircut`) — zero LLM cost, same idiom as Phase 0's task commands.
Open to any roster member, no permission check, the same shape as
feedback submission: a staff member at the register should be able to
log a sale without needing owner/manager rights.

**Viewing** is gated: `financials` / `sales report` over WhatsApp, and
`GET /api/metrics/summary` / `GET /api/metrics/export.csv` over HTTP,
all owner/manager-only (`VIEW_FINANCIALS`) and tenant-isolated —
aggregated totals are more sensitive than a single log-entry action, the
same reasoning as `VIEW_FEEDBACK`. CSV export uses Python's stdlib `csv`
module — no new dependency, no PDF library added (see Known
limitations).

Live-verified end to end against a real running server: confirmed an
empty summary for a brand-new tenant, logged real sale/expense entries,
confirmed both the summary endpoint and the CSV export reflected them
correctly, and confirmed cross-tenant access is refused. 13 new tests
across `tests/test_metrics.py` (6) and `tests/test_metrics_routes.py`
(7) — 446 total, all green.

## What V1.18 adds: Weekly Business Scorecard (Phase 13)

A week-over-week rollup (`scorecard.py`) built entirely from data every
earlier phase already collects — tasks completed, customer
dissatisfaction rate, manual sales/expense/collections (Phase 12), top
recurring team feedback theme, and open single-point-of-failure risk
count (Phase 10) — no new store, pure data-assembly + render exactly
like `digest.py`'s own shape, just a weekly lens instead of a rolling
window.

`POST /api/v1/admin/weekly-scorecard/run` (the same external-cron,
platform_admin-only convention as every other admin `/run` endpoint)
sends the scorecard by email (when configured) and WhatsApp
(best-effort) to every ACTIVE tenant with any activity in the week —
quiet tenants are skipped, not spammed with an empty report, same
reasoning as the daily digest's `has_digest_content` gate. Unlike the
daily digest, this has no persistent dedup: the caller's own weekly
cadence is the only guard, since re-sending the same week's numbers
twice is harmless, not a correctness problem.

A real same-second boundary bug surfaced while writing this: computing
"this week" as `[one_week_ago, now)` with a fixed `now` string means a
row logged in that exact same second gets silently excluded by the
strict `<` comparison. Fixed by leaving "this week" open-ended (no upper
bound at all) rather than trying to compute a "now" precise enough to
never collide — the fix and the reasoning are in `scorecard.py`'s own
comment.

Live-verified end to end against a real running server: a brand-new
tenant with zero activity was correctly skipped, then real sale/expense
entries were logged and the very next cron run correctly included and
sent that tenant's scorecard. 10 new tests across `tests/test_scorecard.py`
(6) and `tests/test_scorecard_cron.py` (4) — 456 total, all green.

## What V1.19 adds: Production Readiness — Backup, Restore & Data Integrity (Phase 14)

Business AI runs as SQLite + local Chroma on a single instance (see
ARCHITECTURE.md's "Scaling past one instance"); until this phase, a lost
or corrupted Railway volume had zero recovery path. `ops.py` closes that
gap with the smallest correct mechanism: a plain tar.gz snapshot of the
whole `data/` directory, not a clever incremental format.

**Backup**: `POST /api/v1/admin/backup/run` (platform_admin, same
external-cron convention as every other admin `/run` endpoint) snapshots
every tenant's SQLite files plus the Chroma vector store into one
timestamped archive under `data_backups/` (a sibling of `data/`, never
inside it — a backup must never recursively contain earlier backups).
`GET /api/v1/admin/backup/list` lists what exists. The same mechanism is
also a standalone script, `scripts/backup_data.py`, for a platform that
would rather cron a shell command than hit an HTTP endpoint.

**Restore**: `scripts/restore_data.py --archive ... --confirm` — refuses
to run without `--confirm`, and moves the CURRENT data directory aside
(never deletes it) before extracting, so a restore from the wrong
archive is itself trivially reversible. Deliberately a script, not an
HTTP route — restoring is the one operation here that should require
someone with real shell access to the instance, not a bearer token.

**Detailed health**: `GET /api/v1/admin/health/detailed` extends the
bare `/healthz` liveness probe with real operational visibility — every
core SQLite store actually opens and answers a trivial query, the
vector store is reachable, disk space, and the most recent backup's age
— the kind of thing an on-call rotation actually wants to see, not just
"the process is up."

**Data integrity**: `GET /api/v1/admin/data-integrity` is a read-only
scan for orphaned cross-store references — a task assigned to an
employee row that no longer exists, or team guidance authored by one —
that would otherwise fail silently (e.g. a task quietly rendering
"(unassigned)" forever, never surfacing that something is actually
wrong). Never mutates anything; a pure report.

Live-verified end to end against a real running server holding real
accumulated data from every prior phase's smoke testing: the detailed
health check reported every store `"ok"` and the vector store reachable;
the integrity scan checked all 18 real tenants and found zero issues; a
real backup was triggered over HTTP, appeared in the list endpoint and
in the health check's `last_backup`; and `scripts/restore_data.py` was
run against that real archive, correctly restoring the exact tenant
count into a separate location, with the pre-restore directory
preserved, not deleted. 18 new tests across `tests/test_ops.py` (10) and
`tests/test_ops_routes.py` (8) — 474 total, all green.

## What V1.20 adds: Revenue Leakage Radar (Phase 15)

A single, consolidated, owner-facing report of concrete missed-revenue
situations (`revenue_radar.py`) — no new store, no inference, no LLM,
built entirely from fields every earlier phase's automations already
key off (`Lead.appointment_at`, `deposit_link_sent_at`/`deposit_paid_at`,
`appointment_outcome`, and `AnalyticsStore`'s buying-intent tracking).
Every existing automation trigger ACTS on one condition; this is the
complementary READ-ONLY view answering "where is money actually being
left on the table right now":

- **Missed buying intent**: a lead whose conversation showed real
  purchase interest but never got an appointment set or a deposit paid.
- **Unpaid deposits**: a deposit link sent, still unpaid past a 48-hour
  grace window (an honest estimate — `total_estimated_leakage_inr` uses
  the tenant's own configured deposit amount, never presented as exact
  lost revenue since there's no live payment webhook).
- **No-shows**: appointments the owner has already marked as a no-show.

Reachable via `GET /api/revenue-radar` (owner/manager-gated, reusing
`VIEW_FEEDBACK` — the same "aggregate management view, not for staff"
permission Phase 10's Business Map uses, since plain `VIEW_LEADS` is
available to staff and too broad for a consolidated financial-leakage
report) and the admin bot's `revenue radar` / `leakage` WhatsApp
commands.

**Deliberately NOT a new proactive push.** Business AI already has four
owner-facing proactive channels (daily digest, weekly scorecard,
dependency-risk scan, evolution-monitor rollback alerts) — a fifth
unconditional WhatsApp push risks the alert fatigue that makes an owner
start ignoring all of them. This ships as a pull report only.

Live-verified end to end against a real running server: an empty radar
for a brand-new tenant, then a real buying-intent conversation turn and
a real appointment marked no-show both correctly surfaced through
`GET /api/revenue-radar` against the live database. 13 new tests across
`tests/test_revenue_radar.py` (9) and `tests/test_revenue_radar_routes.py`
(4) — 487 total, all green.

## What V1.21 adds: Unified Owner Dashboard + Final Hardening (Phase 16)

The capstone of this build arc (Phases 9-16): wires Financials (Phase
12), the Weekly Scorecard (Phase 13), and the Revenue Radar (Phase 15)
into the dashboard for the first time — all three had shipped
backend/WhatsApp-only, explicitly flagged as a dashboard gap in their
own Known Limitations. Zero new backend risk: every dashboard call
reuses an already-tested API, except one new pure-read route this phase
adds.

**New route**: `GET /api/scorecard` (owner/manager-gated) — the weekly-
scorecard cron pushes by email/WhatsApp on a schedule with no on-demand
equivalent; this lets the owner pull the identical numbers from the
dashboard at any time, computed with the exact same `scorecard.py`
functions, with zero side effects (no send, no state written).

**New dashboard section, "Business Intel"**: a Financials card (summary
+ CSV download, reusing Phase 12's tested endpoints), a Weekly Scorecard
table (this week vs. prior week, reusing the new route above), and a
Revenue Radar card (reusing Phase 15's tested endpoint) — all fetched in
parallel, all owner-only like the Business Map and Self-Evolution
sections before it.

**New platform-admin "System Health" panel**: surfaces Phase 14's
`GET /api/v1/admin/health/detailed` (every core store, the vector store,
last backup age, disk space) plus a "Run Backup Now" button calling
`POST /api/v1/admin/backup/run` directly from the browser — closing the
loop on Phase 14's backup infrastructure, which previously required
shell/curl access to actually trigger.

**Final hardening pass**: re-ran `pip-audit` against the full dependency
set — clean, same four pre-existing chromadb advisories still correctly
ignored with documented reasoning, no new vulnerabilities introduced
across Phases 9-16's additions (cryptography-free, no new network-facing
dependency was ever added in this arc).

Live-verified end to end: the real running server's `/api/scorecard`
route was exercised by the automated test suite; the dashboard's new
Business Intel section was loaded in a real browser against a real
tenant with a real logged sale, and its Financials/Scorecard/Revenue
Radar cards were confirmed rendering the correct live figures via direct
DOM inspection with zero console errors; the platform-admin System
Health panel's render path was exercised against the live server with a
real platform_admin token, correctly reporting every store `"ok"`. 4 new
tests in `tests/test_scorecard_routes.py` — 491 total, all green.

## What V1.22 adds: Restaurant Foundation (Phase 17)

The first phase of the Restaurant vertical, and the first genuinely new
business type Business AI understands beyond the original
salon/clinic-shaped service business. Five new stores
(`menu.py`, `inventory.py`, `suppliers.py`, `purchases.py`,
`wastage.py`), all following the exact `SqliteStore` pattern every
earlier store uses — no new architecture, no framework change.

**Menu &amp; recipes**: a `MenuItem` and its `RecipeLine`s (ingredient +
quantity per dish) are one aggregate, set up via the dashboard/API
(`POST /api/menu/items`, `POST /api/menu/items/{id}/recipe`) — a
deliberate choice: entering a whole menu is a one-time bulk task, not
the quick, repeated, on-the-floor action WhatsApp commands are for.

**Inventory &amp; stock depletion**: `InventoryStore` tracks ingredient-
level stock, keyed by a normalized ingredient name reusing
`dependency_graph.py`'s own `_normalize_title` idiom (as
`normalize_ingredient_name`) — "Chicken Breast" and "chicken   breast"
are the same ingredient. Logging a dish sale over WhatsApp
(`log sale butter chicken x2`) auto-prices from the menu and
automatically depletes every ingredient in that dish's recipe.

**Purchases &amp; wastage**: `log purchase 10 kg chicken ₹4200 from
Ramesh` records a receipt and receives the stock; `log waste 2 kg
paneer: spoiled` records wastage, depletes stock, and estimates a cost
from that ingredient's own average purchase price — honestly ₹0 when
there's no purchase history yet, never a guessed number, the same
discipline as Revenue Radar's own "estimated" figure. All three log
commands are open to any roster member, no permission check — identical
shape to Phase 12's `log sale/expense/collection`.

**Low-stock alerts**: `POST /api/v1/admin/inventory-alert/run` follows
the exact `AuditLogStore`-dedup pattern as Phase 10's dependency-risk
scan, with a shorter 24-hour renotify window (`INVENTORY_ALERT_RENOTIFY_HOURS`)
since a low ingredient is a same-day problem, not a slow-moving one.

**Scope decision, stated honestly**: this phase built a purchase
*receipt* log (`PurchaseStore`), not a full purchase-*order* lifecycle
(draft → sent → received) — that richer workflow is real, separate
scope for a later phase (Phase 30's autonomous purchasing builds on
top of this receipt data, not instead of it).

**Two real bugs found and fixed during this phase's own live
verification**, both now covered by regression tests:
- `InventoryStore.adjust_quantity` used to silently accept a mismatched
  unit for the same ingredient (a 5kg purchase followed by a 200g
  depletion computed a confidently wrong quantity). Now raises
  `UnitMismatchError` and refuses the whole WhatsApp command — including
  never partially recording a purchase/wastage entry that wouldn't
  actually reflect in stock — rather than silently corrupting a number.
- A menu item name starting with a digit (a real, plausible dish like
  "7 Up" or "2 Piece Chicken") would have been misread by the generic
  numeric `log sale <amount> [note]` grammar. Fixed by checking for a
  real menu-item match FIRST for any `log sale ...` text, falling back
  to numeric parsing only when no menu item matches — non-restaurant
  tenants (with no menu at all) are completely unaffected.

Live-verified end to end against a real running server: a real menu
item and recipe were created via the API, real stock was received and
then depleted by a real (simulated) WhatsApp dish sale, the live
`/api/inventory` and `/api/metrics/summary` endpoints correctly
reflected the depletion and the ₹700 sale, the low-stock cron correctly
notified once then deduped on rerun, cross-tenant access was refused,
the audit trail showed every real restaurant event, and a full tenant
export/delete correctly included and removed all five new stores. 73
new tests across `tests/test_menu.py` (12), `tests/test_inventory.py`
(13), `tests/test_suppliers.py` (6), `tests/test_purchases.py` (7),
`tests/test_wastage.py` (8), `tests/test_restaurant_routes.py` (8),
`tests/test_admin_bot_restaurant.py` (15), and
`tests/test_inventory_alert_cron.py` (4) — 564 total, all green.

## What V1.23 adds: Full Revenue Completion (Phase 18)

Closes the exact gap the original README named: "no payment-status
webhook... the owner checks their own Razorpay dashboard for now."

**A tenant's own deposit payments now reconcile automatically.** A new
per-tenant webhook, `POST /api/webhooks/razorpay/{tenant_id}`, verified
against a new `razorpay_webhook_secret` field the owner sets from their
OWN Razorpay dashboard (encrypted at rest exactly like
`razorpay_key_secret`/`whatsapp_access_token`, and correctly excluded
from tenant data exports and covered by `scripts/rotate_secrets.py`).
Deposit links now carry `reference_id=lead_id` when created, so a real
`payment_link.paid` event auto-calls the exact same
`LeadStore.mark_deposit_paid` an owner would otherwise call by hand —
verified live with a real HMAC-signed webhook request that correctly
moved a real lead to the `converted` stage.

**Both webhooks (platform + per-tenant) now understand the full
`payment_link.*` event family** — `paid` (as before), plus `expired`,
`cancelled`, and `partially_paid` — not just the one event type. Scope
stated honestly: `payment.*`/`refund.*`/`payment.dispute.*` events are
deliberately NOT handled, since their payload shape isn't the same
`payment_link.entity` structure this app can confidently correlate back
to a tenant/lead without a live Razorpay account to verify the real
shape against — guessing that mapping would risk silently mis-filing a
real payment event, worse than not handling it at all.

**Two new Revenue Radar recovery actions**: `POST /api/leads/{id}/nudge`
(a one-tap manual re-engagement, sending the exact same message text as
the automated reengagement cron) and reusing the existing
`POST /api/leads/{id}/deposit-link` route to resend a deposit link —
both now wired directly into the dashboard's Revenue Radar card as
per-lead buttons instead of requiring a trip to WhatsApp or another
dashboard section.

Live-verified end to end against a real running server: a real lead was
created, a real HMAC-SHA256-signed webhook request (computed the same
way Razorpay itself would sign one) correctly auto-confirmed its
deposit and moved it to the `converted` stage with a real audit-log
entry; a wrong signature was correctly rejected; redelivery of the same
event was correctly idempotent; the dashboard's new Nudge button
rendered with the real lead's name and a working handler, correctly
surfacing a clear "connect WhatsApp first" error rather than a silent
no-op when no WhatsApp number is connected. 17 new tests across
`tests/test_tenant_razorpay_webhook.py` (12) and 5 additions to
`tests/test_automations.py` — 581 total, all green.

## What V1.24 adds: Automation Engine 2.0 (Phase 19)

Generalizes the Automation Engine to cover the Restaurant vertical and
close the one gap its own Known Limitations section named: no action
that messages the customer directly.

**A fifth trigger type, `low_stock`.** Fires when any ingredient's
`quantity_on_hand` drops below its configured `par_level` (Phase 17) —
same synthetic `inventory:<ingredient_key>` target-id/dedup shape as
every other trigger, so an already-alerted ingredient is never
re-announced until it's restocked. **Event-driven, not just cron-driven:**
a real WhatsApp `log sale`/`log waste` command that actually depletes
stock below par now fires any enabled `low_stock` rule immediately, in
the same request, reusing the identical `_fire_automation_rule`
history-based dedup the external cron uses — an owner finds out the
moment the depletion happens, not up to a cron interval later.

**A third action type, `message_lead`.** Messages the CUSTOMER directly
over WhatsApp (every existing action type only ever notified the owner
or created an internal task) — reusing the exact single-lead send call
already used by the manual Nudge button (Phase 18) and the verified-
outcome ping (Phase 9). Scoped honestly: only valid when the rule's
trigger targets a lead (`deposit_unpaid_after_appointment` today);
`action_params["message"]` is required at creation and can't be emptied
out from under an existing `message_lead` rule via `PATCH` either — both
paths now reject the edit with a 400 rather than leaving a rule that
would silently fail every time it fires. Missing phone, no WhatsApp
connected, or a non-lead target all fail the run cleanly (recorded,
retried, eventually given up on) — never a silent no-op, never a crash.

**`escalate_after_hours` generalizes the one re-fire exception that used
to be special-cased to `recurring_feedback_theme`'s count-growth
logic.** Set it on any rule and a target that already fired successfully
can fire again — worded as "⏰ Still unresolved" rather than a fresh
alert — once it's been that many hours since the last successful fire
AND the underlying condition is still true. Left unset (the default,
and every rule created before this phase), a rule keeps its exact
original one-shot-until-resolved behavior — this is a strictly additive,
opt-in generalization, not a change to existing rules' behavior.

The dashboard's existing automation-rule builder (Phase 6) now exposes
all of this: a `low_stock` trigger option (no threshold field needed —
it reads each ingredient's own par level), a `message_lead` action with
its own message textarea, and an optional "re-notify after (hours)"
field wired to `escalate_after_hours` on every rule, not just the two
new types.

Live-verified against a real running server: creating a `low_stock` rule
and then sending a real `log sale`/`log waste` WhatsApp command that
crossed an ingredient's par level correctly fired the alert in that same
request with no cron tick; a `message_lead` rule correctly delivered a
templated message to a real lead's own WhatsApp number and correctly
failed closed (recorded, not silently dropped) when the lead had no
phone and separately when the tenant had no WhatsApp connected; an
`escalate_after_hours` rule correctly did not re-fire immediately after
its first success, then correctly re-fired once its one successful run
was backdated past the configured threshold, worded as an escalation
rather than a fresh alert; a pre-existing `task_overdue` rule with
`escalate_after_hours` left unset was confirmed to keep its exact
original one-shot behavior even after its one successful run was
backdated 1000 hours — the regression guard for every rule created
before this phase. 12 new tests across `tests/test_automation.py` (9)
and `tests/test_admin_bot_restaurant.py` (3) — 593 total, all green.

## What V1.25 adds: Self-Evolution Expansion (Phase 20)

Deepens the ONE thing Self-Evolution already does — proposing reviewable
assistant-tone adjustments — rather than widening it to configuration
that doesn't fit its conversation-replay sandbox. See "A scoping
decision" below for why "reorder par levels" and "automation threshold
tuning" from the original roadmap sketch aren't part of this phase.

**Themed, LLM-drafted proposals.** Detection itself is unchanged and
still 100% deterministic (see V1.16's own description) — a failure
signal fires exactly as it always did. What's new is WHAT the resulting
draft says: once a signal fires, this pulls the actual customer
questions that showed dissatisfaction in that window
(`AnalyticsStore.list_recent_dissatisfied_queries`, a new sibling of the
existing `list_recent_answered_queries`) and asks a new narrow LLM
method, `draft_tone_adjustment`, to name the common theme and draft tone
guidance tailored to it — "refund policy confusion" gets different
guidance than "unclear pricing answers," instead of every proposal
suggesting the same generic acknowledge-before-answering text. The
theme appears directly in the proposal's rationale, so an owner sees
*why* a change is being suggested, not just the resulting text.

**The safety boundary gets stronger, not weaker, from adding an LLM
step.** The LLM-drafted text is pre-validated through
`validate_behavior_payload` — the exact same single choke point every
version has always passed through — before it's ever used; on ANY
failure (no OpenAI access, a malformed response, or content the safety
filter rejects for being oversized or containing a banned pattern) this
falls back to the original deterministic `DEFAULT_TONE_SUGGESTION` text,
never to a half-applied or unvalidated draft. Sandbox evaluation and the
owner-approval gate are completely unchanged — an LLM only ever drafts
what a candidate SAYS; it never gains a new way to make a candidate
live.

**More configurable levers.** Three new optional per-tenant settings —
dissatisfaction-rate threshold, conversation-history lookback window,
and auto-rollback regression sensitivity — override the platform
defaults from `constants.py` for a single business, bounded at the API
layer (`schemas.TenantConfigUpdate`) so a malformed value can never
reach evolution.py's rate comparisons. Every tenant that existed before
this phase keeps the exact same platform-default behavior with zero
action required (all three default to `None`, meaning "use the platform
default").

**A scoping decision, made during this phase's own design step, not
before it:** the original roadmap sketch also named "more CONFIG_TYPES:
automation thresholds, reorder par levels" for this phase. Building
that surfaced a real architectural mismatch: Self-Evolution's sandbox
specifically shadow-replays real customer CONVERSATIONS through
retrieval+generation to catch a regression before it reaches anyone —
there is nothing to shadow-replay for "should this ingredient's par
level be higher" or "should this automation rule's threshold change."
Forcing those through the same version/proposal/sandbox lineage built
for conversational behavior would be exactly the kind of unnecessary,
ill-fitting architecture this project has deliberately avoided at every
other phase boundary (see V1.22's WhatsApp-vs-dashboard reasoning for
menu setup). The genuinely valuable piece of that idea — noticing an
ingredient keeps triggering `low_stock` alerts and suggesting a higher
par level — is real and is planned for Phase 22 (Restaurant
Profitability Intelligence) as a simple one-tap recommendation reusing
Phase 19's own automation-run history, not a new Self-Evolution config
type. "Automation threshold tuning" is deferred with no target phase
yet: there is no existing, honest, already-recorded signal for whether a
rule's timing is miscalibrated (as opposed to just working as designed)
without first building an owner feedback loop on alert usefulness, which
doesn't exist yet — building detection for a signal that doesn't exist
would be exactly the "meaningless feature" this project has been asked
to avoid.

Live-verified against the real running app: a real tenant's
dissatisfaction threshold was lowered via `PUT /api/tenant`, confirmed
to correctly fire a proposal the platform default would have missed on
the same data, then confirmed to correctly reject an out-of-range value
(150%) with a 422; a themed proposal was confirmed to carry its LLM-
drafted theme text through to the stored candidate's rationale exactly
as an owner would see it in the dashboard. 11 new tests across
`tests/test_evolution.py` (7) and `tests/test_evolution_cron.py` (4) —
604 total, all green.

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

604 tests (and rising — see each phase's own "What Vx.x adds" section
above for that phase's exact test count and what it covers; this
section deliberately stops narrating in detail at V1.7 rather than
re-summarizing every later phase inline, since keeping ONE hand-written
running narrative in sync with 20+ phases is exactly the kind of
staleness this codebase's own documentation audit caught once already).
The foundational V1-V1.7 coverage below still holds exactly as
described: the full HTTP lifecycle (signup → ingest → activate →
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
V1.7 adds FeedbackStore (tenant isolation, theme aggregation, unresolved-
negative queries) and the classification pipeline end-to-end (submission
by any role, `VIEW_FEEDBACK`-gated viewing, tenant-isolated capture over
a real webhook payload) — via the same deterministic `FakeGenerator`
pattern as every other LLM-touching test, plus a separate, uncommitted
live check against the real OpenAI API on realistic employee messages
before the prompt/schema was finalized.

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
- **~~No payment-status webhook~~ — closed in Phase 18** for a tenant
  who sets their own `razorpay_webhook_secret`. A tenant who never sets
  one keeps the original manual "mark paid" flow exactly as before,
  unchanged — this is a strictly additive, opt-in capability.
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
- **Feedback classification costs one real LLM call per `feedback`
  message** — unlike Phase 0's task commands, this isn't free. It isn't
  yet metered against a separate admin-bot quota (see the architecture
  plan's Phase-1 "second `UsageLimiter` instance" item) — today it draws
  from the same OpenAI budget as everything else, with no per-tenant cap
  of its own.
- **No recurring-issue → SOP/workaround conversion yet.** `feedback
  themes` surfaces counts and negative-report totals so an owner can see
  a pattern, but there's no one-tap "turn this into a documented fix"
  flow yet — that's a deliberately separate, later increment.
- **Raw feedback text has no retention/expiry policy yet.** Every
  submission is kept indefinitely; a time-based fade-out for the raw
  text (keeping only the aggregated theme counts) is a recommended,
  not-yet-built privacy hardening step given this data is about people,
  not just business operations.
- **No sales/expense/inventory data model exists, so no such reports
  exist.** The scorecard and digest report real task/feedback/customer
  data only — never a fabricated or estimated revenue/inventory number.
  Building a manual metrics ledger (and PDF/CSV report export) was
  deliberately deferred this sprint rather than shipped half-real.
- **The admin bot's LLM calls (feedback classification, the extended
  action brief) draw from the same OpenAI budget as everything else** —
  there's no separate quota/rate-limit dimension for admin-bot usage yet.
  Fine at pilot scale; a malicious or runaway insider sending many
  `feedback` messages has no per-tenant cap of its own before it would
  hit the platform's overall API budget.
- **WhatsApp template fallback sends a fixed, static notice only** — no
  dynamic content (the actual digest numbers, the actual complaint text)
  can go into it without Meta-side template variables, which aren't
  built. It exists to avoid a silent drop, not to fully replace the
  free-text push.
- **`find_by_whatsapp_phone_number_id` has no uniqueness check.** Two
  tenants could technically end up with the same `phone_number_id` if
  one were manually mistyped or copy-pasted (caught during this sprint's
  own live smoke test, with two throwaway test tenants) — in real usage
  each WhatsApp Business number's `phone_number_id` is assigned uniquely
  by Meta, so this is a data-entry-error edge case, not a normal-path
  risk, but a validation/uniqueness constraint is a reasonable future
  hardening step.
- **NL commands add a real LLM call per unmatched message, still on the
  shared OpenAI budget.** Exact-syntax commands stay free; free-form
  phrasing now costs one classification call each, on top of feedback
  classification's own per-message cost — the "no dedicated admin-bot
  quota" limitation above applies more now than it did in V1.7.
- **NL date extraction is date-only (no time-of-day).** "by friday
  evening" resolves to a plain date, dropping "evening" — a deliberate
  simplification made after live validation showed asking the model for
  two possible formats (date-or-datetime) reproducibly corrupted its
  output; a plain date-only format is fully validated and reliable.
- **NL report-request recognition is best-effort, not exhaustive.**
  "how are we doing this week" was classified as `other` (falls back to
  help text) rather than `report_request` in live validation — a real
  recall gap, not a correctness bug (nothing wrong gets returned, the
  user just needs to try the exact `scorecard` command or rephrase).
- **Linking a task to a lead via WhatsApp requires knowing (or being
  told) the lead's short id** (`assign ... for lead <id>`) — there's no
  "convert this complaint into a task" one-tap flow yet from the
  dashboard or from a dissatisfaction alert; `POST /api/tasks` is the
  more practical way to set `customer_facing_lead_id` today.
- **The verified-outcome customer ping is fire-and-forget, like every
  other one-way send in this app.** The customer's reply (if any) isn't
  parsed or tracked anywhere — an owner sees it as a normal WhatsApp
  reply, not a structured "resolved: yes/no" signal.
- **Revenue figures are exactly as complete as what's been manually
  confirmed.** `confirmed_revenue_inr` only ever reflects `mark paid`/
  `POST .../deposit-paid` calls — a business that hasn't been confirming
  payments through Business AI will correctly show ₹0, not an estimate.
  This is by design (no invented revenue), but it means the number is
  only useful once confirmation becomes a habit.
- **Trend percentages can be noisy at low volume.** "▲ 100%" from 1
  lead to 2 is mathematically correct but not a meaningful trend at
  that scale — the trend feature has no minimum-sample-size guard yet.
- **`suggest sop` drafts skew conservative.** Live validation showed it
  sometimes suggests "more information is needed" even when the reports
  given point fairly clearly at a specific fix — a deliberate trade-off
  (erring toward not inventing a fix over being maximally useful), not
  a bug.
- **The Automation Engine has five trigger types and three action types,
  not an arbitrary rule builder.** Deliberately scoped to what's real and
  reusable today (task/feedback/deposit/low-stock conditions; WhatsApp-
  notify-owner, create-task, and message-the-customer actions) rather
  than a generic condition/action DSL nobody asked for yet. `message_lead`
  (Phase 19) is scoped to lead-targeted triggers only — it does not
  duplicate the existing reengagement/reminder/winback cron jobs, which
  own their own specific messages; it's for a rule an owner configures
  themselves.
- **No per-rule scheduling or priority.** All of a tenant's enabled rules
  are evaluated every time the cron endpoint is hit, in creation order;
  there's no way to run one rule hourly and another daily, or to make one
  rule's action wait on another's outcome.
- **Recurring-feedback-theme re-fire only tracks count growth, not time
  decay.** A theme that stops recurring never "resets" — if it starts
  climbing again after a long quiet period, the rule refires correctly,
  but the comparison is always against the last alert's count, not a
  rolling window boundary.
- **Automation run ordering ties at one-second resolution.** Like every
  other timestamp in this app (`AuditLogStore` included),
  `AutomationRunStore.created_at` is second-resolution — two runs
  recorded within the same second have no guaranteed relative order in a
  tied query. Found while writing this sprint's own retry/give-up test;
  worked around there by asserting on status counts rather than row
  order, not by changing the storage format.
- **Command Center's "recommended actions" are deterministic templates,
  not an LLM-written brief.** By design (see V1.12 above — no cost, no
  latency on dashboard load), but that means the wording is fixed and
  generic compared to the digest email's `generate_action_brief`, which
  still does the richer, LLM-written version on its own once-a-day
  schedule.
- **WhatsApp Embedded Signup's actual Meta popup flow could not be
  live-verified end to end** — this deployment has no real
  `WHATSAPP_APP_ID`/`WHATSAPP_CONFIG_ID`/approved Meta Tech Provider
  registration. The frontend code (SDK load, `FB.login` call, the
  `WA_EMBEDDED_SIGNUP` message listener, the capability-flag gating that
  falls back to the manual fields) is real and reviewed against Meta's
  documented contract, and the server-side code-exchange it calls into
  has been tested since V1.4, but the popup interaction itself needs a
  real Meta App to confirm.
- **One platform subscription price, not multiple plans/tiers.**
  `PLATFORM_SUBSCRIPTION_PRICE_INR` is a single configurable number, not
  an invented set of named tiers with different features — a deliberate
  choice to avoid fabricating a pricing model that's a real business
  decision, not this app's to invent. Multiple tiers, if ever wanted, are
  a natural additive extension of the same mechanism.
- **The Razorpay webhooks understand the full `payment_link.*` event
  family only** (Phase 18 widened this from just `payment_link.paid` to
  also include `expired`/`cancelled`/`partially_paid`). `payment.*`/
  `refund.*`/`payment.dispute.*` events are still neither handled nor
  expected — a different, order-centric payload shape this app has no
  verified way to correlate back to a tenant/lead without a live
  Razorpay account to confirm the real shape against; Business AI's own
  billing also has no refund/dispute flow for those events to update
  even if they were parsed.
- **The self-serve checkout link has no expiry/retry UI.** If a Razorpay
  payment link goes unpaid, the owner can click "Pay Now" again to
  generate a fresh one, but there's no reminder, no automatic re-send,
  and no visibility into a specific failed payment attempt beyond the
  tenant's own Razorpay account.
- **The onboarding wizard's step navigation has no server-side
  "resume where you left off."** Which step is showing is pure
  client-side state (not persisted), by design — every real constraint
  (knowledge required, payment required if priced) is enforced by the
  actual backend regardless of which step the wizard happens to be
  showing, so there was nothing to gain from inventing server-tracked
  wizard progress on top of state that already exists for real reasons.
- **Rate limiting is in-memory and per-process.** `rate_limiting.py`'s
  IP/tenant counters reset on restart and aren't shared across multiple
  app instances — correct at today's single-instance scale (see
  ARCHITECTURE.md's "Scaling past one instance"), but would need a
  shared backend (Redis, most likely) the day this app runs more than
  one process at once, same trigger point as the SQLite storage layer.
- **Four `chromadb` advisories are explicitly ignored in CI**
  (`.github/workflows/security.yml`), not fixed — they're all in
  chromadb's HTTP server mode (unauthenticated API access, RBAC bypass),
  and this app only ever uses `chromadb.PersistentClient` (in-process, no
  network listener), so none are reachable in how this app actually uses
  the dependency. Re-audit this reasoning if retrieval.py ever moves to
  a networked chromadb deployment.
- **`SECRET_ENCRYPTION_KEY` migration is lazy by default.** A tenant's
  WhatsApp/Razorpay secrets are only encrypted the next time their config
  is written, unless an operator explicitly runs
  `scripts/rotate_secrets.py --apply` once after setting the key — a
  deployment that sets the key and never runs that script keeps
  plaintext secrets for any tenant that happens not to be updated again.
- **The router split's shared `ctx` object has no type-checked contract.**
  `RouteContext` (`routing_context.py`) is a plain, dynamically-typed
  attribute bag by design (see its own docstring for why), which means a
  typo in a cross-module `ctx.` reference is a runtime `AttributeError`
  on first use, not a static-analysis or import-time failure — mitigated
  today by `pyflakes` catching undefined bare names and the 361-test
  suite exercising nearly every route, but a genuinely different
  guarantee than a typed dependency-injection contract would give.
- **A "process" is defined by exact-normalized-title matching, not fuzzy
  grouping.** "Restock shelf 3" and "restock shelf #3" are the same
  process; "restock shelf 3" and "restock shelves" are not — an owner
  who titles the same recurring job inconsistently gets it split into
  separate, individually-lower-bus-factor processes instead of one
  correctly-counted one. A later phase could add fuzzy/embedding-based
  title grouping; this phase deliberately didn't, to keep the risk
  computation fully deterministic and explainable.
- **Knowledge concentration shows current SOP authorship only, not
  history.** `SopNote.approved_by_employee_id` records who approved the
  note as it stands today; if authorship changed hands, the prior
  author's now-transferred knowledge isn't reflected as a past
  concentration risk.
- **Dependency intelligence is a derived read model, recomputed on every
  request** — `compute_dependency_snapshot` re-scans a tenant's full
  task/employee/SOP/lead history each time `/api/dependency/map` or the
  scan cron runs, the same pattern as the existing business-health
  snapshot. Correct and simple at today's task volumes; a tenant with a
  very large task history would eventually warrant caching or
  incremental computation.
- **The proactive scan only fires WhatsApp alerts for bus-factor-1
  process risk**, not workload/knowledge/customer concentration — those
  stay dashboard-only. This mirrors the existing convention of reserving
  interrupting pushes for the most unambiguous risk, at the cost of an
  owner only discovering a concentration risk if they open the Business
  Map themselves.
- **Self-evolution's only versioned/proposable CONFIG_TYPE is one
  plain-text tone-guidance string** (Phase 11) — Phase 20 added
  per-tenant sensitivity SETTINGS (dissatisfaction threshold, lookback
  window, regression delta) but deliberately did not add a second
  CONFIG_TYPE, having concluded during that phase's own design step that
  automation-rule thresholds and inventory par levels don't fit the
  conversation-replay sandbox this pattern is built around (see V1.25's
  own "scoping decision" above) — adding a genuinely conversational
  second CONFIG_TYPE remains a natural extension of the exact same
  pattern if one is ever needed, explicitly added to
  `evolution.CONFIG_TYPES` with its own bounds, never a generic "any
  setting" path.
- **Failure detection is a single fixed threshold on one metric**
  (dissatisfaction rate over a 2-week window), not a themed/root-cause
  classifier — deliberately deterministic (no LLM call) so this stage is
  100% reproducible and reviewable, at the cost of every proposal
  carrying the same generic suggested tone text rather than one tailored
  to the specific complaint pattern.
- **Sandbox evaluation replays at most 5 historical questions**, and
  needs at least one real answered (non-abstained) question in a
  tenant's history to evaluate anything at all — a brand-new tenant with
  no real traffic yet gets `insufficient_data`, never a false "pass."
  Each sandbox run costs two real LLM calls per replayed question (spent
  only when a failure signal actually fires and only against the
  platform_admin-triggered scan cron, never on customer traffic).
- **Monitoring compares two 2-week windows around one activation
  timestamp** and needs at least 5 real questions in each to trust the
  comparison — a low-traffic tenant's regression could take longer than
  24 hours to actually get caught, simply because there isn't enough
  post-activation traffic yet to compute a reliable rate.
- **A version rolled back for one reason stays out of rotation until an
  owner (or a future proposal) reactivates it.** There's no "try it
  again automatically later" — an owner reviewing version history and
  clicking Restore, or a fresh proposal, are the only ways a rolled-back
  version returns.
- **The Financial Truth Layer has no PDF report generation** (Phase 12)
  — CSV export and a WhatsApp `financials` summary ship now; the earlier
  roadmap's `fpdf2` PDF report was deliberately deferred rather than
  adding a new rendering dependency mid-autonomous-run without a human
  checkpoint to review that tradeoff.
- **No dashboard UI for financials yet** (Phase 12) — `/api/metrics/
  summary` and `/api/metrics/export.csv` exist and are tested, but
  there's no dashboard panel for them today; viewing happens over
  WhatsApp (`financials`) or direct API/CSV.
- **Financial entries have no edit/delete.** A mis-logged sale or
  expense can't be corrected or removed today — only a fresh, correct
  entry can be logged alongside it. Real correction support (and
  probably an "edit window" policy) is a deliberate later addition, not
  an oversight.
- **The weekly scorecard has no persistent dedup** (Phase 13) — calling
  `/api/v1/admin/weekly-scorecard/run` twice in the same week resends
  the same numbers rather than being silently skipped like the
  dependency-risk cron. Harmless (a report, not an alert an owner needs
  to act on once), and deliberately simpler than adding a new audit-log
  dedup key for a job whose cadence the external cron already controls.
- **The scorecard's task-completion count uses `updated_at`, not a
  dedicated `completed_at` field** — a task marked done and then
  reassigned or edited again within the same week would have its
  `updated_at` bumped past the done-marking event, though `status`
  itself never leaves `"done"` from a normal workflow, so this is a
  narrow edge case, not a routine miscount.
- **Backups are local to the instance's own disk** (Phase 14) —
  `data_backups/` lives on the same Railway volume as `data/` itself, so
  a full volume loss takes the backups with it. This is real recovery
  from application-level corruption/mistakes (a bad migration, an
  accidental delete), not disaster recovery from infrastructure loss —
  that needs off-instance storage (S3 or similar), a deliberately
  separate, later decision rather than something to bolt on without a
  real object-storage credential to test against.
- **No scheduled/automatic backups** — `POST /api/v1/admin/backup/run`
  and `scripts/backup_data.py` both require something external to
  trigger them (a cron, a manual run); there's no in-process scheduler,
  consistent with every other `/run` endpoint's own documented
  reasoning (see ARCHITECTURE.md).
- **The data-integrity scanner checks tasks and SOP notes only** —
  employee/lead/task cross-references specifically, the ones most likely
  to silently degrade a rendered view. It doesn't yet check every
  possible cross-store reference in the codebase (e.g. automation rules
  referencing a deleted employee); extending it is additive, following
  the same pattern.
- **Revenue leakage's "estimated" figure covers unpaid deposits only**
  (Phase 15) — a missed-buying-intent lead or a no-show has no reliable
  rupee figure attached anywhere in the data model (no expected-sale
  amount is ever captured before a sale happens), so those two
  categories are reported as counts, not estimated amounts. Inventing a
  number for them would be a guess dressed up as data.
- **The Revenue Radar's "missed buying intent" window is fixed at 14
  days** and isn't yet a tenant-configurable setting — same category as
  several other fixed windows in this codebase (task escalation hours,
  reminder windows) that are implementation constants today, not owner
  dials, until a real business asks for control over the number.
- **The dashboard's Financials/Scorecard/Revenue Radar cards have no
  write actions** (Phase 16) — logging a financial entry, approving an
  evolution proposal from within Business Intel, or acting on a radar
  finding still requires WhatsApp or another dashboard section. This
  phase closed the "can't even SEE these numbers on the dashboard" gap;
  cross-linking the read views to their existing write actions is a
  natural, low-risk next UI pass.
- **The "Run Backup Now" dashboard button has no progress/size-limit
  handling** — a very large `data/` directory would make the button's
  synchronous HTTP call slow without any visible progress indicator.
  Fine at today's per-tenant SQLite scale; would need a background-job
  pattern (poll a status endpoint) if data volume grows substantially.
- **Purchases are receipts, not a full purchase-order lifecycle**
  (Phase 17) — there's no draft → sent → received workflow, no PO status
  tracking, and no way to record an order before it arrives. `log
  purchase` is "this stock just arrived," always in the past tense.
- **No unit conversion between ingredients** (Phase 17) — an ingredient
  must be logged in ONE consistent unit for its whole lifetime (always
  grams, or always kg, never mixed); `InventoryStore.adjust_quantity`
  fails closed with `UnitMismatchError` rather than silently converting
  or corrupting the number, but there's no "500g = 0.5kg" reconciliation
  built. An owner who needs to switch units must currently do so
  deliberately via the `POST /api/inventory/par-level` route (the one
  place a unit change is allowed to overwrite).
- **Recipe-based depletion has no undo.** Editing or deleting a logged
  dish sale doesn't currently exist, so a mis-logged sale's stock
  depletion can't be reversed except by manually logging a compensating
  purchase — the same "no edit/delete" limitation Phase 12's financial
  ledger already has, now inherited by dish sales too.
- **Wastage cost estimates use a simple lifetime average purchase
  price**, not the most recent price or a FIFO/LIFO costing method — a
  reasonable approximation for a small kitchen's own use, not
  accounting-grade inventory valuation.
- **The `log purchase`/`log waste` WhatsApp grammar requires a specific
  word order** (`log purchase <qty> <unit> <ingredient> ₹<amount> [from
  <supplier>]`, `log waste <qty> <unit> <ingredient>[: <reason>]`) —
  deterministic keyword parsing, same philosophy as Phase 0's task
  commands and the same tradeoff: 100% predictable and free, but "log 10
  kg of chicken, four thousand two hundred rupees" in free-form natural
  language doesn't parse yet.
- **A tenant's Razorpay webhook secret is opt-in and manual to set up**
  (Phase 18) — there's no guided "connect webhook" wizard; an owner must
  paste their webhook secret from their own Razorpay dashboard into
  `PUT /api/tenant`, the same manual-credential-entry shape every other
  bring-your-own-account integration in this app already has (WhatsApp,
  their own Razorpay key/secret).
- **Only the `payment_link.*` event family is understood, and only
  correlates to a LEAD via `reference_id`.** A payment made through any
  path OTHER than a deposit link this app itself created (e.g. a
  tenant's own separately-created Razorpay payment page) has no
  `reference_id` this app set and will never correlate to anything.
- **The nudge/resend-deposit-link recovery actions are WhatsApp-only**,
  matching the automated reengagement cron's own channel limitation — a
  lead with only an email address gets a clear error, not an email
  fallback.
- **`message_lead` (Phase 19) only ever works for a lead-targeted
  trigger** — `deposit_unpaid_after_appointment` today. Attaching it to
  a task/feedback/inventory rule fails every run cleanly (wrong-target-
  type error, recorded and eventually given up on) rather than silently
  messaging the wrong kind of target; a future lead-shaped trigger (e.g.
  a stale/unconverted lead) would automatically become eligible for it
  with no change to the action itself.
- **`escalate_after_hours` re-fires on a fixed interval, not a curve.**
  Set it to 24 and a still-true condition re-alerts every 24 hours
  exactly, forever, until resolved — there's no backoff (escalating
  faster the longer it's ignored) or cap (giving up after N escalations)
  yet, both natural extensions of the same field if a real tenant needs
  them.
- **`low_stock` needs a par level set first** (`set_par_level`, via the
  Restaurant section of the dashboard or the API) — an ingredient with
  no par level configured is invisible to this trigger by design (Phase
  17's own `list_low_stock` only returns `par_level > 0` rows), not a
  bug in the automation engine layered on top of it.
- **The LLM-drafted theme (Phase 20) is a label for the owner's benefit,
  not a stored taxonomy.** Unlike `FeedbackStore`'s fixed
  `FEEDBACK_THEMES` enum (internal employee feedback), a dissatisfied-
  customer-question theme is free text the LLM names fresh each time a
  proposal is generated — there's no theme aggregation/trend view across
  proposals over time, since there's no fixed vocabulary to aggregate
  against yet.
- **The three Self-Evolution sensitivity settings can be set but not
  explicitly cleared back to "platform default" from the dashboard** —
  same pre-existing limitation as every other optional `PUT /api/tenant`
  field (e.g. secrets); leaving the field blank keeps whatever was last
  saved rather than sending an explicit "unset" signal. An owner who
  wants the platform default back today sets the field to that default's
  own value.
