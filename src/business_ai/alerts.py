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
