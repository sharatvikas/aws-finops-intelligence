"""Claude API integration — explain anomalies and generate remediation suggestions."""

from __future__ import annotations

import os

import anthropic

from finops.analyzers.waste import WasteItem
from finops.collectors.cost_explorer import ServiceSpend, DailySpend


_SYSTEM_PROMPT = """You are a senior FinOps engineer and AWS Solutions Architect.
You help engineering teams understand their AWS cloud spend and reduce waste.

When explaining anomalies:
- Be specific: reference exact services, amounts, and percentages
- Correlate with likely engineering causes (auto-scaling events, load tests, forgotten resources)
- Distinguish "expected growth" from "unexpected spike"
- Give actionable next steps ranked by impact

When generating remediation:
- Give exact AWS CLI commands or Terraform snippets
- Estimate savings per month
- Flag any risks (data loss, downtime) with the remediation

Be concise. Bullet points preferred over paragraphs."""


def explain_anomaly(
    service: str,
    current_spend: float,
    baseline_spend: float,
    change_pct: float,
    daily_trend: list[DailySpend],
    max_tokens: int = 1024,
) -> str:
    """Explain why a service cost anomaly likely occurred."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    trend_lines = "\n".join(
        f"  {d.date}: ${d.amount:.2f}" for d in daily_trend[-14:]
    )

    message = f"""Explain this AWS cost anomaly:

Service: {service}
Current 30-day spend: ${current_spend:.2f}
30-day baseline: ${baseline_spend:.2f}
Change: +{change_pct:.1f}%

Daily trend (last 14 days):
{trend_lines}

What likely caused this increase and what should the team investigate?"""

    resp = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=max_tokens,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": message}],
    )
    return resp.content[0].text


def generate_remediation_terraform(waste_item: WasteItem) -> str:
    """Generate a Terraform snippet to remediate a waste item."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    message = f"""Generate a Terraform snippet to remediate this AWS waste:

Category: {waste_item.category}
Resource: {waste_item.resource_id} ({waste_item.resource_type})
Reason: {waste_item.reason}
Recommended Action: {waste_item.remediation}
Details: {waste_item.details}

Provide:
1. Terraform code to fix or decommission this resource
2. Any data.aws_* lookups needed
3. A note on whether this requires downtime or data migration"""

    resp = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": message}],
    )
    return resp.content[0].text


def generate_executive_summary(
    total_spend: float,
    forecast: float,
    waste_items: list[WasteItem],
    anomalies: list[ServiceSpend],
    max_tokens: int = 800,
) -> str:
    """Generate a plain-English executive summary of the FinOps report."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    total_waste = sum(w.estimated_monthly_cost for w in waste_items)
    top_waste = waste_items[:3] if waste_items else []
    top_anomalies = [a for a in anomalies if a.change_pct > 30][:3]

    message = f"""Write a brief executive summary of this AWS FinOps report.

Current month spend: ${total_spend:,.0f}
Month-end forecast: ${forecast:,.0f}
Total identified waste: ${total_waste:,.0f}/month

Top waste items:
{chr(10).join(f'  - {w.resource_type}: ${w.estimated_monthly_cost:.0f}/mo — {w.reason}' for w in top_waste)}

Services with notable cost increases:
{chr(10).join(f'  - {a.service}: +{a.change_pct:.0f}% (${a.amount_last_30d:.0f} vs ${a.amount_prev_30d:.0f})' for a in top_anomalies)}

Write 3-4 sentences suitable for a Slack message to engineering leadership.
Focus on the most actionable insight and total potential savings."""

    resp = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=max_tokens,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": message}],
    )
    return resp.content[0].text
