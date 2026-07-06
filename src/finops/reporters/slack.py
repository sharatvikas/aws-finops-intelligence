"""Slack reporter — Block Kit FinOps digest via webhook or bot token.

Two transports:
  * Incoming webhook (``SLACK_WEBHOOK_URL``) — simplest, channel fixed by the webhook.
  * Bot token (``SLACK_BOT_TOKEN`` + channel) — needed for threads, multiple
    channels, or workspaces that disallow webhooks.

``dry_run=True`` prints the exact JSON payload instead of sending — used by
the CLI's ``--dry-run`` flag and in CI.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date

from slack_sdk import WebClient, WebhookClient
from slack_sdk.errors import SlackApiError, SlackClientError

from finops.analyzers.anomaly import CostAnomaly
from finops.analyzers.rightsizing import RightsizingRecommendation
from finops.analyzers.waste import WasteItem
from finops.collectors.cost_explorer import ServiceSpend

logger = logging.getLogger(__name__)

_SEVERITY_EMOJI = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "⚪"}


@dataclass
class DigestData:
    """Everything the daily digest can render. All sections are optional."""

    total_mtd: float = 0.0
    forecast: float = 0.0
    anomalies: list[CostAnomaly] | None = None
    waste_items: list[WasteItem] | None = None
    rightsizing: list[RightsizingRecommendation] | None = None
    top_services: list[ServiceSpend] | None = None
    account_label: str | None = None


def build_digest_blocks(data: DigestData, max_per_section: int = 5) -> list[dict]:
    """Build the Block Kit blocks for a FinOps digest. Pure — fully unit-testable."""
    anomalies = data.anomalies or []
    waste_items = data.waste_items or []
    rightsizing = data.rightsizing or []
    top_services = data.top_services or []

    total_waste = sum(w.estimated_monthly_cost for w in waste_items)
    total_rightsizing = sum(r.monthly_savings for r in rightsizing)
    critical = [a for a in anomalies if a.severity == "CRITICAL"]

    title = f"☁️ AWS FinOps Digest — {date.today().strftime('%B %d, %Y')}"
    if data.account_label:
        title += f" ({data.account_label})"

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": title}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*MTD Spend*\n${data.total_mtd:,.0f}"},
                {"type": "mrkdwn", "text": f"*Month Forecast*\n${data.forecast:,.0f}"},
                {
                    "type": "mrkdwn",
                    "text": f"*Savings Identified*\n${total_waste + total_rightsizing:,.0f}/mo",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Anomalies*\n{len(anomalies)} ({len(critical)} critical)",
                },
            ],
        },
        {"type": "divider"},
    ]

    if anomalies:
        lines = []
        for a in anomalies[:max_per_section]:
            emoji = _SEVERITY_EMOJI.get(a.severity, "⚪")
            lines.append(
                f"{emoji} *{a.service}*: ${a.amount:,.0f} on {a.date} "
                f"(+{a.change_pct:.0f}% vs baseline, z={a.z_score:.1f})"
            )
            for hint in a.root_causes[:2]:
                lines.append(f"    ↳ _{hint.summary()}_")
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Cost Anomalies*\n" + "\n".join(lines)},
        })

    if waste_items:
        lines = [
            f"• *{w.category}* — `{w.resource_id}`: ${w.estimated_monthly_cost:,.0f}/mo\n"
            f"  _{w.reason}_"
            for w in sorted(
                waste_items, key=lambda w: w.estimated_monthly_cost, reverse=True
            )[:max_per_section]
        ]
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Waste Findings* (${total_waste:,.0f}/mo total)\n" + "\n".join(lines),
            },
        })

    if rightsizing:
        lines = [
            f"• `{r.name}`: {r.current_type} → *{r.recommended_type}* — "
            f"save ${r.monthly_savings:,.0f}/mo (P90 CPU {r.p90_cpu_pct:.0f}%, {r.confidence})"
            for r in rightsizing[:max_per_section]
        ]
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Rightsizing* (${total_rightsizing:,.0f}/mo potential)\n"
                    + "\n".join(lines)
                ),
            },
        })

    if top_services:
        lines = []
        for s in top_services[:max_per_section]:
            arrow = "↑" if s.change_pct > 5 else "↓" if s.change_pct < -5 else "→"
            dot = "🔴" if s.change_pct > 30 else "🟢" if s.change_pct < -10 else "⚪"
            lines.append(
                f"{dot} {s.service}: ${s.amount_last_30d:,.0f} {arrow} {s.change_pct:+.0f}%"
            )
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Top Services by Spend (30d)*\n" + "\n".join(lines)},
        })

    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": "aws-finops-intelligence • `finops scan --slack` for the full report",
        }],
    })
    return blocks


def digest_fallback_text(data: DigestData) -> str:
    """Plain-text summary for notifications / clients that don't render blocks."""
    anomalies = data.anomalies or []
    savings = sum(w.estimated_monthly_cost for w in (data.waste_items or [])) + sum(
        r.monthly_savings for r in (data.rightsizing or [])
    )
    return (
        f"AWS FinOps Digest: ${data.total_mtd:,.0f} MTD, "
        f"{len(anomalies)} anomalies, ${savings:,.0f}/mo savings identified"
    )


