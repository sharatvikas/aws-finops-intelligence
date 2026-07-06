"""Statistical anomaly detection for AWS cost data.

Detection strategy (per service, or per account when grouped that way):

1. Sort the daily-cost series and evaluate every point against a trailing
   window of ``lookback_days`` prior observations (rolling baseline — a spike
   anywhere in the series is caught, not just in the most recent days).
2. Score deviation with a robust z-score: ``0.6745 * (x - median) / MAD``.
   Median + MAD shrug off previous outliers that would inflate a mean/stddev
   baseline and mask follow-on anomalies.
3. Week-over-week seasonality guard: if the point is close to the median of
   the *same weekday* in the window, it is a weekly pattern (weekend batch
   jobs, Monday scale-ups) — suppressed, not an anomaly.
4. Severity is a 0-100 composite of statistical surprise (z), relative change,
   and absolute dollar impact, mapped to CRITICAL/HIGH/MEDIUM/LOW.

Root-cause hints: ``RootCauseAnalyzer`` re-queries Cost Explorer grouped by
USAGE_TYPE / REGION scoped to the anomalous service and reports which
dimension values moved the most versus their own baseline.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import date as date_cls

from finops.collectors.cost_explorer import DailySpend, DimensionSpend, ServiceSpend

logger = logging.getLogger(__name__)

# Scale factor that makes MAD consistent with the stddev of a normal distribution.
_MAD_TO_SIGMA = 0.6745
# Cap for the fallback z-score used when the baseline has zero dispersion
# (a perfectly flat series): any deviation is infinitely surprising, so we
# score it by relative magnitude instead and cap to keep sorting sane.
_ZERO_DISPERSION_Z_CAP = 100.0


@dataclass
class RootCauseHint:
    """One dimension value (usage type / region / account) that moved."""

    dimension: str            # "USAGE_TYPE" | "REGION" | "LINKED_ACCOUNT"
    value: str                # e.g. "USE1-BoxUsage:c5.2xlarge"
    anomaly_day_spend: float  # spend for this value on the anomalous day
    baseline_daily_avg: float
    delta: float              # anomaly_day_spend - baseline_daily_avg
    contribution_pct: float   # share of the total anomaly delta explained

    def summary(self) -> str:
        return (
            f"{self.dimension}={self.value}: ${self.anomaly_day_spend:,.2f} vs "
            f"${self.baseline_daily_avg:,.2f}/day baseline "
            f"(+${self.delta:,.2f}, {self.contribution_pct:.0f}% of the spike)"
        )


@dataclass
class CostAnomaly:
    service: str
    date: str
    amount: float
    baseline_avg: float          # kept for backward compatibility (mean of window)
    baseline_stddev: float       # kept for backward compatibility
    z_score: float               # robust (MAD-based) z-score
    change_pct: float            # % change vs the window median
    severity: str                # CRITICAL / HIGH / MEDIUM / LOW
    baseline_median: float = 0.0
    mad: float = 0.0
    weekday_median: float | None = None  # same-weekday baseline, if enough samples
    severity_score: float = 0.0          # 0-100 composite
    dollar_impact: float = 0.0           # amount - baseline_median
    account_id: str | None = None
    root_causes: list[RootCauseHint] = field(default_factory=list)


def _robust_z(value: float, window: list[float]) -> tuple[float, float, float]:
    """Return (robust_z, median, mad) of value against the window.

    Falls back to a classic z-score when MAD is zero, and to a capped
    relative-magnitude score when the window has no dispersion at all.
    """
    med = statistics.median(window)
    mad = statistics.median(abs(x - med) for x in window)

    if mad > 0:
        return _MAD_TO_SIGMA * (value - med) / mad, med, mad

    stdev = statistics.stdev(window) if len(window) > 1 else 0.0
    if stdev > 0:
        return (value - med) / stdev, med, mad

    # Perfectly flat baseline: any change is a genuine surprise. Score by
    # relative magnitude (10x per 100% change), capped.
    if value == med:
        return 0.0, med, mad
    rel = abs(value - med) / max(abs(med), 1e-9)
    z = min(rel * 10.0, _ZERO_DISPERSION_Z_CAP)
    return (z if value >= med else -z), med, mad


def _severity_score(z: float, change_pct: float, dollar_impact: float) -> float:
    """Composite 0-100 severity: statistical surprise + relative + absolute impact."""
    z_part = 40.0 * min(abs(z) / 8.0, 1.0)
    pct_part = 30.0 * min(max(change_pct, 0.0) / 200.0, 1.0)
    dollar_part = 30.0 * min(max(dollar_impact, 0.0) / 1000.0, 1.0)
    return round(z_part + pct_part + dollar_part, 1)


def _severity_label(score: float) -> str:
    if score >= 75:
        return "CRITICAL"
    if score >= 50:
        return "HIGH"
    if score >= 25:
        return "MEDIUM"
    return "LOW"


def _weekday_median(point_date: str, window: list[DailySpend]) -> float | None:
    """Median of same-weekday amounts within the window (needs >= 3 samples)."""
    try:
        weekday = date_cls.fromisoformat(point_date).weekday()
    except ValueError:
        return None
    same_day = [
        p.amount
        for p in window
        if date_cls.fromisoformat(p.date).weekday() == weekday
    ]
    if len(same_day) < 3:
        return None
    return statistics.median(same_day)


def detect_anomalies(
    daily_spend: list[DailySpend],
    z_threshold: float = 2.5,
    lookback_days: int = 21,
    min_change_pct: float = 20.0,
    min_baseline_spend: float = 1.0,
    min_history_days: int = 10,
    seasonal_tolerance_pct: float = 25.0,
) -> list[CostAnomaly]:
    """Detect cost anomalies using a rolling median + MAD z-score baseline.

    Every point (after ``min_history_days`` of history) is compared against the
    trailing ``lookback_days`` window, so spikes are detected anywhere in the
    series. A point that matches its same-weekday norm (within
    ``seasonal_tolerance_pct``) is treated as weekly seasonality and skipped.

    Args:
        daily_spend: Daily cost points (mixed services are grouped internally;
            points with an ``account_id`` are additionally split per account).
        z_threshold: Minimum robust z-score to flag.
        lookback_days: Max trailing window size for the baseline.
        min_change_pct: Minimum % increase vs the window median to flag
            (filters statistically-significant-but-tiny moves).
        min_baseline_spend: Ignore services whose baseline median is below this.
        min_history_days: Points evaluated only once this much history exists.
        seasonal_tolerance_pct: Deviation from the same-weekday median (in %)
            below which a point is considered normal weekly seasonality.
    """
    series: dict[tuple[str, str | None], list[DailySpend]] = {}
    for point in sorted(daily_spend, key=lambda x: x.date):
        series.setdefault((point.service, point.account_id), []).append(point)

    anomalies: list[CostAnomaly] = []
    for (service, account_id), points in series.items():
        if len(points) < min_history_days + 1:
            continue  # Not enough history

        for idx in range(min_history_days, len(points)):
            window_points = points[max(0, idx - lookback_days):idx]
            window = [p.amount for p in window_points]
            point = points[idx]

            z, median, mad = _robust_z(point.amount, window)
            if median < min_baseline_spend:
                continue  # Trivially small services

            change_pct = ((point.amount - median) / median) * 100 if median else 0.0
            if z < z_threshold or change_pct < min_change_pct:
                continue

            wk_median = _weekday_median(point.date, window_points)
            if wk_median is not None and wk_median > 0:
                weekday_dev_pct = abs(point.amount - wk_median) / wk_median * 100
                if weekday_dev_pct <= seasonal_tolerance_pct:
                    logger.debug(
                        "Suppressing %s %s: within %.0f%% of same-weekday median",
                        service, point.date, seasonal_tolerance_pct,
                    )
                    continue  # Weekly pattern, not an anomaly

            dollar_impact = point.amount - median
            score = _severity_score(z, change_pct, dollar_impact)
            anomalies.append(CostAnomaly(
                service=service,
                date=point.date,
                amount=point.amount,
                baseline_avg=statistics.mean(window),
                baseline_stddev=statistics.stdev(window) if len(window) > 1 else 0.0,
                z_score=z,
                change_pct=change_pct,
                severity=_severity_label(score),
                baseline_median=median,
                mad=mad,
                weekday_median=wk_median,
                severity_score=score,
                dollar_impact=dollar_impact,
                account_id=account_id,
            ))

    # Highest severity first; z-score as tie-breaker.
    anomalies.sort(key=lambda a: (a.severity_score, a.z_score), reverse=True)
    logger.info("Detected %d cost anomalies across %d series", len(anomalies), len(series))
    return anomalies


def detect_service_trend_anomalies(
    service_spends: list[ServiceSpend],
    change_threshold_pct: float = 30.0,
) -> list[ServiceSpend]:
    """Return services with significant month-over-month spend increases."""
    return [
        s for s in service_spends
        if s.change_pct >= change_threshold_pct and s.amount_last_30d > 100
    ]


class RootCauseAnalyzer:
    """Attribute a service-level cost anomaly to usage types / regions that moved.

    Works against any object exposing ``get_dimension_daily_spend(dimension,
    days, service_filter, top_n)`` returning ``list[DimensionSpend]`` (the
    Cost Explorer collector in production, a stub in tests).
    """

    DEFAULT_DIMENSIONS = ("USAGE_TYPE", "REGION")

    def __init__(self, collector, dimensions: tuple[str, ...] = DEFAULT_DIMENSIONS) -> None:
        self._collector = collector
        self._dimensions = dimensions

    def explain(
        self,
        anomaly: CostAnomaly,
        lookback_days: int = 30,
        max_hints: int = 3,
        min_delta: float = 1.0,
    ) -> list[RootCauseHint]:
        """Return the top dimension values whose spend moved on the anomaly day.

        For each configured dimension, compares each value's spend on the
        anomaly date against its own baseline daily average (all other days in
        the window) and ranks by absolute delta.
        """
        hints: list[RootCauseHint] = []
        total_delta = max(anomaly.dollar_impact, 1e-9)

        for dimension in self._dimensions:
            try:
                rows = self._collector.get_dimension_daily_spend(
                    dimension=dimension,
                    days=lookback_days,
                    service_filter=anomaly.service,
                )
            except Exception as exc:  # noqa: BLE001 — attribution is best-effort
                logger.warning(
                    "Root-cause query failed for %s/%s: %s",
                    anomaly.service, dimension, exc,
                )
                continue

            hints.extend(
                self._rank_movers(rows, dimension, anomaly.date, total_delta, min_delta)
            )

        hints.sort(key=lambda h: h.delta, reverse=True)
        return hints[:max_hints]

    @staticmethod
    def _rank_movers(
        rows: list[DimensionSpend],
        dimension: str,
        anomaly_date: str,
        total_delta: float,
        min_delta: float,
    ) -> list[RootCauseHint]:
        by_value: dict[str, dict[str, float]] = {}
        for row in rows:
            daily = by_value.setdefault(row.value, {})
            daily[row.date] = daily.get(row.date, 0.0) + row.amount

        hints: list[RootCauseHint] = []
        for value, by_date in by_value.items():
            day_spend = by_date.get(anomaly_date, 0.0)
            baseline = [amt for d, amt in by_date.items() if d != anomaly_date]
            baseline_avg = statistics.mean(baseline) if baseline else 0.0
            delta = day_spend - baseline_avg
            if delta < min_delta:
                continue
            hints.append(RootCauseHint(
                dimension=dimension,
                value=value,
                anomaly_day_spend=day_spend,
                baseline_daily_avg=baseline_avg,
                delta=delta,
                contribution_pct=min(delta / total_delta * 100, 100.0),
            ))
        return hints


def enrich_with_root_causes(
    anomalies: list[CostAnomaly],
    collector,
    max_anomalies: int = 5,
    lookback_days: int = 30,
) -> list[CostAnomaly]:
    """Attach root-cause hints to the top N anomalies (in place; returns the list).

    Bounded to ``max_anomalies`` to cap Cost Explorer API cost
    ($0.01 per request).
    """
    analyzer = RootCauseAnalyzer(collector)
    for anomaly in anomalies[:max_anomalies]:
        anomaly.root_causes = analyzer.explain(anomaly, lookback_days=lookback_days)
    return anomalies
