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
| Fail-closed tenant isolation via a single `authorize()` chokepoint | `tenant/authorization.py` | `tenant.py` — simplified to 3 roles (owner/staff/platform_admin) instead of Shri AI's broader role set |
| RAG: chunk → embed → retrieve → pre-LLM abstention gate → post-LLM citation/hallucination validator | `generation/{gates,validator}.py`, `retrieval/*` | `generation.py`, `retrieval.py` — same mechanics, first-person business-voice prompt instead of third-person teaching interpreter, plus two new structured-output fields (`shows_buying_intent`, `suggested_handoff`) driving lead capture/handoff without a second LLM call |
| SSRF-safe URL fetching (DNS + IP-range blocking, checked on every redirect hop) | `runtime/security.py` | `security.py` — copied near-verbatim; this exact guard is already proven |
| Atomic SQLite quota reservation (single-statement UPDATE, no race window) | `runtime/usage_limiter.py` | `usage_limiter.py` — Redis/distributed mode deliberately dropped; single Railway instance doesn't need it |
| PBKDF2 password hashing, JWT principals | `runtime/auth.py` | `auth.py` — trimmed to 3 roles, no password-reset-token flow (no email delivery exists yet) |

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
   business's assistant must never answer regardless of who's asking.
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
                  winback/deposit tracking fields and query methods
  analytics.py    Conversation turn logging + summary (SQLite)
  whatsapp.py     WhatsApp Cloud API: webhook parsing/signature, send client,
                  redelivery idempotency (WhatsAppInboxStore)
  payments.py     Razorpay payment-link client (deposit links)
  app.py          FastAPI app factory: wires everything into HTTP routes
static/           Vanilla HTML/CSS/JS frontend, no build step
tests/            pytest suite (89 tests) — see README.md
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
  send client) and `app.py`'s `_process_question()` is the one grounded-
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
  `TenantAction`, assign to `OWNER_ACTIONS`/`STAFF_ACTIONS`/
  `PUBLIC_ACTIONS` as appropriate — `authorize()` itself doesn't change.
- **Scaling past one instance**: `UsageLimiter` and the SQLite stores are
  the parts that would need to move to a shared backend (Postgres +
  Redis, mirroring Shri AI's distributed-mode design) — everything above
  that layer (routes, RAG logic, auth) is unaffected by that migration.
