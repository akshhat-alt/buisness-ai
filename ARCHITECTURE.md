# Architecture

## Design goal

The smallest correct architecture for a grounded, multi-tenant business
assistant that a single local business can trust with real customer
conversations — and that can grow (more automation, more channels)
without a rewrite of the core.

## Relationship to Shri AI

Business AI is a **separate project** (separate git repo, separate
database files, separate deploy, separate secrets). It reuses several
proven *patterns* from Shri AI's codebase — adapted, not shared at
runtime:

| Pattern | Shri AI origin | Business AI adaptation |
|---|---|---|
| Fail-closed tenant isolation via a single `authorize()` chokepoint | `tenant/authorization.py` | `tenant.py` — simplified to 4 tenant-scoped/platform roles (owner/manager/staff, plus platform_admin) instead of Shri AI's broader role set |
| RAG: chunk → embed → retrieve → pre-LLM abstention gate → post-LLM citation/hallucination validator | `generation/{gates,validator}.py`, `retrieval/*` | `generation.py`, `retrieval.py` — same mechanics, first-person business-voice prompt instead of third-person teaching interpreter, plus two new structured-output fields (`shows_buying_intent`, `suggested_handoff`) driving lead capture/handoff without a second LLM call |
| SSRF-safe URL fetching (DNS + IP-range blocking, checked on every redirect hop) | `runtime/security.py` | `security.py` — copied near-verbatim; this exact guard is already proven |
| Atomic SQLite quota reservation (single-statement UPDATE, no race window) | `runtime/usage_limiter.py` | `usage_limiter.py` — Redis/distributed mode deliberately dropped; single Railway instance doesn't need it |
| PBKDF2 password hashing, JWT principals | `runtime/auth.py` | `auth.py` — trimmed role set; transactional email exists since V1.1 (`email_sender.py`) but there is still no password-*reset-token* flow (a distinct feature: token generation/expiry, a reset page) |

Nothing at runtime imports across the two projects. If Shri AI's code
changes, Business AI is unaffected, and vice versa.

## Request flow

```
Browser (chat widget, dashboard)
    |
    v
FastAPI app (app.py) — CORS, security headers, CSP
    |
    v
resolve_principal(Authorization header)  ->  Principal | None
    |
    v
authorize(principal, action, target_tenant_id, registry)   <- the one chokepoint
    |                                                          every tenant-scoped
    |  raises UnauthorizedError / TenantNotFoundError           route calls
    v
route handler (knowledge ingestion / ask / leads / analytics / admin)
```

### The authorization chokepoint (`tenant.py::authorize`)

Every tenant-scoped route calls this one function. It enforces, in order:

1. **Lifecycle gating**: `PUBLIC_ACTIONS` (currently `QUERY_ASSISTANT` and
   `VIEW_PUBLIC_INFO`) require the tenant to be `ACTIVE`. This is checked
   *before* the principal check, because a suspended/provisioning
   business's assistant must never answer regardless of who's asking —
   with one narrow, deliberate exception (Phase 8): the tenant's OWN
   owner/manager may query their own assistant while `PROVISIONING`
   (never `SUSPENDED`), so the onboarding wizard's "test before you
   activate" step is real rather than a silent 403. An anonymous caller
   or a different tenant's principal is never exempted.
2. **Anonymous access, deliberately narrow**: `PUBLIC_ACTIONS` are the
   *only* actions allowed with `principal=None`. This is what makes the
   customer-facing chat widget work with zero login — a real customer
   never has a Business AI account. Everything else (leads, analytics,
   knowledge management, tenant settings) requires authentication.
3. **Cross-tenant isolation**: a non-`platform_admin` principal can never
   act on a `target_tenant_id` other than their own. This is the single
   most important invariant in the system — verified by a dedicated test
   (`test_tenant_isolation_blocks_cross_business_access`) and re-checked
   at the vector-store layer (`VectorStore.search`/`upsert` fail closed
   without a `tenant_id` filter).
4. **Role-based action gating**: `OWNER_ACTIONS` / `STAFF_ACTIONS` for
   everything else.

This design was arrived at through a real bug: the first version rejected
*every* unauthenticated caller, which broke the actual product (a real
customer using the embedded widget has no login). The fix — a narrow,
explicit `PUBLIC_ACTIONS` allowlist, not a blanket relaxation — keeps the
chokepoint's guarantee intact for everything else.

## RAG pipeline

