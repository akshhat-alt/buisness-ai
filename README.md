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

## What v1 deliberately does not do

Not a CRM, not a website builder, not a workflow-automation platform. No
billing. No email delivery. No multi-instance/distributed mode — single
SQLite + local Chroma, correct for one Railway instance. These are
scoped omissions, not oversights: the goal is the smallest product a real
local business would actually pay for, not "everything a business could
ever want."

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

## Tests

```bash
pytest
```

42 tests covering the full HTTP lifecycle (signup → ingest → activate →
grounded ask → quota → leads → analytics → tenant isolation), the
`authorize()` chokepoint, chunking edge cases, usage-limiter behavior,
and the SSRF guard. All offline — no OpenAI cost — using a deterministic
fake generator and a word-overlap fake embedding provider that still
exercises the real evidence-gate confidence threshold.

## Known limitations (v1, honestly stated)

- **Quota/rate-limiting is per session_id, not per verified identity.** A
  customer can technically get a fresh quota by starting a new chat
  session. This mirrors the accepted risk profile of the pattern it's
  adapted from — it's an abuse deterrent, not a hard billing cap.
- **No password-reset flow.** No email service is wired up yet; a reset
  token with nowhere to send it would be a dead end.
- **Single Railway instance.** SQLite + local Chroma. Correct for the
  current scale; would need real infra work (managed Postgres, a hosted
  vector DB, horizontal scaling) before this could serve many
  high-traffic tenants concurrently.
