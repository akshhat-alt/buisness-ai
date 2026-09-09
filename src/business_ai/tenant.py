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

import sqlite3
import threading
import time
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Any, Generator

from pydantic import BaseModel, Field

from business_ai.auth import Principal


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
    review_link: str | None = None  # e.g. a Google Business review URL, for review-request emails
    # Deposit/payment links (Razorpay) — same "bring your own credential"
    # shape as WhatsApp: the payment goes straight into the TENANT's own
    # Razorpay account, never through a Business AI-held balance.
    razorpay_key_id: str | None = None
    razorpay_key_secret: str | None = None
    deposit_amount_inr: int | None = None  # whole rupees; unset = deposit-link action is disabled
    # Customer win-back: how many days without a repeat visit counts as
    # "lapsed" for THIS business. None = platform default (config.
    # winback_default_days) — a salon's natural cadence is weeks, a
    # dental clinic's is months, so this can't be one hardcoded number.
    winback_after_days: int | None = None
    status: TenantStatus = TenantStatus.PROVISIONING
    question_quota: int | None = None  # None = platform default (see config.active_tenant_quota)
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


OWNER_ACTIONS = frozenset(
    {
        TenantAction.QUERY_ASSISTANT,
        TenantAction.VIEW_PUBLIC_INFO,
        TenantAction.INGEST_KNOWLEDGE,
        TenantAction.VIEW_LEADS,
        TenantAction.VIEW_ANALYTICS,
        TenantAction.MANAGE_ASSISTANT,
    }
)

STAFF_ACTIONS = frozenset(
    {
        TenantAction.QUERY_ASSISTANT,
        TenantAction.VIEW_PUBLIC_INFO,
        TenantAction.VIEW_LEADS,
        TenantAction.VIEW_ANALYTICS,
    }
)

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


class TenantRegistry:
    """Thread-safe SQLite store for tenant (business) configuration."""

    def __init__(self, db_path: Path | str = "data/tenants.db") -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    @contextmanager
    def _db(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._db() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=10000;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tenants (
                    tenant_id TEXT PRIMARY KEY,
                    config_json TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def register(self, config: TenantConfig, *, override_existing: bool = False) -> TenantConfig:
        with self._lock, self._db() as conn:
            existing = conn.execute(
                "SELECT tenant_id FROM tenants WHERE tenant_id = ?", (config.tenant_id,)
            ).fetchone()
            if existing and not override_existing:
                raise ValueError(f"Tenant '{config.tenant_id}' is already registered.")
            conn.execute(
                "INSERT INTO tenants (tenant_id, config_json) VALUES (?, ?) "
                "ON CONFLICT(tenant_id) DO UPDATE SET config_json = excluded.config_json",
                (config.tenant_id, config.model_dump_json()),
            )
            conn.commit()
        return config

    def _row_to_config(self, row: sqlite3.Row) -> TenantConfig:
        return TenantConfig.model_validate_json(row["config_json"])

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
    if action in CUSTOMER_FACING_ACTIONS and config.status != TenantStatus.ACTIVE:
        raise UnauthorizedError(f"Business is {config.status.value}, not active. This action is disabled.")

    if principal is None or not principal.is_authenticated:
        if action in PUBLIC_ACTIONS:
            return config
        raise UnauthorizedError("Authentication required.")

    if principal.role != "platform_admin" and principal.tenant_id != target_tenant_id:
        raise UnauthorizedError("You are not authorized to act on this business's data.")

    if principal.role == "platform_admin":
        return config

    allowed = OWNER_ACTIONS if principal.role == "owner" else STAFF_ACTIONS
    if action not in allowed:
        raise UnauthorizedError(f"Role '{principal.role}' is not authorized to perform '{action.value}'.")

    return config
