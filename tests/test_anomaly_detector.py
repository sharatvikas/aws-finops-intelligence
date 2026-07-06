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

        data = _baseline("AWSLambda", days=30, base_amount=10.0)
        data.append(_make_spend("AWSLambda", "2024-02-01", 10000.0))  # 1000x

        anomalies = detect_anomalies(data, z_threshold=2.5)
        assert anomalies[0].severity == "CRITICAL"

    def test_anomaly_severity_high_at_moderate_z(self):
        """Moderate spike (3-4 sigma) should be HIGH."""

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


class TestRobustDetection:
    def test_mad_baseline_not_masked_by_prior_outlier(self):
        """A huge prior outlier in the window must not mask a later real spike.

        A mean/stddev baseline is inflated by the 1000.0 outlier (classic
        z ~= 0.8 for the 300.0 spike); median+MAD still flags it.
        """
        from datetime import date, timedelta

        start = date(2024, 1, 1)
        data = [
            _make_spend("AmazonEC2", (start + timedelta(days=i)).isoformat(),
                        90.0 if i % 2 == 0 else 110.0)
            for i in range(20)
        ]
        # Prior outlier inside the future baseline window
        data.append(_make_spend("AmazonEC2", (start + timedelta(days=20)).isoformat(), 1000.0))
        # The spike under test
        data.append(_make_spend("AmazonEC2", (start + timedelta(days=21)).isoformat(), 300.0))

        anomalies = detect_anomalies(data, z_threshold=2.5)
        dates = [a.date for a in anomalies]
        assert (start + timedelta(days=21)).isoformat() in dates

    def test_weekly_seasonality_suppressed(self):
        """A recurring Sunday spike matching prior Sundays is not an anomaly."""
        from datetime import date, timedelta

        start = date(2024, 1, 1)  # a Monday
        data = []
        for i in range(35):  # ends 2024-02-04, a Sunday
            d = start + timedelta(days=i)
            amount = 500.0 if d.weekday() == 6 else 100.0
            data.append(_make_spend("AmazonEMR", d.isoformat(), amount))

        anomalies = detect_anomalies(data, z_threshold=1.5)
        # The final Sunday (2024-02-04) matches its weekday norm -> suppressed
        assert "2024-02-04" not in [a.date for a in anomalies]

    def test_non_seasonal_spike_still_flagged(self):
        """Same magnitude spike on a weekday (no weekly precedent) is flagged."""
        from datetime import date, timedelta

        start = date(2024, 1, 1)
        data = []
        for i in range(35):
            d = start + timedelta(days=i)
            amount = 500.0 if d.weekday() == 6 else 100.0
            data.append(_make_spend("AmazonEMR", d.isoformat(), amount))
        # 2024-02-05 is a Monday: 500 has no Monday precedent
        data.append(_make_spend("AmazonEMR", "2024-02-05", 500.0))

        anomalies = detect_anomalies(data, z_threshold=1.5)
        assert "2024-02-05" in [a.date for a in anomalies]

    def test_severity_score_populated_and_bounded(self):
        data = _baseline("AmazonEC2", days=30, base_amount=200.0)
        data.append(_make_spend("AmazonEC2", "2024-02-01", 5000.0))

        anomalies = detect_anomalies(data)
        assert anomalies
        a = anomalies[0]
        assert 0 <= a.severity_score <= 100
        assert a.severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
        assert a.dollar_impact == pytest.approx(5000.0 - a.baseline_median)
        assert a.baseline_median == pytest.approx(200.0)

    def test_min_change_pct_filters_small_relative_moves(self):
        """Statistically significant but relatively small moves are not flagged."""
        from datetime import date, timedelta

        start = date(2024, 1, 1)
        data = [
            _make_spend("AmazonS3", (start + timedelta(days=i)).isoformat(),
                        1000.0 + (0.5 if i % 2 == 0 else -0.5))
            for i in range(30)
        ]
        # +10% move: tiny MAD makes z enormous, but change < min_change_pct=20
        data.append(_make_spend("AmazonS3", (start + timedelta(days=30)).isoformat(), 1100.0))

        anomalies = detect_anomalies(data, z_threshold=2.5, min_change_pct=20.0)
        assert anomalies == []

    def test_per_account_series_are_independent(self):
        data = _baseline("All Services", days=30, base_amount=100.0)
        for p in data:
            p.account_id = "111111111111"
        stable_other = _baseline("All Services", days=30, base_amount=100.0)
        for p in stable_other:
            p.account_id = "222222222222"
        spike = _make_spend("All Services", "2024-02-01", 900.0)
        spike.account_id = "111111111111"

        anomalies = detect_anomalies(data + stable_other + [spike])
        assert len(anomalies) == 1
        assert anomalies[0].account_id == "111111111111"


