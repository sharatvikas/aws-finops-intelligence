"""Slack reporter — post FinOps digest to Slack with rich formatting."""

from __future__ import annotations

import json
import os
from datetime import date

import httpx

from finops.analyzers.anomaly import CostAnomaly
from finops.analyzers.waste import WasteItem
from finops.collectors.cost_explorer import ServiceSpend


def post_daily_digest(
    total_mtd: float,
    forecast: float,
    anomalies: list[CostAnomaly],
    waste_items: list[WasteItem],
    top_services: list[ServiceSpend],
    webhook_url: str | None = None,
    channel: str | None = None,
) -> bool:
    """Post a rich Slack message with the FinOps daily digest."""
    webhook = webhook_url or os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        print("SLACK_WEBHOOK_URL not set, skipping Slack notification")
        return False

    total_waste = sum(w.estimated_monthly_cost for w in waste_items)
    critical_anomalies = [a for a in anomalies if a.severity == "CRITICAL"]

    # Header color based on findings
    color = "#2eb886"  # green
    if critical_anomalies:
        color = "#e01e5a"  # red
    elif anomalies or total_waste > 500:
        color = "#ecb22e"  # yellow

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"☁️ AWS FinOps Digest — {date.today().strftime('%B %d, %Y')}",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*MTD Spend*\n${total_mtd:,.0f}"},
                {"type": "mrkdwn", "text": f"*Month Forecast*\n${forecast:,.0f}"},
                {"type": "mrkdwn", "text": f"*Identified Waste*\n${total_waste:,.0f}/mo"},
                {"type": "mrkdwn", "text": f"*Anomalies*\n{len(anomalies)} ({len(critical_anomalies)} critical)"},
            ],
        },
        {"type": "divider"},
    ]

    # Anomalies section
    if anomalies:
        anomaly_lines = []
        for a in anomalies[:5]:
            emoji = "🔴" if a.severity == "CRITICAL" else "🟠" if a.severity == "HIGH" else "🟡"
            anomaly_lines.append(
                f"{emoji} *{a.service}*: ${a.amount:.0f} on {a.date} "
                f"(+{a.change_pct:.0f}%, z={a.z_score:.1f})"
            )
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Cost Anomalies*\n" + "\n".join(anomaly_lines),
            },
        })

    # Top waste
    if waste_items:
        waste_lines = []
        for w in waste_items[:5]:
            waste_lines.append(
                f"• *{w.category}* — {w.resource_id}: ${w.estimated_monthly_cost:.0f}/mo\n"
                f"  _{w.reason}_"
            )
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Top Waste Items* (${total_waste:,.0f}/mo total)\n" + "\n".join(waste_lines),
            },
        })

    # Top services by spend
    if top_services:
        svc_lines = []
        for s in top_services[:5]:
            arrow = "↑" if s.change_pct > 5 else "↓" if s.change_pct < -5 else "→"
            color_code = "🔴" if s.change_pct > 30 else "🟢" if s.change_pct < -10 else "⚪"
            svc_lines.append(
                f"{color_code} {s.service}: ${s.amount_last_30d:,.0f} {arrow} {s.change_pct:+.0f}%"
            )
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*Top Services by Spend (30d)*\n" + "\n".join(svc_lines),
            },
        })

    blocks.append({
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": "aws-finops-intelligence • Run `finops analyze` for full report with Terraform remediation",
            }
        ],
    })

    payload: dict = {"blocks": blocks, "attachments": [{"color": color, "blocks": []}]}
    if channel:
        payload["channel"] = channel

    resp = httpx.post(webhook, json=payload, timeout=10.0)
    return resp.is_success
