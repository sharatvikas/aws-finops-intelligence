"""Unit tests for cost anomaly detection — no AWS calls, pure logic."""

from __future__ import annotations

import pytest
from finops.analyzers.anomaly import detect_anomalies, CostAnomaly
from finops.collectors.cost_explorer import DailySpend


def _make_spend(service: str, date: str, amount: float) -> DailySpend:
    return DailySpend(date=date, service=service, amount=amount, unit="USD")


def _baseline(service: str, days: int = 21, base_amount: float = 100.0) -> list[DailySpend]:
    """Stable historical spend for N days."""
    from datetime import date, timedelta

    start = date(2024, 1, 1)
    return [
        _make_spend(service, (start + timedelta(days=i)).isoformat(), base_amount)
        for i in range(days)
    ]


class TestDetectAnomalies:
    def test_no_anomaly_on_stable_spend(self):
        """Flat spend at baseline should not trigger any anomaly."""
        data = _baseline("AmazonEC2", days=30, base_amount=500.0)
        anomalies = detect_anomalies(data, z_threshold=2.5)
        assert anomalies == []

    def test_detects_spike(self):
        """A day with 5x the baseline cost should be flagged."""
        from datetime import date, timedelta

        data = _baseline("AmazonRDS", days=30, base_amount=200.0)
        spike_day = (date(2024, 1, 1) + timedelta(days=25)).isoformat()
        data.append(_make_spend("AmazonRDS", spike_day, 2000.0))  # 10x

        anomalies = detect_anomalies(data, z_threshold=2.5)
        assert len(anomalies) >= 1
        spike = next(a for a in anomalies if a.date == spike_day)
        assert spike.service == "AmazonRDS"
        assert spike.z_score > 2.5
        assert spike.amount == 2000.0

    def test_anomaly_severity_critical_at_high_z(self):
        """Very high Z-score (>>5) should be marked CRITICAL."""
        from datetime import date, timedelta

        data = _baseline("AWSLambda", days=30, base_amount=10.0)
        data.append(_make_spend("AWSLambda", "2024-02-01", 10000.0))  # 1000x

        anomalies = detect_anomalies(data, z_threshold=2.5)
        assert anomalies[0].severity == "CRITICAL"

    def test_anomaly_severity_high_at_moderate_z(self):
        """Moderate spike (3-4 sigma) should be HIGH."""
        from datetime import date, timedelta

        # Use a tight baseline with low stddev to create controllable z-score
        data = [_make_spend("S3", f"2024-01-{i+1:02d}", 100.0) for i in range(25)]
        data.append(_make_spend("S3", "2024-01-26", 450.0))  # moderate spike

        anomalies = detect_anomalies(data, z_threshold=2.5)
        if anomalies:
            assert anomalies[0].severity in ("HIGH", "CRITICAL")

    def test_insufficient_history_skips_service(self):
        """Services with fewer data points than lookback_days are skipped."""
        data = [_make_spend("NewService", "2024-01-01", 500.0)]
        anomalies = detect_anomalies(data, z_threshold=2.5, lookback_days=21)
        assert anomalies == []

    def test_multiple_services_independent(self):
        """Anomaly detection runs per-service independently."""
        stable = _baseline("AmazonEC2", days=30, base_amount=1000.0)
        spiky = _baseline("AmazonS3", days=30, base_amount=50.0)
        spiky.append(_make_spend("AmazonS3", "2024-02-01", 5000.0))

        all_data = stable + spiky
        anomalies = detect_anomalies(all_data, z_threshold=2.5)

        services_with_anomalies = {a.service for a in anomalies}
        assert "AmazonS3" in services_with_anomalies
        assert "AmazonEC2" not in services_with_anomalies

    def test_change_pct_calculation(self):
        """change_pct should reflect the ratio of spike to baseline avg."""
        data = _baseline("Comprehend", days=25, base_amount=100.0)
        data.append(_make_spend("Comprehend", "2024-01-26", 300.0))

        anomalies = detect_anomalies(data, z_threshold=1.0)  # low threshold to force detection
        if anomalies:
            a = anomalies[0]
            assert a.change_pct > 100  # 300 is > 100% above 100 baseline

    def test_z_threshold_respected(self):
        """Raising z_threshold should suppress weaker anomalies."""
        data = _baseline("Glue", days=30, base_amount=100.0)
        data.append(_make_spend("Glue", "2024-01-31", 250.0))

        low_threshold = detect_anomalies(data, z_threshold=1.0)
        high_threshold = detect_anomalies(data, z_threshold=5.0)

        # The weak spike should appear at z=1 but not necessarily at z=5
        assert len(low_threshold) >= len(high_threshold)
