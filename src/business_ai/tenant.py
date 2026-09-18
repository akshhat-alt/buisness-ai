"""Tenant (business) model, registry, and authorization.

Adapted from Shri AI's tenant/schema.py + registry.py + authorization.py —
same proven shape (fail-closed status gating, a single authorization
chokepoint, one tenant can never act on another's data) — but persisted in
SQLite instead of one JSON file per tenant directory. That's a deliberate
simplification, not a missing feature: one table is fewer moving parts than
a hybrid in-memory-dict-plus-filesystem design, and SQLite is already the
persistence choice for every other Business AI store.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Any, Generator

from pydantic import BaseModel, Field

from business_ai.auth import Principal
from business_ai.secrets_vault import decrypt_secret, encrypt_secret
from business_ai.storage import SqliteStore

logger = logging.getLogger(__name__)


class TenantStatus(str, Enum):
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DISABLED = "disabled"


class TenantConfig(BaseModel):
    tenant_id: str
    business_name: str
    owner_email: str
    assistant_name: str = "Assistant"
    welcome_message: str = "Hi! How can I help you today?"
    whatsapp_number: str | None = None  # E.164 without "+", e.g. "919329999716" (click-to-chat handoff link)
    # WhatsApp Business Cloud API — this tenant's OWN WhatsApp Business
    # number, registered by the owner in their own Meta developer console.
    # Business AI has one shared Meta App/webhook (platform-level
    # WHATSAPP_APP_SECRET/WHATSAPP_VERIFY_TOKEN); each tenant brings their
    # own phone_number_id + permanent access token for THEIR number, the
    # same "bring your own credential" shape as a tenant's review_link.
    whatsapp_phone_number_id: str | None = None
    whatsapp_access_token: str | None = None
    # The OWNER's (or staff's) own personal WhatsApp number — E.164 without
    # "+". Two things read this: (1) real-time complaint alerts and the
    # daily digest are pushed here over WhatsApp as a best-effort fast
    # path alongside the guaranteed email; (2) any inbound message FROM
    # this number on the tenant's WhatsApp line is treated as an internal
    # owner command ("what needs my attention today?"), not a customer
    # question — see app.py's webhook handler.
    owner_whatsapp_number: str | None = None
    # Name of a pre-approved Meta message template (created by the tenant
    # in their own Meta Business Manager — same bring-your-own shape as
    # everything else here) used ONLY as a fallback when a proactive
    # admin-bot notification (daily digest, urgent feedback alert) can't
    # be sent as free-form text because nobody on the roster has messaged
    # the business's line in the last 24h. None = no fallback; the
    # notification is simply skipped on WhatsApp (email stays guaranteed).
    admin_notify_template_name: str | None = None
    review_link: str | None = None  # e.g. a Google Business review URL, for review-request emails
    # Deposit/payment links (Razorpay) — same "bring your own credential"
    # shape as WhatsApp: the payment goes straight into the TENANT's own
    # Razorpay account, never through a Business AI-held balance.
    razorpay_key_id: str | None = None
    razorpay_key_secret: str | None = None
    # Phase 18: a tenant's OWN Razorpay webhook secret (set once in their
    # own Razorpay dashboard, pointing at POST /api/webhooks/razorpay/
    # {tenant_id}) — closes the gap the original README explicitly named
    # ("no payment-status webhook... the owner checks their own Razorpay
    # dashboard for now"). None = reconciliation stays manual for this
    # tenant, exactly as before this field existed; nothing breaks.
    razorpay_webhook_secret: str | None = None
    deposit_amount_inr: int | None = None  # whole rupees; unset = deposit-link action is disabled
    # Customer win-back: how many days without a repeat visit counts as
    # "lapsed" for THIS business. None = platform default (config.
    # winback_default_days) — a salon's natural cadence is weeks, a
    # dental clinic's is months, so this can't be one hardcoded number.
    winback_after_days: int | None = None
    # Business AI's OWN subscription revenue from this tenant — separate
    # from razorpay_key_id/secret above, which are the TENANT's own
    # account for collecting deposits from THEIR customers. None/0 means
    # this tenant was never priced (e.g. an early free pilot) and the
    # activation gate below simply doesn't apply to them.
    subscription_price_inr: int | None = None
    billing_status: str = "unbilled"  # "unbilled" | "invoiced" | "paid"
    billing_link_sent_at: str | None = None
    billing_paid_at: str | None = None
    # Phase 2 — real recurring billing via Razorpay Subscriptions,
    # additive to the billing_status/billing_paid_at fields above (which
    # stay exactly as they are for the one-time Payment Links path).
    # None for every tenant until this tenant is actually migrated to a
    # real subscription — nothing reads these until then.
    platform_subscription_id: str | None = None
    # Mirrors Razorpay's own subscription status vocabulary verbatim
    # (authenticated/active/pending/halted/cancelled/completed/paused) —
    # not a value this app invents, so a webhook handler can just copy
    # Razorpay's `status` field straight through.
    platform_subscription_status: str | None = None
    platform_subscription_current_period_end: str | None = None
    status: TenantStatus = TenantStatus.PROVISIONING
    question_quota: int | None = None  # None = platform default (see config.active_tenant_quota)
    # Owner-facing automation kill switch (Phase 6) — default True so
    # existing tenants keep working with zero action; an owner can flip
    # this to False to instantly stop every automation rule from taking
    # any action for their business, without touching individual rules'
    # enabled flags. Checked once, fail-closed, at the top of the
    # automation cron run — see app.py's admin_run_automation.
    automation_enabled: bool = True
    # Self-Evolution Infrastructure (Phase 11) — an explicit OPT-IN kill
    # switch, default False, unlike automation_enabled's default-True.
    # Self-evolution can change a tenant's live customer-assistant tone;
    # it must never start doing that for a tenant who never asked for it.
    # When False, the evolution-scan/evolution-monitor crons skip this
    # tenant entirely — no proposals are ever generated, and an already-
    # active self-evolved version simply stops updating (it isn't rolled
    # back just because the switch flips off; an owner who wants that
    # uses the explicit manual rollback endpoint).
    evolution_enabled: bool = False
    # Phase 20 — per-tenant overrides of evolution.py's global detection/
    # monitoring constants ("more configurable levers"). None (the
    # default for every tenant, including every one that existed before
    # this phase) means "use the platform default from constants.py" —
    # these are optional sensitivity dials, not required configuration.
    # Bounds are enforced at the API layer (schemas.TenantConfigUpdate),
    # not here, so a value already in the database is trusted as-is.
    evolution_dissatisfaction_threshold: float | None = None
    evolution_lookback_hours: int | None = None
    evolution_regression_delta: float | None = None
    # Phase 25 — Perception & Input Expansion. This tenant's own Google
    # Place ID (from their Google Business Profile) — the platform-level
    # GOOGLE_PLACES_API_KEY (Settings) does the actual API call, this
    # says WHICH business to look up. None = the automated review-sync
    # cron skips this tenant; manual review logging is unaffected.
    google_place_id: str | None = None
    # Voice-note transcription (Whisper, via the OpenAI key this app
    # already requires) — an explicit opt-in, default False like
    # evolution_enabled, because every transcribed voice note spends
    # this tenant's own OpenAI usage. When False, an inbound voice note
    # gets a reply asking the employee to type instead, never silently
    # dropped and never transcribed without consent.
    voice_notes_enabled: bool = False
    # Phase 1 monetization infrastructure — which plan tier this tenant is
    # on. Every tenant that existed before this field was added simply
    # gets "starter" when their stored JSON (which has no "plan" key) is
    # parsed, the same zero-migration pattern every other field addition
    # in this file already relies on. Validated as a plain str, not a
    # pydantic Literal, so a not-yet-recognized plan value loaded from an
    # old row fails closed via PLAN_ACTIONS.get(..., frozenset()) in
    # authorize() below, rather than raising at load time.
    plan: str = "starter"
    created_at: str = Field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


class TenantNotFoundError(ValueError):
    pass


class TenantNotActiveError(ValueError):
    pass


class UnauthorizedError(ValueError):
    pass


class TenantAction(str, Enum):
    QUERY_ASSISTANT = "query_assistant"
    VIEW_PUBLIC_INFO = "view_public_info"
    INGEST_KNOWLEDGE = "ingest_knowledge"
    VIEW_LEADS = "view_leads"
    VIEW_ANALYTICS = "view_analytics"
    MANAGE_ASSISTANT = "manage_assistant"
    ACTIVATE_TENANT = "activate_tenant"
    SUSPEND_TENANT = "suspend_tenant"
    INSPECT_ALL_TENANTS = "inspect_all_tenants"
    # Admin WhatsApp bot (employee coordination) — see employees.py/tasks.py.
    MANAGE_EMPLOYEES = "manage_employees"
    ASSIGN_TASK = "assign_task"
    VIEW_TASKS = "view_tasks"
    UPDATE_TASK_STATUS = "update_task_status"
    # Employee feedback/sentiment — see feedback.py. Owner+manager only:
    # aggregated peer feedback is more sensitive than task/lead data, and
    # is deliberately NOT in STAFF_ACTIONS (submitting feedback over
    # WhatsApp needs no permission check at all — anyone in the roster
    # can send a message; VIEWING the aggregated result is the gated part).
    VIEW_FEEDBACK = "view_feedback"
    # Approving a workaround/SOP note for a recurring feedback theme is a
    # policy action, owner-only like MANAGE_EMPLOYEES — see memory.py.
    MANAGE_SOPS = "manage_sops"
    # Automation Engine (Phase 6) — see automation.py. Creating/editing/
    # deleting rules and flipping the kill switch is owner-only policy,
    # like MANAGE_SOPS; viewing rules and execution history is available
    # to managers too, like VIEW_FEEDBACK/VIEW_TASKS.
    MANAGE_AUTOMATION = "manage_automation"
    VIEW_AUTOMATION = "view_automation"
    # Self-Evolution Infrastructure (Phase 11) — see evolution.py.
    # Owner-only, no manager/staff variant at all: this action gates
    # every proposal/version/rollback/kill-switch route, and it changes
    # the live customer assistant's behavior, which is a strictly
    # higher-stakes lever than MANAGE_AUTOMATION's rule CRUD.
    MANAGE_EVOLUTION = "manage_evolution"
    # Financial Truth Layer (Phase 12) — see metrics.py. Logging an entry
    # needs no permission check at all (same shape as feedback
    # submission — any roster member can log a sale/expense/collection
    # over WhatsApp); VIEWING the aggregated total is the gated part,
    # owner+manager only, like VIEW_FEEDBACK.
    VIEW_FINANCIALS = "view_financials"
    # Restaurant Foundation (Phase 17) — see menu.py/suppliers.py.
    # Menu/recipe/supplier setup is config, not a quick floor action —
    # owner+manager, same sensitivity tier as VIEW_FINANCIALS. Logging a
    # purchase/wastage/dish-sale over WhatsApp needs no permission check
    # at all, identical shape to Phase 12's financial log commands.
    MANAGE_MENU = "manage_menu"
    VIEW_INVENTORY = "view_inventory"
    # Phase 23 (Restaurant Operations Intelligence) — see shifts.py.
    # Same pairing as ASSIGN_TASK/VIEW_TASKS: scheduling is a day-to-day
    # operating decision (owner+manager), not an owner-only lever like
    # MANAGE_EMPLOYEES; every roster member can see their OWN shifts
    # (enforced by row-scoping in the route/WhatsApp handler, not by this
    # action, matching VIEW_TASKS' own precedent).
    MANAGE_SHIFTS = "manage_shifts"
    VIEW_SHIFTS = "view_shifts"


OWNER_ACTIONS = frozenset(
    {
        TenantAction.QUERY_ASSISTANT,
        TenantAction.VIEW_PUBLIC_INFO,
        TenantAction.INGEST_KNOWLEDGE,
        TenantAction.VIEW_LEADS,
        TenantAction.VIEW_ANALYTICS,
        TenantAction.MANAGE_ASSISTANT,
        TenantAction.MANAGE_EMPLOYEES,
        TenantAction.ASSIGN_TASK,
        TenantAction.VIEW_TASKS,
        TenantAction.UPDATE_TASK_STATUS,
        TenantAction.VIEW_FEEDBACK,
        TenantAction.MANAGE_SOPS,
        TenantAction.MANAGE_AUTOMATION,
        TenantAction.VIEW_AUTOMATION,
        TenantAction.MANAGE_EVOLUTION,
        TenantAction.VIEW_FINANCIALS,
        TenantAction.MANAGE_MENU,
        TenantAction.VIEW_INVENTORY,
        TenantAction.MANAGE_SHIFTS,
        TenantAction.VIEW_SHIFTS,
    }
)

# Day-to-day operating power (assign/approve tasks, everything a staff
# member can do) without owner-only levers: can't touch the knowledge
# base, assistant config, the employee roster, SOP policy, the
# automation engine's rules/kill switch, or self-evolution — a manager
# can SEE what automation is doing (VIEW_AUTOMATION) but not change what
# it does, and has no visibility or control over evolution at all.
MANAGER_ACTIONS = OWNER_ACTIONS - frozenset(
    {
        TenantAction.INGEST_KNOWLEDGE,
        TenantAction.MANAGE_ASSISTANT,
        TenantAction.MANAGE_EMPLOYEES,
        TenantAction.MANAGE_SOPS,
        TenantAction.MANAGE_AUTOMATION,
        TenantAction.MANAGE_EVOLUTION,
    }
)

STAFF_ACTIONS = frozenset(
    {
        TenantAction.QUERY_ASSISTANT,
        TenantAction.VIEW_PUBLIC_INFO,
        TenantAction.VIEW_LEADS,
        TenantAction.VIEW_ANALYTICS,
        TenantAction.VIEW_TASKS,
        TenantAction.UPDATE_TASK_STATUS,
        TenantAction.VIEW_SHIFTS,
    }
)

# Fail-closed role -> allowed-actions lookup for authorize() below. An
# explicit map, not an if/elif chain that falls through to a default: a
# role string that doesn't appear here (a typo, a future role added to
# auth.ROLES without being wired in here yet) gets the empty set, not
# STAFF_ACTIONS by accident. Keep this in sync with auth.ROLES.
ROLE_ACTIONS: dict[str, frozenset[TenantAction]] = {
    "owner": OWNER_ACTIONS,
    "manager": MANAGER_ACTIONS,
    "staff": STAFF_ACTIONS,
}

# ==============================================================================
# Plan tiers (Phase 1 monetization infrastructure) — a SECOND, independent
# dimension of authorization alongside role. A principal must clear BOTH
# the role check above and the plan check below; neither substitutes for
# the other. This mirrors ROLE_ACTIONS' own shape deliberately: an
# explicit map, fail-closed via .get(plan, frozenset()), never an
# if/elif chain that could silently fall through to a permissive default.
# ==============================================================================

# Starter — the core WhatsApp/website assistant, lead capture, staff
# roster and tasks, and basic owner-configured automations. No vertical
# intelligence, no staff-feedback/SOP layer, no financials/restaurant
# modules, no self-evolution.
STARTER_ACTIONS = frozenset(
    {
        TenantAction.QUERY_ASSISTANT,
        TenantAction.VIEW_PUBLIC_INFO,
        TenantAction.INGEST_KNOWLEDGE,
        TenantAction.VIEW_LEADS,
        TenantAction.VIEW_ANALYTICS,
        TenantAction.MANAGE_ASSISTANT,
        TenantAction.MANAGE_EMPLOYEES,
        TenantAction.ASSIGN_TASK,
        TenantAction.VIEW_TASKS,
        TenantAction.UPDATE_TASK_STATUS,
        TenantAction.MANAGE_AUTOMATION,
        TenantAction.VIEW_AUTOMATION,
    }
)

# Growth adds staff-operations intelligence (feedback/SOPs), financials
# (also what reviews.py and the restaurant "Ask Your Business Anything"
# report types reuse — see reviews_routes.py/business_query_routes.py,
# which authorize against VIEW_FINANCIALS/VIEW_INVENTORY/etc. rather than
# a dedicated action of their own), and the restaurant/shift modules.
GROWTH_ACTIONS = STARTER_ACTIONS | frozenset(
    {
        TenantAction.VIEW_FEEDBACK,
        TenantAction.MANAGE_SOPS,
        TenantAction.VIEW_FINANCIALS,
        TenantAction.MANAGE_MENU,
        TenantAction.VIEW_INVENTORY,
        TenantAction.MANAGE_SHIFTS,
        TenantAction.VIEW_SHIFTS,
    }
)

# Scale adds Self-Evolution — the most sensitive lever in the system (it
# can change the live customer-assistant's tone), on top of everything
# Growth already includes.
SCALE_ACTIONS = GROWTH_ACTIONS | frozenset({TenantAction.MANAGE_EVOLUTION})

# Fail-closed plan -> allowed-actions lookup, same shape as ROLE_ACTIONS.
# A plan string that doesn't appear here (a typo, a not-yet-supported
# future tier, or simply garbage in a hand-edited row) gets the empty
# set, not STARTER_ACTIONS by accident.
PLAN_ACTIONS: dict[str, frozenset[TenantAction]] = {
    "starter": STARTER_ACTIONS,
    "growth": GROWTH_ACTIONS,
    "scale": SCALE_ACTIONS,
}

# Actions a real, anonymous website visitor (a customer, not a platform
# user) may call with no Bearer token at all. Deliberately a short list:
# only what the embeddable chat widget itself needs (ask a question, read
# the business's public display name/welcome message). Everything else —
# leads, analytics, knowledge management — requires a logged-in owner or
# staff account. A real end-to-end browser test caught this: the chat
# widget is meant for a business's own customers, who will never have a
# Business AI login, and the original authorize() rejected every
# unauthenticated caller unconditionally, breaking the actual product.
PUBLIC_ACTIONS = frozenset({TenantAction.QUERY_ASSISTANT, TenantAction.VIEW_PUBLIC_INFO})

# Actions a non-ACTIVE tenant may never perform, regardless of role, except
# platform_admin managing its own lifecycle (activate/suspend/inspect).
# Deliberately just the two customer-facing actions above. VIEW_LEADS/
# VIEW_ANALYTICS/knowledge-source listing are the OWNER's own dashboard
# views — they must keep working during PROVISIONING, since that's
# exactly when an owner is setting up and checking progress (caught by a
# real end-to-end test: gating these too broke "see what I've uploaded so
# far" before activation).
CUSTOMER_FACING_ACTIONS = PUBLIC_ACTIONS


class TenantRegistry(SqliteStore):
    """Thread-safe SQLite store for tenant (business) configuration."""

    def __init__(self, db_path: Path | str = "data/tenants.db", *, secret_encryption_key: str | None = None) -> None:
        super().__init__(db_path)
        # Phase 9: encrypts whatsapp_access_token/razorpay_key_secret at
        # rest — see secrets_vault.py. None (the default, and every call
        # site until Services wires the real setting through) means
        # those fields stay plaintext, exactly as before this existed.
        self._secret_encryption_key = secret_encryption_key
        self._init_db()

    def _init_db(self) -> None:
        with self._lock, self._db() as conn:
            self._apply_default_pragmas(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tenants (
                    tenant_id TEXT PRIMARY KEY,
                    config_json TEXT NOT NULL
                )
                """
            )
            conn.commit()
        self.migrate_grandfathered_tenants()

    def migrate_grandfathered_tenants(self) -> int:
        """One-time, idempotent startup step: for every existing tenant row
        where `plan` is NULL/missing/absent in storage, set it to "scale".
        Tenants created after this step run default through the existing normal
        path to "starter".
        """
        migrated = 0
        with self._lock, self._db() as conn:
            rows = conn.execute("SELECT tenant_id, config_json FROM tenants").fetchall()
            for r in rows:
                try:
                    raw = json.loads(r["config_json"])
                except Exception:
                    continue
                if "plan" not in raw or raw.get("plan") is None:
                    raw["plan"] = "scale"
                    conn.execute(
                        "UPDATE tenants SET config_json = ? WHERE tenant_id = ?",
                        (json.dumps(raw), r["tenant_id"]),
                    )
                    migrated += 1
            if migrated:
                conn.commit()
        if migrated:
            logger.info("Migrated %d grandfathered tenant(s) to 'scale' plan.", migrated)
        return migrated

    def register(self, config: TenantConfig, *, override_existing: bool = False) -> TenantConfig:
        # Encrypt onto a COPY for storage — never mutate the plaintext
        # object the caller holds and may keep using after this call.
        stored = config.model_copy(
            update={
                "whatsapp_access_token": encrypt_secret(config.whatsapp_access_token, key=self._secret_encryption_key),
                "razorpay_key_secret": encrypt_secret(config.razorpay_key_secret, key=self._secret_encryption_key),
                "razorpay_webhook_secret": encrypt_secret(config.razorpay_webhook_secret, key=self._secret_encryption_key),
            }
        )
        with self._lock, self._db() as conn:
            existing = conn.execute(
                "SELECT tenant_id FROM tenants WHERE tenant_id = ?", (config.tenant_id,)
            ).fetchone()
            if existing and not override_existing:
                raise ValueError(f"Tenant '{config.tenant_id}' is already registered.")
            conn.execute(
                "INSERT INTO tenants (tenant_id, config_json) VALUES (?, ?) "
                "ON CONFLICT(tenant_id) DO UPDATE SET config_json = excluded.config_json",
                (stored.tenant_id, stored.model_dump_json()),
            )
            conn.commit()
        return config

    def _row_to_config(self, row: sqlite3.Row) -> TenantConfig:
        config = TenantConfig.model_validate_json(row["config_json"])
        return config.model_copy(
            update={
                "whatsapp_access_token": decrypt_secret(config.whatsapp_access_token, key=self._secret_encryption_key),
                "razorpay_key_secret": decrypt_secret(config.razorpay_key_secret, key=self._secret_encryption_key),
                "razorpay_webhook_secret": decrypt_secret(config.razorpay_webhook_secret, key=self._secret_encryption_key),
            }
        )

    def get_config(self, tenant_id: str | None) -> TenantConfig:
        if not tenant_id:
            raise TenantNotFoundError("tenant_id is required.")
        with self._lock, self._db() as conn:
            row = conn.execute("SELECT config_json FROM tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
            if not row:
                raise TenantNotFoundError(f"Unknown tenant_id '{tenant_id}'.")
            return self._row_to_config(row)

    def get_active_tenant(self, tenant_id: str | None) -> TenantConfig:
        """Fail-closed resolution: raises unless the tenant exists AND is ACTIVE."""
        config = self.get_config(tenant_id)
        if config.status != TenantStatus.ACTIVE:
            raise TenantNotActiveError(f"Tenant '{tenant_id}' is {config.status.value}, not active.")
        return config

    def update_status(self, tenant_id: str, new_status: TenantStatus) -> TenantConfig:
        config = self.get_config(tenant_id)
        updated = config.model_copy(update={"status": new_status})
        self.register(updated, override_existing=True)
        return updated

    def update_config(self, tenant_id: str, **fields: Any) -> TenantConfig:
        config = self.get_config(tenant_id)
        updated = config.model_copy(update=fields)
        self.register(updated, override_existing=True)
        return updated

    def list_all(self) -> list[TenantConfig]:
        with self._lock, self._db() as conn:
            rows = conn.execute("SELECT config_json FROM tenants").fetchall()
            return [self._row_to_config(r) for r in rows]

    def find_by_whatsapp_phone_number_id(self, phone_number_id: str) -> TenantConfig | None:
        """Routes an inbound WhatsApp webhook event to its owning tenant.

        Business AI runs ONE shared Meta App/webhook for every tenant, so
        the webhook payload's `phone_number_id` (Meta's own routing key,
        never client-suppliable in a way that could impersonate another
        tenant — it's tied to the sender's verified WhatsApp Business
        number at Meta's end) is the only trustworthy way to know which
        business a message belongs to. A linear scan over all tenants is
        the smallest correct implementation at pilot-phase tenant counts;
        revisit with a dedicated index only if tenant volume ever makes
        this a real bottleneck.
        """
        if not phone_number_id:
            return None
        for config in self.list_all():
            if config.whatsapp_phone_number_id == phone_number_id:
                return config
        return None

    def is_registered(self, tenant_id: str) -> bool:
        with self._lock, self._db() as conn:
            row = conn.execute("SELECT 1 FROM tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
            return row is not None

    def delete(self, tenant_id: str) -> bool:
        """Phase 9 tenant data deletion: removes the tenant's own config
        row. Returns whether a row actually existed to delete."""
        with self._lock, self._db() as conn:
            cur = conn.execute("DELETE FROM tenants WHERE tenant_id = ?", (tenant_id,))
            conn.commit()
            return cur.rowcount > 0


_REGISTRIES: dict[Path, TenantRegistry] = {}


def get_global_tenant_registry(data_root: Path | str = "data") -> TenantRegistry:
    resolved_path = Path(data_root).resolve() / "tenants.db"
    if resolved_path not in _REGISTRIES:
        _REGISTRIES[resolved_path] = TenantRegistry(resolved_path)
    return _REGISTRIES[resolved_path]


# ==============================================================================
# Authorization — the single chokepoint every route must call
# ==============================================================================


def authorize(
    principal: Principal | None,
    action: TenantAction,
    *,
    target_tenant_id: str,
    registry: TenantRegistry,
) -> TenantConfig:
    """Authorize `principal` to perform `action` against `target_tenant_id`.

    Central invariant: a non-platform_admin principal can NEVER act on a
    tenant_id other than their own, no matter what target_tenant_id is
    requested. Returns the tenant's config on success (callers need it
    anyway) or raises UnauthorizedError / TenantNotFoundError.

    The one exception to "no principal, no access" is PUBLIC_ACTIONS: a
    real customer using the chat widget has no Business AI account at
    all, by design, so those specific actions must work with principal
    being None — gated instead by the tenant's lifecycle status below.
    """
    config = registry.get_config(target_tenant_id)

    # Lifecycle gating: customer-facing actions require the tenant to be
    # ACTIVE. Platform admins bypass this only for lifecycle-management
    # actions themselves (activate/suspend/inspect), never for querying a
    # non-active tenant's assistant.
    #
    # One narrow, deliberate exception (Phase 8 onboarding): the tenant's
    # OWN owner/manager may preview/test their assistant while still
    # PROVISIONING, so "test before you activate" is a real capability
    # instead of a silent 400. This is never true for SUSPENDED (that
    # status means something is deliberately wrong, never exempted) and
    # never true for an anonymous caller or a different tenant's
    # principal — a real customer still never sees a non-live assistant.
    if action in CUSTOMER_FACING_ACTIONS and config.status != TenantStatus.ACTIVE:
        is_own_provisioning_preview = (
            config.status == TenantStatus.PROVISIONING
            and principal is not None
            and principal.is_authenticated
            and principal.tenant_id == target_tenant_id
            and principal.role in ("owner", "manager")
        )
        if not is_own_provisioning_preview:
            raise UnauthorizedError(f"Business is {config.status.value}, not active. This action is disabled.")

    if principal is None or not principal.is_authenticated:
        if action in PUBLIC_ACTIONS:
            return config
        raise UnauthorizedError("Authentication required.")

    if principal.role != "platform_admin" and principal.tenant_id != target_tenant_id:
        raise UnauthorizedError("You are not authorized to act on this business's data.")

    if principal.role == "platform_admin":
        return config

    # Explicit, fail-closed lookup: an unrecognized role gets zero
    # actions, never a default fallthrough to STAFF_ACTIONS.
    allowed = ROLE_ACTIONS.get(principal.role, frozenset())
    if action not in allowed:
        raise UnauthorizedError(f"Role '{principal.role}' is not authorized to perform '{action.value}'.")

    # Second, independent dimension: the tenant's PLAN must also include
    # this action, on top of the role already allowing it. Same
    # fail-closed shape as the role check just above — an unrecognized
    # plan value gets zero actions, never a default fallthrough.
    plan_allowed = PLAN_ACTIONS.get(config.plan, frozenset())
    if action not in plan_allowed:
        raise UnauthorizedError(
            f"Your plan ('{config.plan}') does not include '{action.value}'. Upgrade your plan to access this feature."
        )

    return config
