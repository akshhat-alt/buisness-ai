"""HTTP request/response Pydantic models (Phase 9 extraction from
app.py) — grouped in one module since none of them depend on
`Services`/route context, only on pydantic and a couple of enums.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from business_ai.automation import ActionType, TriggerType


class SignupRequest(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=6, max_length=200)
    name: str = Field(min_length=1, max_length=100)
    business_name: str = Field(min_length=1, max_length=100)


class LoginRequest(BaseModel):
    email: str
    password: str


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=6, max_length=200)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=6, max_length=200)



class AskRequest(BaseModel):
    query: str = Field(min_length=1)
    session_id: str | None = None


class LeadRequest(BaseModel):
    session_id: str
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    message: str | None = None


class WebsiteIngestRequest(BaseModel):
    url: str
    label: str | None = None


class TextIngestRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    text: str = Field(min_length=1, max_length=1_000_000)


class WhatsAppHelpRequest(BaseModel):
    phone_number: str = Field(min_length=1, max_length=50)
    note: str | None = Field(default=None, max_length=1000)


class GapPublishRequest(BaseModel):
    answer_text: str = Field(min_length=1, max_length=2000)


class AppointmentRequest(BaseModel):
    appointment_at: str = Field(min_length=1)  # ISO 8601 datetime, e.g. "2026-09-20T16:00:00"
    party_size: int | None = Field(default=None, gt=0)  # Phase 23 — a restaurant reservation's guest count


class DepositPaidRequest(BaseModel):
    amount_inr: int | None = None  # defaults to the tenant's configured deposit_amount_inr if unset


class AppointmentOutcomeRequest(BaseModel):
    outcome: str  # "completed" | "no_show" | "cancelled"


class EmbeddedSignupRequest(BaseModel):
    # Both handed to the frontend directly by Meta's Embedded Signup
    # popup callback — see MetaEmbeddedSignupClient's docstring.
    code: str = Field(min_length=1)
    phone_number_id: str = Field(min_length=1)


class BillingLinkRequest(BaseModel):
    amount_inr: int = Field(gt=0)


class MarkPaidRequest(BaseModel):
    amount_inr: int | None = None


class SetPlanRequest(BaseModel):
    # Literal, not a plain str: rejects an invalid plan at the API layer
    # with a clean 422, before it ever reaches authorize()'s own
    # fail-closed PLAN_ACTIONS.get(..., frozenset()) — that fallback
    # exists for defense in depth (e.g. a hand-edited row), not as the
    # primary validation for this endpoint.
    plan: Literal["starter", "growth", "scale"]


class CreateEmployeeRequest(BaseModel):
    whatsapp_number: str
    name: str
    role: str = "staff"  # "owner" | "manager" | "staff"


class CreateTaskRequest(BaseModel):
    title: str
    assigned_to_employee_id: str
    description: str | None = None
    due_at: str | None = None  # parsed the same way as appointment_at (local business time -> UTC)
    approval_required: bool = False
    customer_facing_lead_id: str | None = None  # enables the verified-outcome customer ping on completion


class RejectTaskRequest(BaseModel):
    reason: str | None = None


class CreateShiftRequest(BaseModel):
    employee_id: str
    shift_date: str = Field(min_length=1)  # "YYYY-MM-DD"
    start_time: str = Field(min_length=1)  # "HH:MM"
    end_time: str = Field(min_length=1)  # "HH:MM"
    role_label: str = ""


class LogMetricRequest(BaseModel):
    metric_type: str  # sale | expense | collection — validated against METRIC_TYPES by the store itself
    amount_inr: int
    note: str = ""


class ApproveSopRequest(BaseModel):
    theme: str
    text: str


class UpdateEmployeeRequest(BaseModel):
    role: str | None = None
    active: bool | None = None


class CreateAutomationRuleRequest(BaseModel):
    name: str
    trigger_type: TriggerType
    trigger_params: dict = {}
    action_type: ActionType
    action_params: dict = {}
    escalate_after_hours: float | None = None


class UpdateAutomationRuleRequest(BaseModel):
    name: str | None = None
    trigger_params: dict | None = None
    action_params: dict | None = None
    enabled: bool | None = None
    escalate_after_hours: float | None = None


class AutomationKillSwitchRequest(BaseModel):
    enabled: bool


class EvolutionKillSwitchRequest(BaseModel):
    enabled: bool


class TenantConfigUpdate(BaseModel):
    assistant_name: str | None = None
    welcome_message: str | None = None
    whatsapp_number: str | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_access_token: str | None = None
    owner_whatsapp_number: str | None = None
    admin_notify_template_name: str | None = None
    review_link: str | None = None
    razorpay_key_id: str | None = None
    razorpay_key_secret: str | None = None
    razorpay_webhook_secret: str | None = None
    deposit_amount_inr: int | None = None
    winback_after_days: int | None = None
    # Phase 20 — "more configurable levers" for Self-Evolution. Bounded
    # here (the one place this codebase validates numeric tenant
    # settings) so a malformed value can never reach evolution.py's rate
    # comparisons; leaving a field unset keeps whatever is already saved
    # (or the platform default from constants.py if nothing ever was).
    evolution_dissatisfaction_threshold: float | None = Field(default=None, gt=0, le=1)
    evolution_lookback_hours: int | None = Field(default=None, ge=1, le=24 * 90)
    evolution_regression_delta: float | None = Field(default=None, gt=0, le=1)
    google_place_id: str | None = None
    voice_notes_enabled: bool | None = None
    notify_new_leads: bool | None = None


class TenantDeleteRequest(BaseModel):
    # A real, hard-to-fumble confirmation for an irreversible action —
    # the same "type the name to confirm" pattern used by every serious
    # platform for destructive operations. Must match the tenant's
    # CURRENT business_name exactly (case-sensitive).
    confirm_business_name: str


# ---------------------------------------------------------- Restaurant Foundation (Phase 17)


class CreateMenuItemRequest(BaseModel):
    name: str
    price_inr: int
    category: str | None = None


class UpdateMenuItemRequest(BaseModel):
    name: str | None = None
    price_inr: int | None = None
    category: str | None = None
    active: bool | None = None


class RecipeLineInput(BaseModel):
    ingredient_name: str
    quantity: float
    unit: str


class SetRecipeRequest(BaseModel):
    lines: list[RecipeLineInput]


class CreateSupplierRequest(BaseModel):
    name: str
    phone: str | None = None
    notes: str | None = None


class SetInventoryParLevelRequest(BaseModel):
    ingredient_name: str
    par_level: float
    unit: str | None = None


# ---------------------------------------------------------- Restaurant Autopilot (Phase 24)


class SimulateMenuPriceRequest(BaseModel):
    menu_item_id: str
    hypothetical_price_inr: int = Field(gt=0)


# ---------------------------------------------------------- Perception & Input Expansion (Phase 25)


class ManualReviewLogRequest(BaseModel):
    platform: str
    rating: float = Field(gt=0, le=5)
    review_count: int | None = Field(default=None, ge=0)


class PlanUpgradeRequest(BaseModel):
    target_plan: str = Field(min_length=1, max_length=50)
    note: str | None = Field(default=None, max_length=1000)

