"""Statistical anomaly detection for AWS cost data."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

from finops.collectors.cost_explorer import DailySpend, ServiceSpend


@dataclass
class CostAnomaly:
    service: str
    date: str
    amount: float
    baseline_avg: float
    baseline_stddev: float
    z_score: float
    change_pct: float
    severity: str  # CRITICAL / HIGH / MEDIUM


def detect_anomalies(
    daily_spend: list[DailySpend],
    z_threshold: float = 2.5,
    lookback_days: int = 21,
) -> list[CostAnomaly]:
    """
    Detect cost anomalies using Z-score against a rolling baseline.
    Compares recent spend to the prior lookback_days window.
    """
    # Group by service
    by_service: dict[str, list[DailySpend]] = {}
    for d in sorted(daily_spend, key=lambda x: x.date):
        by_service.setdefault(d.service, []).append(d)

    anomalies = []
    for service, points in by_service.items():
        if len(points) < lookback_days + 1:
            continue  # Not enough history

        baseline_points = points[:-7]  # Exclude last 7 days from baseline
        recent_points = points[-7:]    # Check last 7 days

        if len(baseline_points) < 7:
            continue

        baseline_amounts = [p.amount for p in baseline_points[-lookback_days:]]
        baseline_avg = statistics.mean(baseline_amounts)
        baseline_std = statistics.stdev(baseline_amounts) if len(baseline_amounts) > 1 else 0

        if baseline_avg < 1.0:  # Skip trivially small services
            continue

        for point in recent_points:
            if baseline_std == 0:
                z = 0.0
            else:
                z = (point.amount - baseline_avg) / baseline_std

            change_pct = ((point.amount - baseline_avg) / baseline_avg) * 100

            if z >= z_threshold and change_pct >= 20:
                if z >= 4.0:
                    severity = "CRITICAL"
                elif z >= 3.0:
                    severity = "HIGH"
                else:
                    severity = "MEDIUM"

                anomalies.append(CostAnomaly(
                    service=service,
                    date=point.date,
                    amount=point.amount,
                    baseline_avg=baseline_avg,
                    baseline_stddev=baseline_std,
                    z_score=z,
                    change_pct=change_pct,
                    severity=severity,
                ))

    return sorted(anomalies, key=lambda a: a.z_score, reverse=True)


def detect_service_trend_anomalies(
    service_spends: list[ServiceSpend],
    change_threshold_pct: float = 30.0,
) -> list[ServiceSpend]:
    """Return services with significant month-over-month spend increases."""
    return [
        s for s in service_spends
        if s.change_pct >= change_threshold_pct and s.amount_last_30d > 100
    ]