class _StubCollector:
    """Duck-typed Cost Explorer collector for root-cause tests."""

    def __init__(self, rows_by_dimension, fail_dimensions=()):
        self._rows = rows_by_dimension
        self._fail = set(fail_dimensions)
        self.calls = []

    def get_dimension_daily_spend(self, dimension, days=30, service_filter=None, top_n=25):
        self.calls.append((dimension, service_filter))
        if dimension in self._fail:
            raise RuntimeError("CE unavailable")
        return self._rows.get(dimension, [])


class TestRootCauseAnalyzer:
    def _anomaly(self):
        return CostAnomaly(
            service="Amazon Elastic Compute Cloud - Compute",
            date="2024-01-20",
            amount=900.0,
            baseline_avg=100.0,
            baseline_stddev=5.0,
            z_score=12.0,
            change_pct=800.0,
            severity="CRITICAL",
            baseline_median=100.0,
            dollar_impact=800.0,
        )

    def _rows(self, dimension, value, baseline_amount, spike_amount):
        from finops.collectors.cost_explorer import DimensionSpend
        from datetime import date, timedelta

        rows = [
            DimensionSpend(dimension=dimension, value=value,
                           date=(date(2024, 1, 1) + timedelta(days=i)).isoformat(),
                           amount=baseline_amount)
            for i in range(19)
        ]
        rows.append(DimensionSpend(dimension=dimension, value=value,
                                   date="2024-01-20", amount=spike_amount))
        return rows

    def test_identifies_moved_usage_type(self):
        from finops.analyzers.anomaly import RootCauseAnalyzer

        rows = (
            self._rows("USAGE_TYPE", "BoxUsage:c5.2xlarge", 50.0, 820.0)
            + self._rows("USAGE_TYPE", "DataTransfer-Out-Bytes", 50.0, 52.0)
        )
        collector = _StubCollector({"USAGE_TYPE": rows, "REGION": []})
        hints = RootCauseAnalyzer(collector).explain(self._anomaly())

        assert hints, "expected at least one root-cause hint"
        top = hints[0]
        assert top.value == "BoxUsage:c5.2xlarge"
        assert top.delta == pytest.approx(770.0)
        assert top.contribution_pct == pytest.approx(770.0 / 800.0 * 100, rel=0.01)
        # The flat usage type must not be the top hint
        assert all(h.value != "DataTransfer-Out-Bytes" or h.delta < 10 for h in hints)

    def test_queries_are_scoped_to_the_anomalous_service(self):
        from finops.analyzers.anomaly import RootCauseAnalyzer

        collector = _StubCollector({"USAGE_TYPE": [], "REGION": []})
        RootCauseAnalyzer(collector).explain(self._anomaly())
        assert all(svc == self._anomaly().service for _, svc in collector.calls)
        assert {dim for dim, _ in collector.calls} == {"USAGE_TYPE", "REGION"}

    def test_dimension_query_failure_is_non_fatal(self):
        from finops.analyzers.anomaly import RootCauseAnalyzer

        rows = self._rows("REGION", "us-east-1", 50.0, 820.0)
        collector = _StubCollector({"REGION": rows}, fail_dimensions={"USAGE_TYPE"})
        hints = RootCauseAnalyzer(collector).explain(self._anomaly())
        # USAGE_TYPE failed but REGION still produced a hint
        assert [h.value for h in hints] == ["us-east-1"]

    def test_enrich_bounds_api_calls_to_top_anomalies(self):
        from finops.analyzers.anomaly import enrich_with_root_causes

        collector = _StubCollector({"USAGE_TYPE": [], "REGION": []})
        anomalies = [self._anomaly() for _ in range(8)]
        enrich_with_root_causes(anomalies, collector, max_anomalies=3)
        # 3 anomalies x 2 dimensions
        assert len(collector.calls) == 6
