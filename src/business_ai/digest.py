"""Owner daily digest: render a summary email from data that's already
being collected (leads, conversation analytics). No new data model — this
module is pure presentation over AnalyticsStore/LeadStore output.
"""

from __future__ import annotations

from html import escape

from business_ai.analytics import AnalyticsSummary
from business_ai.leads import Lead
from business_ai.tenant import TenantConfig


def has_digest_content(
    new_leads: list[Lead],
    analytics: AnalyticsSummary,
    *,
    overdue_summary_lines: list[str] | None = None,
    recurring_feedback_lines: list[str] | None = None,
) -> bool:
    """Whether there's anything worth emailing about. A business with zero
    activity in the window shouldn't get an empty digest every single day —
    that trains the owner to ignore it. Overdue tasks and recurring
    feedback themes count as content too — a quiet lead day with a real
    task backlog or a repeating employee complaint still deserves an email."""
    return (
        bool(new_leads)
        or analytics.total_questions > 0
        or bool(overdue_summary_lines)
        or bool(recurring_feedback_lines)
    )


def render_owner_digest(
    tenant: TenantConfig,
    *,
    new_leads: list[Lead],
    analytics: AnalyticsSummary,
    window_hours: int,
    dashboard_url: str | None,
    action_items: list[str] | None = None,
    overdue_summary_lines: list[str] | None = None,
    recurring_feedback_lines: list[str] | None = None,
) -> tuple[str, str]:
    """Returns (subject, html_body). action_items is the optional LLM-
    generated advisory brief (see generation.generate_action_brief) — a
    report of raw numbers turned into 1-3 concrete recommendations. None
    or an empty list simply omits that section, so a digest still renders
    correctly without it (e.g. when email is configured but the caller
    chooses not to spend the extra LLM call, or the brief came back
    empty because there was nothing meaningful to say).

    overdue_summary_lines / recurring_feedback_lines are pre-rendered,
    already-real-data strings assembled by the caller (app.py, which has
    the TaskStore/EmployeeStore/FeedbackStore access this module
    deliberately doesn't) — this module stays pure presentation, exactly
    like the rest of its content."""
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

    health_section = ""
    if overdue_summary_lines or recurring_feedback_lines:
        overdue_html = (
            "".join(f"<li>{escape(line)}</li>" for line in overdue_summary_lines)
            if overdue_summary_lines else "<li style='color:#666;'>None — nice.</li>"
        )
        feedback_html = (
            "".join(f"<li>{escape(line)}</li>" for line in recurring_feedback_lines)
            if recurring_feedback_lines else "<li style='color:#666;'>None reported.</li>"
        )
        health_section = f"""
      <h3>Team &amp; operations</h3>
      <p style="color:#666; margin-top:-8px;">Overdue tasks</p>
      <ul>{overdue_html}</ul>
      <p style="color:#666; margin-bottom:-8px;">Recurring employee feedback</p>
      <ul>{feedback_html}</ul>
        """

    html = f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 560px;">
      <h2 style="margin-bottom: 4px;">{escape(tenant.business_name)} — {period}'s summary</h2>
      <p style="color:#666; margin-top:0;">From your Bizistic assistant.</p>

      <table style="width:100%; border-collapse:collapse; margin: 16px 0;">
        <tr>
          <td style="padding:8px; background:#f5f5f5; text-align:center;"><strong>{analytics.total_questions}</strong><br>Questions asked</td>
          <td style="padding:8px; background:#f5f5f5; text-align:center;"><strong>{analytics.answered_count}</strong><br>Answered</td>
          <td style="padding:8px; background:#f5f5f5; text-align:center;"><strong>{len(new_leads)}</strong><br>New leads</td>
          <td style="padding:8px; background:#f5f5f5; text-align:center;"><strong>{analytics.buying_intent_count}</strong><br>Buying interest</td>
        </tr>
      </table>

      {action_section}
      {health_section}

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


def render_owner_whatsapp_summary(
    tenant: TenantConfig,
    *,
    new_leads: list[Lead],
    analytics: AnalyticsSummary,
    open_gaps_count: int,
    window_label: str,
    action_items: list[str] | None = None,
    overdue_summary_lines: list[str] | None = None,
    recurring_feedback_lines: list[str] | None = None,
) -> str:
    """A short, plain-text version of the same digest content, for the
    owner's own WhatsApp — both the pushed daily summary and the pull
    ("what needs my attention today?") command render through this one
    function, since they show the same kind of thing on demand vs. on a
    schedule. WhatsApp brevity rules apply: numbers first, no HTML, no
    more than a couple of action lines.
    """
    lines = [
        f"📊 {tenant.business_name} — {window_label}",
        "",
        f"{analytics.total_questions} questions asked ({analytics.answered_count} answered)",
        f"{len(new_leads)} new lead(s)",
        f"{analytics.buying_intent_count} showed buying interest",
    ]
    if analytics.dissatisfaction_count:
        lines.append(f"⚠️ {analytics.dissatisfaction_count} complaint(s) flagged")
    if open_gaps_count:
        lines.append(f"{open_gaps_count} knowledge gap(s) open")
    if overdue_summary_lines:
        lines.append("")
        lines.append("⚠️ Overdue:")
        lines.extend(f"• {line}" for line in overdue_summary_lines)
    if recurring_feedback_lines:
        lines.append("")
        lines.append("📋 Recurring feedback:")
        lines.extend(f"• {line}" for line in recurring_feedback_lines)
    if action_items:
        lines.append("")
        lines.append("What to do:")
        lines.extend(f"• {item}" for item in action_items[:3])
    if not (
        new_leads or analytics.total_questions or open_gaps_count
        or overdue_summary_lines or recurring_feedback_lines
    ):
        lines.append("")
        lines.append("All quiet — nothing needs you right now.")
    return "\n".join(lines)
