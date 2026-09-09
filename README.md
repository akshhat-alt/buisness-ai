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
  emails that customer a review-link request. Deliberately owner-
  triggered, not automatic off a buying-intent-style chat signal — the
  assistant has no way to know a service was actually delivered, and
  asking too early would look presumptuous.

## What v1 deliberately does not do

Not a CRM, not a website builder, not a workflow-automation platform. No
billing. No customer-facing email (only the owner-facing digest above —
no password-reset flow, no email support inbox). No multi-instance/
distributed mode — single SQLite + local Chroma, correct for one Railway
instance. No WhatsApp Business API (only click-to-chat handoff links).
These are scoped omissions, not oversights: the goal is the smallest
product a real local business would actually pay for, not "everything a
business could ever want."

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

## Tests

```bash
pytest
```

57 tests covering the full HTTP lifecycle (signup → ingest → activate →
grounded ask → quota → leads → analytics → tenant isolation), the
knowledge-gap closer (draft → publish → gap resolves → assistant answers
from the new FAQ entry), owner digest (sends only to active tenants with
activity, includes the action brief, skips gracefully when
unconfigured), the real-time dissatisfaction alert and review-request
flows (including tenant isolation), the `authorize()` chokepoint,
chunking edge cases, usage-limiter behavior, and the SSRF guard. All
offline — no OpenAI cost — using a deterministic fake generator and a
word-overlap fake embedding provider that still exercises the real
evidence-gate confidence threshold.

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