class SlackReporter:
    """Send Block Kit messages to Slack via webhook or bot token.

    Resolution order: explicit args, then ``SLACK_WEBHOOK_URL`` /
    ``SLACK_BOT_TOKEN`` + ``SLACK_CHANNEL`` env vars. Bot token requires a
    channel. With ``dry_run=True`` nothing is sent; the payload is printed.
    """

    def __init__(
        self,
        webhook_url: str | None = None,
        bot_token: str | None = None,
        channel: str | None = None,
        dry_run: bool = False,
        timeout: int = 10,
    ) -> None:
        self._webhook_url = webhook_url or os.environ.get("SLACK_WEBHOOK_URL")
        self._bot_token = bot_token or os.environ.get("SLACK_BOT_TOKEN")
        self._channel = channel or os.environ.get("SLACK_CHANNEL")
        self._dry_run = dry_run
        self._timeout = timeout

        if not dry_run and not self._webhook_url and not self._bot_token:
            raise ValueError(
                "No Slack credentials: pass webhook_url/bot_token or set "
                "SLACK_WEBHOOK_URL or SLACK_BOT_TOKEN (+ SLACK_CHANNEL)."
            )
        if not dry_run and self._bot_token and not self._webhook_url and not self._channel:
            raise ValueError("Bot-token delivery requires a channel (arg or SLACK_CHANNEL).")

    def send_digest(self, data: DigestData) -> bool:
        """Build and deliver the digest. Returns True on success."""
        blocks = build_digest_blocks(data)
        return self.send_blocks(blocks, fallback_text=digest_fallback_text(data))

    def send_blocks(self, blocks: list[dict], fallback_text: str = "AWS FinOps digest") -> bool:
        payload: dict = {"text": fallback_text, "blocks": blocks}
        if self._channel:
            payload["channel"] = self._channel

        if self._dry_run:
            print(json.dumps(payload, indent=2, default=str))
            logger.info("Dry run: printed Slack payload (%d blocks), nothing sent", len(blocks))
            return True

        if self._webhook_url:
            return self._send_webhook(payload)
        return self._send_bot(payload)

    # -- transports ----------------------------------------------------------

    def _send_webhook(self, payload: dict) -> bool:
        try:
            client = WebhookClient(self._webhook_url, timeout=self._timeout)
            resp = client.send(text=payload["text"], blocks=payload["blocks"])
        except SlackClientError as exc:
            logger.error("Slack webhook delivery failed: %s", exc)
            return False
        if resp.status_code != 200:
            logger.error(
                "Slack webhook returned %s: %s", resp.status_code, resp.body
            )
            return False
        logger.info("Posted digest via Slack webhook")
        return True

    def _send_bot(self, payload: dict) -> bool:
        try:
            client = WebClient(token=self._bot_token, timeout=self._timeout)
            client.chat_postMessage(
                channel=payload["channel"],
                text=payload["text"],
                blocks=payload["blocks"],
            )
        except SlackApiError as exc:
            logger.error(
                "Slack API error posting to %s: %s",
                payload.get("channel"), exc.response.get("error", exc),
            )
            return False
        except SlackClientError as exc:
            logger.error("Slack client error: %s", exc)
            return False
        logger.info("Posted digest via bot token to %s", payload["channel"])
        return True


def post_daily_digest(
    total_mtd: float,
    forecast: float,
    anomalies: list[CostAnomaly],
    waste_items: list[WasteItem],
    top_services: list[ServiceSpend],
    webhook_url: str | None = None,
    channel: str | None = None,
) -> bool:
    """Backward-compatible wrapper used by ``finops analyze --slack``."""
    data = DigestData(
        total_mtd=total_mtd,
        forecast=forecast,
        anomalies=anomalies,
        waste_items=waste_items,
        top_services=top_services,
    )
    try:
        reporter = SlackReporter(webhook_url=webhook_url, channel=channel)
    except ValueError:
        logger.warning("Slack credentials not configured; skipping notification")
        return False
    return reporter.send_digest(data)
