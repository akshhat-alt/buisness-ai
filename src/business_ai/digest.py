"""Owner daily digest: render a summary email from data that's already
being collected (leads, conversation analytics). No new data model — this
module is pure presentation over AnalyticsStore/LeadStore output.
"""

from __future__ import annotations

from html import escape

from business_ai.analytics import AnalyticsSummary
from business_ai.leads import Lead
from business_ai.tenant import TenantConfig


def has_digest_content(new_leads: list[Lead], analytics: AnalyticsSummary) -> bool:
    """Whether there's anything worth emailing about. A business with zero
    activity in the window shouldn't get an empty digest every single day —
    that trains the owner to ignore it."""
    return bool(new_leads) or analytics.total_questions > 0


def render_owner_digest(
    tenant: TenantConfig,
    *,
    new_leads: list[Lead],
    analytics: AnalyticsSummary,
    window_hours: int,
    dashboard_url: str | None,
    action_items: list[str] | None = None,
) -> tuple[str, str]:
    """Returns (subject, html_body). action_items is the optional LLM-
    generated advisory brief (see generation.generate_action_brief) — a
    report of raw numbers turned into 1-3 concrete recommendations. None
    or an empty list simply omits that section, so a digest still renders
    correctly without it (e.g. when email is configured but the caller
    chooses not to spend the extra LLM call, or the brief came back
    empty because there was nothing meaningful to say)."""
    period = "today" if window_hours <= 24 else f"the last {window_hours} hours"
    subject = f"{tenant.business_name}: {len(new_leads)} new lead(s), {analytics.total_questions} question(s) {period}"

    lead_rows = "".join(
        f"<tr><td>{escape(lead.name or '—')}</td><td>{escape(lead.phone or lead.email or '—')}</td>"
        f"<td>{escape((lead.message or '')[:80])}</td></tr>"
        for lead in new_leads
    ) or "<tr><td colspan='3' style='color:#666;'>No new leads in this period.</td></tr>"

    gap_items = "".join(f"<li>{escape(gap)}</li>" for gap in analytics.recent_knowledge_gaps) or (
        "<li style='color:#666;'>None — nice.</li>"
    )

    dashboard_link = (
        f'<p><a href="{escape(dashboard_url)}">Open your dashboard &rarr;</a></p>' if dashboard_url else ""
    )

    action_section = ""
    if action_items:
        items_html = "".join(f"<li>{escape(item)}</li>" for item in action_items)
        action_section = f"""
      <div style="background:#f0f7f2; border-left:3px solid #2f6f4f; padding:12px 16px; margin:16px 0;">
        <h3 style="margin-top:0;">What to do about it</h3>
        <ul style="margin-bottom:0;">{items_html}</ul>
      </div>
        """

    html = f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 560px;">
      <h2 style="margin-bottom: 4px;">{escape(tenant.business_name)} — {period}'s summary</h2>
      <p style="color:#666; margin-top:0;">From your Business AI assistant.</p>

      <table style="width:100%; border-collapse:collapse; margin: 16px 0;">
        <tr>
          <td style="padding:8px; background:#f5f5f5; text-align:center;"><strong>{analytics.total_questions}</strong><br>Questions asked</td>
          <td style="padding:8px; background:#f5f5f5; text-align:center;"><strong>{analytics.answered_count}</strong><br>Answered</td>
          <td style="padding:8px; background:#f5f5f5; text-align:center;"><strong>{len(new_leads)}</strong><br>New leads</td>
          <td style="padding:8px; background:#f5f5f5; text-align:center;"><strong>{analytics.buying_intent_count}</strong><br>Buying interest</td>
        </tr>
      </table>

      {action_section}

      <h3>New leads</h3>
      <table style="width:100%; border-collapse:collapse;">
        <thead><tr><th align="left">Name</th><th align="left">Contact</th><th align="left">Message</th></tr></thead>
        <tbody>{lead_rows}</tbody>
      </table>

      <h3>Questions your assistant couldn't answer</h3>
      <p style="color:#666; margin-top:-8px;">Add these to your knowledge base to close the gap.</p>
      <ul>{gap_items}</ul>

      {dashboard_link}
    </div>
    """
    return subject, html
