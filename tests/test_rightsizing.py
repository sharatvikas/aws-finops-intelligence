"""Tests for the EC2 rightsizing analyzer — stubbed boto3 clients, no AWS calls."""

from __future__ import annotations

import boto3
import pytest
from botocore.exceptions import ClientError

from finops.analyzers.rightsizing import RightsizingAnalyzer
from finops.pricing import HOURS_PER_MONTH, downsize_instance_type, ec2_monthly_cost


def _instance(instance_id: str, instance_type: str, name: str | None = None) -> dict:
    inst = {"InstanceId": instance_id, "InstanceType": instance_type}
    if name:
        inst["Tags"] = [{"Key": "Name", "Value": name}]
    return inst


class FakeEC2:
    def __init__(self, instances: list[dict]):
        self._instances = instances

    def get_paginator(self, name: str):
        assert name == "describe_instances"
        instances = self._instances

        class _Paginator:
            def paginate(self, **kwargs):
                return [{"Reservations": [{"Instances": instances}]}]

        return _Paginator()


class FakeCloudWatch:
    """Returns configured (avg, p90) daily series per instance id."""

    def __init__(self, cpu_by_instance: dict[str, tuple[list[float], list[float]]]):
        self._cpu = cpu_by_instance
        self.error_instances: set[str] = set()

    def get_metric_data(self, MetricDataQueries, StartTime, EndTime):
        instance_id = MetricDataQueries[0]["MetricStat"]["Metric"]["Dimensions"][0]["Value"]
        if instance_id in self.error_instances:
            raise ClientError(
                {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}},
                "GetMetricData",
            )
        avgs, p90s = self._cpu.get(instance_id, ([], []))
        return {
            "MetricDataResults": [
                {"Id": "avg_cpu", "Values": avgs},
                {"Id": "p90_cpu", "Values": p90s},
            ]
        }


def _analyzer(instances, cpu_by_instance, **kwargs) -> RightsizingAnalyzer:
    session = boto3.Session(
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        region_name="us-east-1",
    )
    analyzer = RightsizingAnalyzer(session, region="us-east-1", **kwargs)
    analyzer._ec2 = FakeEC2(instances)
    analyzer._cw = FakeCloudWatch(cpu_by_instance)
    return analyzer


def _flat(value: float, days: int = 14) -> list[float]:
    return [value] * days


class TestRecommendations:
    def test_very_idle_instance_downsized_two_steps(self):
        analyzer = _analyzer(
            [_instance("i-1", "c5.4xlarge", name="analytics-worker")],
            {"i-1": (_flat(8.0), _flat(12.0))},
        )
        recs = analyzer.recommend()
        assert len(recs) == 1
        rec = recs[0]
        assert rec.recommended_type == "c5.xlarge"
        assert rec.confidence == "HIGH"
        assert rec.name == "analytics-worker"
        expected = (0.68 - 0.17) * HOURS_PER_MONTH
        assert rec.monthly_savings == pytest.approx(expected, rel=0.01)

    def test_moderately_idle_instance_downsized_one_step(self):
        analyzer = _analyzer(
            [_instance("i-2", "m5.2xlarge")],
            {"i-2": (_flat(20.0), _flat(35.0))},
        )
        recs = analyzer.recommend()
        assert len(recs) == 1
        assert recs[0].recommended_type == "m5.xlarge"
        assert recs[0].confidence == "MEDIUM"

    def test_healthy_instance_not_flagged(self):
        analyzer = _analyzer(
            [_instance("i-3", "m5.xlarge")],
            {"i-3": (_flat(45.0), _flat(72.0))},
        )
        assert analyzer.recommend() == []

    def test_burstable_family_limited_to_one_step(self):
        """t-family CPU-credit behaviour changes per size: never jump two."""
        analyzer = _analyzer(
            [_instance("i-4", "t3.xlarge")],
            {"i-4": (_flat(3.0), _flat(6.0))},
        )
        recs = analyzer.recommend()
        assert len(recs) == 1
        assert recs[0].recommended_type == "t3.large"

    def test_worst_daily_p90_governs(self):
        """One busy day (p90 85%) must veto a downsize even if the average is low."""
        analyzer = _analyzer(
            [_instance("i-5", "c5.2xlarge")],
            {"i-5": (_flat(10.0), [12.0] * 13 + [85.0])},
        )
        assert analyzer.recommend() == []

    def test_insufficient_datapoints_skipped(self):
        """A freshly launched instance (3 datapoints < min 7) is never flagged."""
        analyzer = _analyzer(
            [_instance("i-6", "c5.4xlarge")],
            {"i-6": (_flat(5.0, days=3), _flat(8.0, days=3))},
        )
        assert analyzer.recommend() == []

    def test_unknown_instance_type_skipped(self):
        analyzer = _analyzer(
            [_instance("i-7", "x2gd.metal")],
            {"i-7": (_flat(5.0), _flat(8.0))},
        )
        assert analyzer.recommend() == []

    def test_no_metrics_skipped(self):
        analyzer = _analyzer([_instance("i-8", "m5.large")], {})
        assert analyzer.recommend() == []

    def test_metrics_error_skips_instance_not_run(self):
        analyzer = _analyzer(
            [_instance("i-err", "c5.4xlarge"), _instance("i-ok", "m5.2xlarge")],
            {
                "i-err": (_flat(5.0), _flat(8.0)),
                "i-ok": (_flat(20.0), _flat(35.0)),
            },
        )
        analyzer._cw.error_instances.add("i-err")
        recs = analyzer.recommend()
        assert [r.resource_id for r in recs] == ["i-ok"]

    def test_sorted_by_savings_descending(self):
        analyzer = _analyzer(
            [_instance("i-small", "m5.xlarge"), _instance("i-big", "c5.12xlarge")],
            {
                "i-small": (_flat(10.0), _flat(15.0)),
                "i-big": (_flat(10.0), _flat(15.0)),
            },
        )
        recs = analyzer.recommend()
        assert len(recs) == 2
        assert recs[0].resource_id == "i-big"
        assert recs[0].monthly_savings > recs[1].monthly_savings


class TestPricingHelpers:
    def test_monthly_cost_known_type(self):
        assert ec2_monthly_cost("m5.large") == pytest.approx(0.096 * HOURS_PER_MONTH)

    def test_monthly_cost_unknown_type(self):
        assert ec2_monthly_cost("u-6tb1.metal") is None

    def test_downsize_one_step(self):
        assert downsize_instance_type("m5.2xlarge") == "m5.xlarge"

    def test_downsize_two_steps(self):
        assert downsize_instance_type("c5.4xlarge", steps=2) == "c5.xlarge"

    def test_downsize_skips_unpriced_sizes(self):
        # c5.8xlarge is not in the price table: 9xlarge steps down to 4xlarge
        assert downsize_instance_type("c5.9xlarge") == "c5.4xlarge"

    def test_downsize_at_floor_returns_none(self):
        assert downsize_instance_type("t3.nano") is None

    def test_downsize_malformed_type_returns_none(self):
        assert downsize_instance_type("not-a-type") is None
