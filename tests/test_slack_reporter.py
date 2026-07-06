"""Tests for the Slack reporter — block building, transports, and dry-run."""

from __future__ import annotations

import json

import pytest

from finops.analyzers.anomaly import CostAnomaly, RootCauseHint
from finops.analyzers.rightsizing import RightsizingRecommendation
from finops.analyzers.waste import WasteItem
from finops.reporters.slack import (
    DigestData,
    SlackReporter,
    build_digest_blocks,
    digest_fallback_text,
    post_daily_digest,
)


def _anomaly(**overrides) -> CostAnomaly:
    base = dict(
        service="Amazon EC2",
        date="2026-07-01",
        amount=8200.0,
        baseline_avg=5700.0,
        baseline_stddev=300.0,
        z_score=5.6,
        change_pct=43.0,
        severity="CRITICAL",
        baseline_median=5700.0,
        dollar_impact=2500.0,
    )
    base.update(overrides)
    return CostAnomaly(**base)


def _waste() -> WasteItem:
    return WasteItem(
        category="Unattached EBS",
        resource_id="vol-0abc123",
        resource_type="aws_ebs_volume (gp3, 340GB)",
        region="us-east-1",
        estimated_monthly_cost=840.0,
        reason="Volume unattached for 45 days",
        remediation="Delete after snapshot",
        details={},
    )


def _rec() -> RightsizingRecommendation:
    return RightsizingRecommendation(
        resource_id="i-0def456",
        name="analytics-worker",
        resource_type="EC2 Instance",
        region="us-east-1",
        current_type="c5.4xlarge",
        recommended_type="c5.xlarge",
        avg_cpu_pct=8.0,
        p90_cpu_pct=12.0,
        current_monthly_cost=496.4,
        projected_monthly_cost=124.1,
        monthly_savings=372.3,
        confidence="HIGH",
        reason="P90 CPU 12.0% over 14 days",
    )


class TestBuildDigestBlocks:
    def test_minimal_digest_has_header_summary_footer(self):
        blocks = build_digest_blocks(DigestData(total_mtd=47293.0, forecast=61800.0))
        types = [b["type"] for b in blocks]
        assert types[0] == "header"
        assert "section" in types
        assert types[-1] == "context"

    def test_summary_fields_render_dollars(self):
        blocks = build_digest_blocks(DigestData(total_mtd=47293.0, forecast=61800.0))
        summary = blocks[1]["fields"]
        assert any("$47,293" in f["text"] for f in summary)
        assert any("$61,800" in f["text"] for f in summary)

    def test_anomaly_section_includes_root_cause_hints(self):
        anomaly = _anomaly(root_causes=[RootCauseHint(
            dimension="USAGE_TYPE",
            value="BoxUsage:c5.2xlarge",
            anomaly_day_spend=2600.0,
            baseline_daily_avg=300.0,
            delta=2300.0,
            contribution_pct=92.0,
        )])
        blocks = build_digest_blocks(DigestData(anomalies=[anomaly]))
        text = json.dumps(blocks)
        assert "Amazon EC2" in text
        assert "BoxUsage:c5.2xlarge" in text
        assert "92%" in text

    def test_rightsizing_section_shows_dollar_impact(self):
        blocks = build_digest_blocks(DigestData(rightsizing=[_rec()]))
        text = json.dumps(blocks)
        assert "c5.4xlarge" in text and "c5.xlarge" in text
        assert "$372" in text

    def test_waste_section_sorted_by_cost(self):
        cheap = _waste()
        cheap.resource_id = "vol-cheap"
        cheap.estimated_monthly_cost = 5.0
        blocks = build_digest_blocks(DigestData(waste_items=[cheap, _waste()]))
        text = json.dumps(blocks)
        assert text.index("vol-0abc123") < text.index("vol-cheap")

    def test_savings_total_combines_waste_and_rightsizing(self):
        blocks = build_digest_blocks(
            DigestData(waste_items=[_waste()], rightsizing=[_rec()])
        )
        summary = json.dumps(blocks[1]["fields"])
        assert "$1,212" in summary  # 840 + 372.3

    def test_sections_capped(self):
        many = [_anomaly(date=f"2026-07-{i:02d}") for i in range(1, 21)]
        blocks = build_digest_blocks(DigestData(anomalies=many), max_per_section=5)
        anomaly_block = next(
            b for b in blocks
            if b["type"] == "section" and "Cost Anomalies" in b.get("text", {}).get("text", "")
        )
        # 5 anomalies -> 5 lines after the title
        assert len(anomaly_block["text"]["text"].splitlines()) == 6

    def test_fallback_text_mentions_counts(self):
        text = digest_fallback_text(DigestData(total_mtd=100.0, anomalies=[_anomaly()]))
        assert "1 anomalies" in text


