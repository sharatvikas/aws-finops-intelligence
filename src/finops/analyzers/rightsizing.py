"""EC2 rightsizing recommendations from CloudWatch utilization percentiles.

Sizing on the P90 of CPU (not the average) means recommendations tolerate
daily peaks: an instance is only a downsize candidate if even its busy periods
would fit comfortably on the smaller size.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from finops.pricing import downsize_instance_type, ec2_monthly_cost

logger = logging.getLogger(__name__)


@dataclass
class RightsizingRecommendation:
    resource_id: str
    name: str
    resource_type: str            # "EC2 Instance"
    region: str
    current_type: str
    recommended_type: str
    avg_cpu_pct: float
    p90_cpu_pct: float
    current_monthly_cost: float
    projected_monthly_cost: float
    monthly_savings: float
    confidence: str               # HIGH / MEDIUM
    reason: str

    def summary(self) -> str:
        return (
            f"{self.name}: {self.current_type} → {self.recommended_type} "
            f"(save ${self.monthly_savings:,.0f}/mo, P90 CPU: {self.p90_cpu_pct:.0f}%)"
        )


class RightsizingAnalyzer:
    """Generate EC2 downsize recommendations based on CPU utilization percentiles.

    Heuristics:
      * P90 CPU < 20%  → recommend two sizes down (workload fits at ~4x density)
      * P90 CPU < 40%  → recommend one size down (~2x density)
      * Requires at least ``min_datapoints`` daily CloudWatch datapoints so a
        freshly-launched instance is never flagged.
      * Burstable (t*) instances are sized down at most one step — CPU-credit
        behaviour changes across t-family sizes.
    """

    def __init__(
        self,
        session: boto3.Session | None = None,
        region: str = "us-east-1",
        lookback_days: int = 14,
        min_datapoints: int = 7,
    ) -> None:
        self._session = session or boto3.Session()
        self._region = region
        self._lookback_days = lookback_days
        self._min_datapoints = min_datapoints
        self._ec2 = self._session.client("ec2", region_name=region)
        self._cw = self._session.client("cloudwatch", region_name=region)

    def recommend(self) -> list[RightsizingRecommendation]:
        """Analyze all running instances; return downsize recommendations."""
        recommendations: list[RightsizingRecommendation] = []
        for instance in self._running_instances():
            try:
                rec = self._analyze_instance(instance)
            except (ClientError, BotoCoreError) as exc:
                logger.warning(
                    "Skipping %s: metrics lookup failed: %s",
                    instance.get("InstanceId", "?"), exc,
                )
                continue
            if rec:
                recommendations.append(rec)

        recommendations.sort(key=lambda r: r.monthly_savings, reverse=True)
        logger.info("Generated %d rightsizing recommendations", len(recommendations))
        return recommendations

    # -- internals ----------------------------------------------------------

    def _running_instances(self) -> list[dict[str, Any]]:
        instances: list[dict[str, Any]] = []
        try:
            paginator = self._ec2.get_paginator("describe_instances")
            for page in paginator.paginate(
                Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
            ):
                for reservation in page["Reservations"]:
                    instances.extend(reservation["Instances"])
        except (ClientError, BotoCoreError) as exc:
            logger.error("Failed to list EC2 instances in %s: %s", self._region, exc)
            raise
        return instances

    def _analyze_instance(self, instance: dict[str, Any]) -> RightsizingRecommendation | None:
        instance_id = instance["InstanceId"]
        instance_type = instance["InstanceType"]
        name = _tag(instance.get("Tags", []), "Name") or instance_id

        current_cost = ec2_monthly_cost(instance_type)
        if current_cost is None:
            logger.debug("No pricing for %s (%s); skipping", instance_id, instance_type)
            return None

        stats = self._cpu_stats(instance_id)
        if stats is None:
            return None
        avg_cpu, p90_cpu, datapoints = stats
        if datapoints < self._min_datapoints:
            logger.debug(
                "%s has only %d datapoints (<%d); skipping",
                instance_id, datapoints, self._min_datapoints,
            )
            return None

        is_burstable = instance_type.startswith("t")
        if p90_cpu < 20 and not is_burstable:
            steps, confidence = 2, "HIGH"
        elif p90_cpu < 40:
            steps, confidence = 1, "HIGH" if p90_cpu < 20 else "MEDIUM"
        else:
            return None  # Utilization is healthy

        recommended = downsize_instance_type(instance_type, steps=steps)
        if recommended is None and steps == 2:
            recommended = downsize_instance_type(instance_type, steps=1)
            confidence = "MEDIUM"
        if recommended is None:
            return None  # Already at the smallest priced size

        projected_cost = ec2_monthly_cost(recommended)
        if projected_cost is None or projected_cost >= current_cost:
            return None

        return RightsizingRecommendation(
            resource_id=instance_id,
            name=name,
            resource_type="EC2 Instance",
            region=self._region,
            current_type=instance_type,
            recommended_type=recommended,
            avg_cpu_pct=round(avg_cpu, 1),
            p90_cpu_pct=round(p90_cpu, 1),
            current_monthly_cost=round(current_cost, 2),
            projected_monthly_cost=round(projected_cost, 2),
            monthly_savings=round(current_cost - projected_cost, 2),
            confidence=confidence,
            reason=(
                f"P90 CPU {p90_cpu:.1f}% / avg {avg_cpu:.1f}% over "
                f"{self._lookback_days} days ({datapoints} daily datapoints)"
            ),
        )

    def _cpu_stats(self, instance_id: str) -> tuple[float, float, int] | None:
        """Return (avg_cpu, p90_cpu, datapoint_count) over the lookback window.

        Uses a single GetMetricData call with two queries — GetMetricStatistics
        cannot return a plain statistic and a percentile in one request.
        """
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=self._lookback_days)
        metric = {
            "Namespace": "AWS/EC2",
            "MetricName": "CPUUtilization",
            "Dimensions": [{"Name": "InstanceId", "Value": instance_id}],
        }

        resp = self._cw.get_metric_data(
            MetricDataQueries=[
                {
                    "Id": "avg_cpu",
                    "MetricStat": {"Metric": metric, "Period": 86400, "Stat": "Average"},
                },
                {
                    "Id": "p90_cpu",
                    "MetricStat": {"Metric": metric, "Period": 86400, "Stat": "p90"},
                },
            ],
            StartTime=start,
            EndTime=end,
        )

        series = {r["Id"]: r.get("Values", []) for r in resp.get("MetricDataResults", [])}
        averages = series.get("avg_cpu", [])
        p90s = series.get("p90_cpu", [])
        if not averages:
            return None

        avg = sum(averages) / len(averages)
        # Worst daily P90 in the window — the busiest day must still fit.
        p90 = max(p90s) if p90s else avg
        return avg, p90, len(averages)


def _tag(tags: list[dict[str, str]], key: str) -> str | None:
    for t in tags:
        if t.get("Key") == key:
            return t.get("Value")
    return None