```
ingest_text() / fetch_website_text() / extract_pdf_text()
    -> chunk_text()                         (knowledge.py)
    -> embed + upsert into VectorStore       (retrieval.py, tenant_id-scoped)

ask() route:
    -> RetrievalEngine.retrieve()            (tenant-scoped vector search)
    -> evaluate_evidence_gate()              (pre-LLM abstention: pack_confidence < 0.35 -> abstain, never call the LLM)
    -> OpenAIGenerationProvider.generate()   (structured JSON-schema output)
    -> validate_llm_draft()                  (post-LLM: reject unknown/cross-tenant segment_ids, force abstention rather than trust an unverifiable claim)
```

The pre-LLM gate exists so a business with thin knowledge coverage never
gets a confident-sounding hallucination — it costs nothing (no LLM call)
and fails toward honesty. The post-LLM validator exists because even a
well-grounded model can occasionally cite something outside the evidence
pack; every citation is checked against the actual retrieved segments
before being trusted, and a citation belonging to a different tenant
(should the vector store ever return one, which it structurally
shouldn't) is treated as a hard stop, not a warning.

## Module layout

```
src/business_ai/
  config.py       Settings (env-driven), fail-closed startup validation
  security.py     SSRF guard, tenant-id validation
  auth.py         JWT principals, PBKDF2 user store
  tenant.py       TenantConfig, TenantRegistry, the authorize() chokepoint
  usage_limiter.py  Atomic SQLite quota/concurrency/rate-limit + kill switch
  knowledge.py    Text chunking
  retrieval.py    Embeddings, tenant-isolated vector store, retrieval engine
  generation.py   Prompts, abstention gate, LLM call, citation validator
  ingestion.py    SSRF-safe website fetch, PDF text extraction, SourceStore
  leads.py        Lead capture (SQLite) + appointment/reminder/reengagement/
                  winback/deposit tracking fields and query methods.
                  Phase 23 added party_size (a restaurant reservation IS
                  a Lead with an appointment) and
                  list_upcoming_appointments (the reservations-book
                  window query, same shape as list_for_reminders/
                  list_for_winback below)
  analytics.py    Conversation turn logging + summary (SQLite)
  email_sender.py  Thin Resend API client — the one place any HTML
                  email actually gets sent from
  alerts.py       Instant, event-triggered emails (a dissatisfaction
                  alert, a review request, task escalation) — fired
                  synchronously from the request path that detected the
                  trigger, never from a cron. Kept separate from
                  digest.py because these are a different kind of thing:
                  one-off reactions to a single event, not a rollup
  digest.py       The owner's scheduled daily summary email — pure
                  presentation over AnalyticsStore/LeadStore output, no
                  new data model
  whatsapp.py     WhatsApp Cloud API: webhook parsing/signature, send client,
                  redelivery idempotency (WhatsAppInboxStore), and
                  MetaEmbeddedSignupClient (one-click onboarding's OAuth
                  code exchange, gated behind WHATSAPP_APP_ID)
  payments.py     Razorpay payment-link client (deposit links) +
                  verify_razorpay_webhook_signature, shared by the
                  platform webhook and Phase 18's per-tenant one
  employees.py    Admin WhatsApp bot: tenant-scoped employee roster/identity
  tasks.py        Admin WhatsApp bot: task assignment, status, approvals
  audit.py        Append-only audit log for admin-bot state changes
  feedback.py     Admin WhatsApp bot: classified employee feedback (sentiment/
                  theme/urgency), theme aggregation for management review
  memory.py       Admin WhatsApp bot: owner-approved SOP/workaround notes
                  per recurring feedback theme
  automation.py   Automation Engine: owner-configured trigger/condition/
                  action rules (AutomationRuleStore) and their execution
                  history (AutomationRunStore) — evaluated by app.py's
                  admin/automation/run cron endpoint, never a scheduler.
                  Phase 19 added a low_stock trigger, a message_lead
                  action, and an opt-in escalate_after_hours re-fire
                  field generalized across every trigger type; low_stock
                  rules also fire eagerly and synchronously from
                  admin_bot.py right after a real WhatsApp depletion
                  event (dish sale / wastage), not only from the cron
  storage.py      Phase 9: SqliteStore, the shared connection-management
                  base every store above inherits (WAL, busy timeout,
                  thread lock) — one place to change if the storage
                  backend ever needs to move past SQLite
  secrets_vault.py  Phase 9: Fernet field-level encryption for a
                  tenant's WhatsApp/Razorpay secrets at rest
  observability.py  Phase 9: structured JSON logging + per-request
                  trace ids (RequestContextMiddleware)
  rate_limiting.py  Phase 9: IP/tenant-wide fixed-window abuse guard,
                  layered in front of usage_limiter's per-session quota
  tenant_data.py  Phase 9: full tenant data export + irreversible
                  deletion across every store
  dependency_graph.py  Phase 10: derived-read-model dependency graph —
                  process/bus-factor computation, workload/knowledge/
                  customer concentration risk, and the "what breaks if
                  X is unavailable" simulation (no new source-of-truth
                  store; reads employees/tasks/sop/leads directly)
  evolution.py    Phase 11: Self-Evolution Infrastructure — the
                  whitelisted/bounded behavior-config validator,
                  EvolutionVersionStore/EvolutionProposalStore/
                  EvolutionEvaluationStore, failure detection (reads
                  AnalyticsStore only), sandbox/shadow evaluation (real
                  retrieval+generation, never shown to a customer), and
                  the LLM-free post-promotion monitoring/auto-rollback
                  check. Can NEVER touch Python/SQL/infra/secrets/
                  money/activation — see the module's own docstring for
                  the enforced boundary. Phase 20 added themed, LLM-
                  drafted proposal text (generation.draft_tone_adjustment,
                  pre-validated through the same choke point, falls back
                  to the original deterministic text on any failure) and
                  three per-tenant sensitivity overrides threaded through
                  detect_failure_signal/run_monitoring_check
                  (TenantConfig.evolution_dissatisfaction_threshold/
                  evolution_lookback_hours/evolution_regression_delta) —
                  no new CONFIG_TYPE; see README's V1.25 section for why
                  automation-threshold/inventory-par-level tuning were
                  deliberately not added as new CONFIG_TYPEs here
  metrics.py      Phase 12: BusinessMetricStore — manual sales/expense/
                  collection ledger, always labeled source="manual"
  scorecard.py    Phase 13: weekly business scorecard — pure data-
                  assembly + render over tasks/analytics/metrics/
                  feedback/dependency-risk, no new store (same shape
                  as digest.py, weekly lens instead of rolling window)
  ops.py          Phase 14: backup/restore (tar.gz snapshot of data/)
                  and the read-only data-integrity scanner — no new
                  store, pure filesystem + read-only cross-store checks
  revenue_radar.py  Phase 15: Revenue Leakage Radar — missed buying
                  intent, unpaid deposits, no-shows; no new store,
                  reuses fields the existing automations already key off
  menu.py         Phase 17 (Restaurant Foundation): MenuStore — menu
                  items and their recipes (bill of materials), one
                  aggregate/two tables; ingredient identity is a
                  normalized name reusing dependency_graph.py's own
                  `_normalize_title` idiom (as normalize_ingredient_name)
  inventory.py    Phase 17: InventoryStore — ingredient-level stock,
                  keyed by the same normalized name; the only two ways
                  quantity_on_hand ever changes are a purchase receipt
                  or a depletion (sale/wastage), never a direct edit.
                  Fails closed (UnitMismatchError) on a unit that
                  conflicts with how an ingredient is already tracked,
                  rather than silently computing a wrong quantity
  suppliers.py    Phase 17: SupplierStore — the supplier directory
  purchases.py    Phase 17: PurchaseStore — purchase receipts (NOT a
                  full purchase-order lifecycle with draft/sent/received
                  states — that's a later, separate decision); each
                  receipt also increments InventoryStore
  wastage.py      Phase 17: WastageStore — wastage entries with an
                  honestly-estimated cost (0 when there's no purchase
                  history for that ingredient yet, never guessed);
                  depletes InventoryStore the same way a sale does
  menu_engineering.py  Phase 22 (Restaurant Profitability Intelligence):
                  pure data-assembly + render over menu/purchases/
                  metrics/inventory/automation-run stores, same shape
                  as scorecard.py/revenue_radar.py — no new store. Food
                  cost/profit/the four-quadrant menu-engineering
                  classification (per-unit contribution margin, not
                  aggregate profit dollars — the real methodology) and a
                  trailing-average demand-forecast baseline for every
                  active dish; separately, reorder suggestions reusing
                  automation.py's own AutomationRunStore low_stock
                  history (the reorder-suggestion idea explicitly
                  deferred from Phase 20's design decision — see
                  README's V1.25 section). Phase 24 (Restaurant
                  Autopilot) added build_menu_recommendations() (a
                  reviewable, never-auto-applied suggestion per dish —
                  applies through the EXISTING PATCH /api/menu/items/{id}
                  route, no new write path) and
                  simulate_menu_item_price() (the "Business Twin":
                  deterministic food-cost%/margin recomputation at a
                  hypothetical price, deliberately with no demand-
                  elasticity model — this app has no honest way to
                  estimate one)
  digital_gm.py   Phase 24: build_digital_gm_briefing() — pure
                  aggregation pulling the single most important line
                  from each of Phase 22/23's existing reports into one
                  view. Deliberately ON-DEMAND PULL ONLY, no new cron —
                  every other periodic job in this codebase is its own
                  external-cron-invoked endpoint, and this reuses that
                  same data rather than re-pushing it on a timer
  shifts.py       Phase 23 (Restaurant Operations Intelligence):
                  ShiftStore — staff working-hours scheduling, genuinely
                  new (EmployeeStore's "roster" is only who works here,
                  never when); one flat table, same SqliteStore pattern
                  as tasks.py
  customer_intelligence.py  Phase 23: pure data-assembly + render over
                  LeadStore data, same shape as scorecard.py/
                  revenue_radar.py — no new store. Groups leads by phone
                  (not session_id — a staff-logged reservation and an
                  organic WhatsApp lead don't share one) to find repeat
                  customers (2+ CONFIRMED completed visits), with their
                  owner-confirmed deposit total as the only revenue
                  figure — dish sales aren't linked to a customer
  supplier_intelligence.py  Phase 23: pure data-assembly + render over
                  SupplierStore/PurchaseStore data, same shape as above
                  — total spend/purchase count/distinct ingredients per
                  supplier, with an explicit "unattributed spend" total
                  for purchases whose supplier name never matched
                  (surfacing find_by_name's exact-match-only gap, not
                  fixing it — see README's Known Limitations)
  formatting.py   Pure formatting/parsing helpers with no store/ctx
                  dependency (appointment time parsing, WhatsApp links)
  schemas.py      Every HTTP request/response Pydantic model
  constants.py    PROJECT_ROOT/DATA_ROOT/STATIC_DIR + automation-window
                  constants — computed once here, never re-derived
  routing_context.py  Phase 9: RouteContext, the plain attribute bag
                  routers/* modules use to share cross-cutting helpers
  routers/        Phase 9: one register_X(app, svc, ctx) module per
                  domain — admin_bot (the core engine: auth resolution,
                  the grounded-answer pipeline, business-health/
                  automation-firing, plus the WhatsApp webhook and admin-
                  bot command grammar), auth_routes, customer_routes,
                  leads_routes, knowledge_routes, tenant_settings_routes,
                  team_routes, feedback_routes, automation_routes,
                  insights_routes, webhook_routes, admin_routes,
                  static_pages, dependency_routes (Phase 10: the
                  Business Map's map/simulate routes), evolution_routes
                  (Phase 11: proposal review/approve/reject, version
                  history/rollback, kill switch, and the two evolution
                  cron endpoints), metrics_routes (Phase 12: financial
                  summary/CSV export), ops_routes (Phase 14: backup
                  trigger/list, detailed health, data integrity),
                  revenue_radar_routes (Phase 15), scorecard_routes
                  (Phase 16: on-demand read of scorecard.py's data,
                  zero side effects), restaurant_routes (Phase 17:
                  menu/recipe/supplier/inventory setup CRUD — API/
                  dashboard-driven, deliberately not a WhatsApp grammar;
                  the high-frequency log sale/purchase/waste commands
                  live in admin_bot.py instead). Phase 21 added
                  POST /api/metrics (dashboard financial logging,
                  metrics_routes.py), POST /api/tasks/{id}/approve|reject
                  (team_routes.py — task approval's first REST route,
                  reusing ctx._maybe_verify_outcome_with_customer now
                  exposed from admin_bot.py), and GET /api/approvals
                  (insights_routes.py — the unified Approval Inbox,
                  aggregating awaiting_approval tasks + pending evolution
                  proposals with per-item-type authorize(), never a
                  blanket check). Phase 21 also removed three routes
                  (business-health, command-center, timeline) that were
                  accidentally duplicated byte-for-byte in
                  feedback_routes.py since the Phase 9 extraction —
                  dead code, silently shadowed by insights_routes.py's
                  copies the whole time (Starlette matches routes in
                  registration order); feedback_routes.py now contains
                  only feedback/SOP routes, matching its own docstring.
                  Phase 22 added menu_engineering_routes.py
                  (GET /api/menu-engineering, GET /api/reorder-suggestions,
                  both VIEW_INVENTORY-gated, both pure reads). Phase 23
                  added shift routes to team_routes.py (POST/GET/DELETE
                  /api/shifts, MANAGE_SHIFTS/VIEW_SHIFTS-gated),
                  GET /api/customer-behavior to leads_routes.py
                  (VIEW_LEADS-gated) alongside a new
                  GET /api/leads/upcoming-appointments and party_size on
                  the existing appointment route, and
                  GET /api/supplier-intelligence to restaurant_routes.py
                  (VIEW_INVENTORY-gated). Phase 24 added
                  GET /api/menu-recommendations,
                  POST /api/menu-engineering/simulate, and
                  GET /api/digital-gm-briefing to
                  menu_engineering_routes.py (all VIEW_INVENTORY-gated)
                  — see app.py's create_app() for wiring
  app.py          FastAPI app factory: Services + middleware + calls
                  every routers/register_X — the routes themselves moved
                  to routers/ in Phase 9, this file no longer defines any
static/           Vanilla HTML/CSS/JS frontend, no build step — includes
                  onboarding.html (Phase 8's guided setup wizard, served
                  at /onboarding, the new-signup landing page)
scripts/          rotate_secrets.py — one-time secret encryption /
                  key-rotation tool for secrets_vault.py (Phase 9);
                  backup_data.py / restore_data.py — data/ snapshot +
                  restore CLI wrapping ops.py (Phase 14)
tests/            pytest suite (728 tests) — see README.md
```

Every store (`TenantRegistry`, `UserStore`, `LeadStore`, `AnalyticsStore`,
`SourceStore`, `VectorStore`) is its own SQLite database (or Chroma
collection) under `data/`, anchored to the project directory via
`__file__` — never to the launching process's current working
directory. (This was a real bug caught in dev: a cwd-relative path
briefly caused the app to try opening a *different* project's database
when launched from a different directory.)

## Extending without a rewrite

The MVP is intentionally narrow, but the seams for later automation are
already in the right places:

- **New channels**: WhatsApp is now built this exact way — `whatsapp.py`
  is a thin adapter (webhook parsing, signature verification, an HTTP
  send client) and `routers/admin_bot.py`'s `_process_question()` (moved
  here from `app.py` in the Phase 9 router split) is the one grounded-
  answer pipeline both the website widget and the WhatsApp webhook call;
  neither the RAG/auth/tenant core nor `/api/ask` itself had to change.
  The same shape applies to a future channel (SMS, Instagram DM): a new
  thin adapter calling `_process_question()` + `LeadStore`/
  `AnalyticsStore`, nothing more.
