"""Instant, event-triggered emails — as opposed to digest.py's scheduled
daily summary. Fired synchronously from the request path that detected
the trigger (a customer message, an owner marking a lead serviced), not
from a cron. Kept separate from digest.py because these are a different
kind of thing: one-off reactions to a single event, not a rollup.
"""

from __future__ import annotations

from html import escape


def render_dissatisfaction_alert(
    *,
    business_name: str,
    query: str,
    answer_text: str | None,
    session_id: str,
    dashboard_url: str | None,
) -> tuple[str, str]:
    """For the OWNER: a customer's message was flagged as showing real
    frustration/a complaint. Sent immediately, not batched into tomorrow's
    digest — the whole point is catching this before it becomes a bad
    review instead of after."""
    subject = f"A customer sounds unhappy — {business_name}"
    reply_block = (
        f"<p><strong>Your assistant replied:</strong><br>{escape(answer_text)}</p>" if answer_text else ""
    )
    dashboard_link = (
        f'<p><a href="{escape(dashboard_url)}">Open your dashboard &rarr;</a></p>' if dashboard_url else ""
    )
    html = f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 560px;">
      <h2 style="margin-bottom:4px; color:#b91c1c;">A customer sounds unhappy</h2>
      <p style="color:#666; margin-top:0;">Your Business AI assistant flagged this conversation
      for you right away, instead of waiting for tomorrow's summary.</p>
      <p><strong>Customer said:</strong><br>{escape(query)}</p>
      {reply_block}
      <p style="color:#999; font-size:0.875rem;">Session: {escape(session_id)}</p>
      {dashboard_link}
    </div>
    """
    return subject, html


def render_new_tenant_signup_alert(
    *, business_name: str, owner_email: str, tenant_id: str, dashboard_url: str | None,
) -> tuple[str, str]:
    """For the PLATFORM ADMIN: a new business just signed up and is
    sitting in PROVISIONING until manually activated. Without this, a
    signup is only visible if the admin happens to check the dashboard's
    admin panel — a real gap for onboarding a real, unfamiliar customer
    rather than a pilot the founder is already watching closely."""
    subject = f"New Business AI signup: {business_name}"
    dashboard_link = (
        f'<p><a href="{escape(dashboard_url)}">Open the admin panel to activate &rarr;</a></p>' if dashboard_url else ""
    )
    html = f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 560px;">
      <h2 style="margin-bottom:4px;">New business signed up</h2>
      <p><strong>{escape(business_name)}</strong> ({escape(owner_email)}) just created a Business AI
      account and is waiting to be activated once they've added their knowledge base.</p>
      <p style="color:#999; font-size:0.875rem;">Tenant ID: {escape(tenant_id)}</p>
      {dashboard_link}
    </div>
    """
    return subject, html


def render_tenant_activated_email(*, business_name: str, assistant_name: str, chat_url: str | None) -> tuple[str, str]:
    """For the OWNER: their assistant just went live. Without this, the
    owner has no signal that activation happened beyond refreshing their
    own dashboard — a confusing silence right at the moment they're
    handing this off to their team or telling customers about it."""
    subject = f"{business_name} is live on Business AI"
    chat_block = (
        f'<p><a href="{escape(chat_url)}">Try your assistant &rarr;</a></p>' if chat_url else ""
    )
    html = f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 560px;">
      <h2 style="margin-bottom:4px;">You're live!</h2>
      <p>{escape(assistant_name)} is now answering questions for {escape(business_name)}.</p>
      {chat_block}
    </div>
    """
    return subject, html


def render_billing_link_email(*, business_name: str, assistant_name: str, amount_inr: int, payment_url: str) -> tuple[str, str]:
    """For the OWNER: a subscription payment is due before their
    assistant can go live. The payment itself lands directly in Business
    AI's own Razorpay account (platform-level credentials, never the
    tenant's) — see app.py's billing-link route."""
    subject = f"Complete your Business AI subscription — {business_name}"
    html = f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 560px;">
      <h2 style="margin-bottom:4px;">Complete your subscription</h2>
      <p>To activate {escape(assistant_name)} for {escape(business_name)}, please complete your subscription
      payment of ₹{amount_inr}:</p>
      <p>
        <a href="{escape(payment_url)}"
           style="display:inline-block; padding:10px 16px; background:#1F5C4E; color:#fff;
                  text-decoration:none; border-radius:6px;">Pay &#8377;{amount_inr}</a>
      </p>
      <p style="color:#666;">Once payment is confirmed, activate your assistant from your dashboard.</p>
    </div>
    """
    return subject, html


def render_tenant_self_activated_notice(*, business_name: str, tenant_id: str, dashboard_url: str | None) -> tuple[str, str]:
    """For the PLATFORM ADMIN: visibility, not a gate — a business just
    activated itself without you clicking anything. Nothing to do here;
    this exists purely so you're not surprised by a new business going
    live that you never touched."""
    subject = f"{business_name} activated itself"
    dashboard_link = (
        f'<p><a href="{escape(dashboard_url)}">Open the admin panel &rarr;</a></p>' if dashboard_url else ""
    )
    html = f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 560px;">
      <h2 style="margin-bottom:4px;">A business went live on its own</h2>
      <p><strong>{escape(business_name)}</strong> just activated their own Business AI assistant — no action
      needed from you.</p>
      <p style="color:#999; font-size:0.875rem;">Tenant ID: {escape(tenant_id)}</p>
      {dashboard_link}
    </div>
    """
    return subject, html


def render_review_request(*, business_name: str, assistant_name: str, review_link: str) -> tuple[str, str]:
    """For the CUSTOMER: sent only when the owner explicitly marks a lead
    as serviced (see /api/leads/{id}/request-review) — deliberately NOT
    auto-triggered off a chat signal like buying_intent, since the
    assistant has no way to know whether the service was actually
    delivered yet. Asking for a review before that would be presumptuous
    and could itself damage trust."""
    subject = f"How was your experience with {business_name}?"
    html = f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 560px;">
      <p>Hi! This is {escape(assistant_name)}, {escape(business_name)}'s assistant.</p>
      <p>We'd really appreciate it if you could share a quick review of your experience:</p>
      <p>
        <a href="{escape(review_link)}"
           style="display:inline-block; padding:10px 16px; background:#2f6f4f; color:#fff;
                  text-decoration:none; border-radius:6px;">Leave a review</a>
      </p>
      <p style="color:#666;">Thank you for your time!</p>
    </div>
    """
    return subject, html
