"""CLI tests — command wiring, JSON output modes, and Slack dry-run end-to-end."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from click.testing import CliRunner

from finops.cli import cli
from finops.collectors.cost_explorer import DailySpend, DimensionSpend, ServiceSpend


@pytest.fixture
def runner():
    return CliRunner()


class StubCollector:
    """Stands in for CostExplorerCollector: 30 flat days + one 10x EC2 spike."""

    SPIKE_DATE = (date.today() - timedelta(days=3)).isoformat()

    def __init__(self, session=None):
        pass

    def get_daily_spend(self, days=30, group_by="SERVICE", service_filter=None):
        start = date.today() - timedelta(days=days)
        points = [
            DailySpend(date=(start + timedelta(days=i)).isoformat(),
                       service="Amazon EC2", amount=100.0, unit="USD")
            for i in range(days)
        ]
        for p in points:
            if p.date == self.SPIKE_DATE:
                p.amount = 1000.0
        return points

    def get_dimension_daily_spend(self, dimension, days=30, service_filter=None, top_n=25):
        start = date.today() - timedelta(days=days)
        rows = []
        for i in range(days):
            d = (start + timedelta(days=i)).isoformat()
            rows.append(DimensionSpend(
                dimension=dimension,
                value="BoxUsage:c5.2xlarge" if dimension == "USAGE_TYPE" else "us-east-1",
                date=d,
                amount=900.0 if d == self.SPIKE_DATE else 80.0,
            ))
        return rows

    def get_service_spend_summary(self):
        return [ServiceSpend(
            service="Amazon EC2", amount_mtd=3000.0,
            amount_last_30d=3900.0, amount_prev_30d=3000.0, change_pct=30.0,
        )]

    def get_cost_forecast(self):
        return {"forecast_amount": 4100.0, "unit": "USD"}


class StubWasteDetector:
    def __init__(self, session=None, region="us-east-1"):
        pass

    def detect_all(self):
        from finops.analyzers.waste import WasteItem
        return [WasteItem(
            category="Unattached EBS", resource_id="vol-123",
            resource_type="aws_ebs_volume (gp3, 100GB)", region="us-east-1",
            estimated_monthly_cost=80.0, reason="Unattached for 30 days",
            remediation="Delete it", details={},
        )]


class StubRightsizer:
    def __init__(self, session=None, region="us-east-1", lookback_days=14, min_datapoints=7):
        pass

    def recommend(self):
        from finops.analyzers.rightsizing import RightsizingRecommendation
        return [RightsizingRecommendation(
            resource_id="i-abc", name="worker", resource_type="EC2 Instance",
            region="us-east-1", current_type="c5.4xlarge", recommended_type="c5.xlarge",
            avg_cpu_pct=8.0, p90_cpu_pct=12.0, current_monthly_cost=496.4,
            projected_monthly_cost=124.1, monthly_savings=372.3,
            confidence="HIGH", reason="P90 CPU 12%",
        )]


@pytest.fixture
def stubbed_aws(monkeypatch):
    monkeypatch.setattr("finops.collectors.cost_explorer.CostExplorerCollector", StubCollector)
    monkeypatch.setattr("finops.analyzers.waste.WasteDetector", StubWasteDetector)
    monkeypatch.setattr("finops.analyzers.rightsizing.RightsizingAnalyzer", StubRightsizer)


class TestCliWiring:
    def test_help_lists_all_commands(self, runner):
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        for cmd in ("analyze", "scan", "rightsize", "anomalies", "waste", "forecast", "budgets", "savings"):
            assert cmd in result.output

    def test_global_profile_and_region_options(self, runner):
        result = runner.invoke(cli, ["--help"])
        assert "--profile" in result.output
        assert "--region" in result.output

    def test_anomalies_detect_help(self, runner):
        result = runner.invoke(cli, ["anomalies", "detect", "--help"])
        assert result.exit_code == 0
        assert "--threshold" in result.output
        assert "--root-cause" in result.output


class TestAnomaliesDetect:
    def test_json_output_includes_root_causes(self, runner, stubbed_aws):
        result = runner.invoke(cli, ["anomalies", "detect", "--json-output"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert len(payload) >= 1
        spike = payload[0]
        assert spike["service"] == "Amazon EC2"
        assert spike["date"] == StubCollector.SPIKE_DATE
        assert spike["robust_z"] > 2.5
        assert spike["severity"] in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
        hints = spike["root_causes"]
        assert any(h["value"] == "BoxUsage:c5.2xlarge" for h in hints)

    def test_threshold_flag_suppresses_detection(self, runner, stubbed_aws):
        result = runner.invoke(
            cli, ["anomalies", "detect", "--threshold", "1e9", "--json-output"]
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == []


class TestRightsize:
    def test_json_output(self, runner, stubbed_aws):
        result = runner.invoke(cli, ["rightsize", "--json-output"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload[0]["recommended_type"] == "c5.xlarge"
        assert payload[0]["monthly_savings"] == pytest.approx(372.3)

    def test_min_savings_filter(self, runner, stubbed_aws):
        result = runner.invoke(cli, ["rightsize", "--min-savings", "1000", "--json-output"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == []


class TestScan:
    def test_json_output_combines_all_findings(self, runner, stubbed_aws):
        result = runner.invoke(cli, ["scan", "--json-output"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["forecast"] == pytest.approx(4100.0)
        assert len(payload["anomalies"]) >= 1
        assert payload["waste"][0]["resource_id"] == "vol-123"
        assert payload["rightsizing"][0]["recommended_type"] == "c5.xlarge"
        assert payload["total_monthly_savings"] == pytest.approx(80.0 + 372.3)

    def test_slack_dry_run_prints_block_kit_payload(self, runner, stubbed_aws, monkeypatch):
        for var in ("SLACK_WEBHOOK_URL", "SLACK_BOT_TOKEN", "SLACK_CHANNEL"):
            monkeypatch.delenv(var, raising=False)
        result = runner.invoke(cli, ["scan", "--slack", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert '"blocks"' in result.output
        assert "AWS FinOps Digest" in result.output
        assert "c5.xlarge" in result.output  # rightsizing made it into the digest

    def test_slack_without_credentials_exits_2(self, runner, stubbed_aws, monkeypatch):
        for var in ("SLACK_WEBHOOK_URL", "SLACK_BOT_TOKEN", "SLACK_CHANNEL"):
            monkeypatch.delenv(var, raising=False)
        result = runner.invoke(cli, ["scan", "--slack"])
        assert result.exit_code == 2