- **New structured signals** (e.g. `wants_appointment_booking`): add a
  field to `RESPONSE_SCHEMA` and `LLMResponseDraft`/`GroundedAnswer`,
  same pattern as `shows_buying_intent`/`suggested_handoff` — no second
  LLM call, no new service.
- **New tenant actions** (e.g. a future `MANAGE_INTEGRATIONS`): add to
  `TenantAction`, assign to `OWNER_ACTIONS`/`MANAGER_ACTIONS`/
  `STAFF_ACTIONS`/`PUBLIC_ACTIONS` via the `ROLE_ACTIONS` map as
  appropriate — `authorize()` itself doesn't change, and an action left
  off every role's set is simply unreachable by anyone but a
  platform_admin, by construction (fail-closed, not fail-open).
- **A second bot on the same number**: the admin WhatsApp bot
  (`employees.py`/`tasks.py`) reuses the exact same webhook, signature
  verification, and inbox-idempotency machinery as the customer bot —
  the only new thing is `EmployeeStore.find_by_whatsapp`, checked before
  the customer lead/RAG path, so an employee's message is routed to
  `_handle_admin_bot_message` and a customer's isn't. Neither pipeline
  forks the other's code; a future third audience (e.g. a supplier
  channel) would follow the identical shape — resolve identity, branch,
  never touch `_process_question`.
- **Scaling past one instance**: `UsageLimiter` and the SQLite stores are
  the parts that would need to move to a shared backend (Postgres +
  Redis, mirroring Shri AI's distributed-mode design) — everything above
  that layer (routes, RAG logic, auth) is unaffected by that migration.