class TestSlackReporterTransports:
    def test_requires_credentials_unless_dry_run(self, monkeypatch):
        for var in ("SLACK_WEBHOOK_URL", "SLACK_BOT_TOKEN", "SLACK_CHANNEL"):
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(ValueError, match="No Slack credentials"):
            SlackReporter()
        assert SlackReporter(dry_run=True) is not None

    def test_bot_token_requires_channel(self, monkeypatch):
        for var in ("SLACK_WEBHOOK_URL", "SLACK_CHANNEL"):
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(ValueError, match="requires a channel"):
            SlackReporter(bot_token="xoxb-test")

    def test_dry_run_prints_payload_and_sends_nothing(self, capsys, monkeypatch):
        def _explode(*a, **k):
            raise AssertionError("network client constructed during dry run")

        monkeypatch.setattr("finops.reporters.slack.WebhookClient", _explode)
        monkeypatch.setattr("finops.reporters.slack.WebClient", _explode)

        reporter = SlackReporter(dry_run=True, channel="#finops")
        ok = reporter.send_digest(DigestData(total_mtd=1234.0, anomalies=[_anomaly()]))

        assert ok is True
        payload = json.loads(capsys.readouterr().out)
        assert payload["channel"] == "#finops"
        assert payload["blocks"][0]["type"] == "header"
        assert "Amazon EC2" in json.dumps(payload["blocks"])

    def test_webhook_success(self, monkeypatch):
        sent = {}

        class FakeWebhookClient:
            def __init__(self, url, timeout=10):
                sent["url"] = url

            def send(self, text, blocks):
                sent["text"], sent["blocks"] = text, blocks
                return type("Resp", (), {"status_code": 200, "body": "ok"})()

        monkeypatch.setattr("finops.reporters.slack.WebhookClient", FakeWebhookClient)
        reporter = SlackReporter(webhook_url="https://hooks.slack.example/T00/B00/xyz")
        assert reporter.send_digest(DigestData(total_mtd=10.0)) is True
        assert sent["url"].startswith("https://hooks.slack.example")
        assert sent["blocks"][0]["type"] == "header"

    def test_webhook_http_error_returns_false(self, monkeypatch):
        class FakeWebhookClient:
            def __init__(self, url, timeout=10):
                pass

            def send(self, text, blocks):
                return type("Resp", (), {"status_code": 500, "body": "server_error"})()

        monkeypatch.setattr("finops.reporters.slack.WebhookClient", FakeWebhookClient)
        reporter = SlackReporter(webhook_url="https://hooks.slack.example/T00/B00/xyz")
        assert reporter.send_digest(DigestData()) is False

    def test_bot_token_posts_to_channel(self, monkeypatch):
        posted = {}

        class FakeWebClient:
            def __init__(self, token, timeout=10):
                posted["token"] = token

            def chat_postMessage(self, channel, text, blocks):
                posted["channel"] = channel
                posted["blocks"] = blocks
                return {"ok": True}

        monkeypatch.setattr("finops.reporters.slack.WebClient", FakeWebClient)
        monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
        reporter = SlackReporter(bot_token="xoxb-test", channel="#finops-alerts")
        assert reporter.send_digest(DigestData(total_mtd=5.0)) is True
        assert posted["token"] == "xoxb-test"
        assert posted["channel"] == "#finops-alerts"

    def test_bot_api_error_returns_false(self, monkeypatch):
        from slack_sdk.errors import SlackApiError

        class FakeWebClient:
            def __init__(self, token, timeout=10):
                pass

            def chat_postMessage(self, channel, text, blocks):
                raise SlackApiError("channel_not_found", {"error": "channel_not_found"})

        monkeypatch.setattr("finops.reporters.slack.WebClient", FakeWebClient)
        monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
        reporter = SlackReporter(bot_token="xoxb-test", channel="#nope")
        assert reporter.send_digest(DigestData()) is False


class TestBackwardCompatWrapper:
    def test_post_daily_digest_skips_gracefully_without_credentials(self, monkeypatch):
        for var in ("SLACK_WEBHOOK_URL", "SLACK_BOT_TOKEN", "SLACK_CHANNEL"):
            monkeypatch.delenv(var, raising=False)
        ok = post_daily_digest(100.0, 200.0, [], [], [])
        assert ok is False
