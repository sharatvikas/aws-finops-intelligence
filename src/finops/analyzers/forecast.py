"""Cost forecasting — project future AWS spend using linear regression and trend analysis.

Given 30+ days of historical Cost Explorer data, projects the next 30-90 days
using simple linear regression per service. Flags services with accelerating
spend (positive acceleration = growing faster than baseline).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from finops.collectors.cost_explorer import DailySpend


@dataclass
class ServiceForecast:
    service: str
    current_daily_avg: float           # avg spend last 7 days
    projected_30d: float               # projected total for next 30 days
    projected_90d: float               # projected total for next 90 days
    daily_growth_rate: float           # % change per day (positive = growing)
    acceleration: float                # change in growth rate (positive = accelerating)
    confidence: str                    # low | medium | high
    trend: str                         # stable | growing | shrinking | volatile
    anomalous: bool = False            # True if growth is unusually fast
    notes: list[str] = field(default_factory=list)


@dataclass
class CostForecast:
    forecast_date: date
    history_days: int
    total_current_monthly: float
    total_projected_30d: float
    total_projected_90d: float
    month_over_month_change_pct: float
    services: list[ServiceForecast]
    accelerating_services: list[str]   # services with notably increasing spend
    warnings: list[str]


def forecast_costs(daily_spend: list[DailySpend], forecast_days: int = 30) -> CostForecast:
    """
    Forecast future AWS costs from historical daily spend data.

    Args:
        daily_spend: At least 14 days of daily spend data from Cost Explorer.
        forecast_days: How many days forward to project.

    Returns:
        CostForecast with per-service projections and overall trend.
    """
    # Group by service
    by_service: dict[str, list[DailySpend]] = {}
    for d in sorted(daily_spend, key=lambda x: x.date):
        by_service.setdefault(d.service, []).append(d)

    service_forecasts: list[ServiceForecast] = []
    warnings: list[str] = []

    for service, points in by_service.items():
        if len(points) < 7:
            warnings.append(f"{service}: insufficient history ({len(points)} days), skipped")
            continue
        forecast = _forecast_service(service, points, forecast_days)
        service_forecasts.append(forecast)

    service_forecasts.sort(key=lambda s: s.projected_30d, reverse=True)

    # Overall totals
    total_current = sum(s.current_daily_avg * 30 for s in service_forecasts)
    total_30d = sum(s.projected_30d for s in service_forecasts)
    total_90d = sum(s.projected_90d for s in service_forecasts)

    mom_change = ((total_30d - total_current) / total_current * 100) if total_current > 0 else 0

    accelerating = [
        s.service for s in service_forecasts
        if s.anomalous or (s.daily_growth_rate > 2.0 and s.trend == "growing")
    ]

    return CostForecast(
        forecast_date=date.today(),
        history_days=max(len(v) for v in by_service.values()) if by_service else 0,
        total_current_monthly=round(total_current, 2),
        total_projected_30d=round(total_30d, 2),
        total_projected_90d=round(total_90d, 2),
        month_over_month_change_pct=round(mom_change, 1),
        services=service_forecasts,
        accelerating_services=accelerating,
        warnings=warnings,
    )


def _forecast_service(
    service: str,
    points: list[DailySpend],
    forecast_days: int,
) -> ServiceForecast:
    """Apply linear regression to project a single service's spend."""
    amounts = [p.amount for p in points]
    n = len(amounts)

    # Linear regression: y = slope * x + intercept
    x_mean = (n - 1) / 2.0
    y_mean = sum(amounts) / n

    numerator = sum((i - x_mean) * (amounts[i] - y_mean) for i in range(n))
    denominator = sum((i - x_mean) ** 2 for i in range(n))

    slope = numerator / denominator if denominator != 0 else 0.0
    intercept = y_mean - slope * x_mean

    # Project forward
    projected_amounts = [
        max(0.0, intercept + slope * (n + d))
        for d in range(forecast_days)
    ]
    projected_30d = sum(projected_amounts[:30])
    projected_90d = sum(projected_amounts[:90]) if forecast_days >= 90 else projected_30d * 3

    # Current daily average (last 7 days)
    recent = amounts[-7:]
    current_avg = sum(recent) / len(recent)

    # Daily growth rate (% per day relative to current avg)
    daily_growth_rate = (slope / current_avg * 100) if current_avg > 0 else 0.0

    # Acceleration: compare slope of first half vs second half
    mid = n // 2
    slope_first = _simple_slope(amounts[:mid])
    slope_second = _simple_slope(amounts[mid:])
    acceleration = slope_second - slope_first

    # Coefficient of variation (volatility)
    std_dev = math.sqrt(sum((a - y_mean) ** 2 for a in amounts) / n) if n > 1 else 0
    cv = std_dev / y_mean if y_mean > 0 else 0

    # Trend classification
    if cv > 0.4:
        trend = "volatile"
    elif daily_growth_rate > 1.0:
        trend = "growing"
    elif daily_growth_rate < -1.0:
        trend = "shrinking"
    else:
        trend = "stable"

    # Confidence based on data volume and volatility
    if n >= 30 and cv < 0.2:
        confidence = "high"
    elif n >= 14 and cv < 0.4:
        confidence = "medium"
    else:
        confidence = "low"

    # Anomalous: growing faster than 5% per day
    anomalous = daily_growth_rate > 5.0

    notes = []
    if anomalous:
        notes.append(f"Spend growing {daily_growth_rate:.1f}%/day — investigate for runaway resources")
    if cv > 0.4:
        notes.append("Highly volatile spend — forecast confidence low")
    if acceleration > slope * 0.5 and slope > 0:
        notes.append("Growth is accelerating — may reach double current rate within 30 days")

    return ServiceForecast(
        service=service,
        current_daily_avg=round(current_avg, 4),
        projected_30d=round(projected_30d, 2),
        projected_90d=round(projected_90d, 2),
        daily_growth_rate=round(daily_growth_rate, 3),
        acceleration=round(acceleration, 6),
        confidence=confidence,
        trend=trend,
        anomalous=anomalous,
        notes=notes,
    )


def _simple_slope(values: list[float]) -> float:
    """Compute linear regression slope for a 1D series."""
    n = len(values)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2.0
    y_mean = sum(values) / n
    num = sum((i - x_mean) * (values[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    return num / den if den != 0 else 0.0


def format_forecast_report(forecast: CostForecast) -> str:
    """Render the cost forecast as a Markdown report."""
    sign = "+" if forecast.month_over_month_change_pct >= 0 else ""
    lines = [
        "## AWS Cost Forecast",
        "",
        f"**Forecast date:** {forecast.forecast_date}  "
        f"|  **History:** {forecast.history_days} days",
        "",
        "| Period | Projected Total | vs Current |",
        "|---|---|---|",
        f"| Current (est. monthly) | ${forecast.total_current_monthly:,.2f} | baseline |",
        f"| Next 30 days | ${forecast.total_projected_30d:,.2f} | "
        f"{sign}{forecast.month_over_month_change_pct:.1f}% |",
        f"| Next 90 days | ${forecast.total_projected_90d:,.2f} | "
        f"{'3x' if forecast.month_over_month_change_pct == 0 else 'see trend'} |",
        "",
    ]

    if forecast.accelerating_services:
        lines.extend([
            "### Services with Rapidly Growing Spend",
            "",
            "> These services have accelerating cost growth and should be reviewed.",
            "",
        ])
        for svc_name in forecast.accelerating_services:
            svc = next((s for s in forecast.services if s.service == svc_name), None)
            if svc:
                lines.append(
                    f"- **{svc_name}**: +{svc.daily_growth_rate:.1f}%/day, "
                    f"${svc.projected_30d:,.2f} projected 30d"
                )
        lines.append("")

    lines.extend([
        "### Per-Service 30-Day Projections",
        "",
        "| Service | Current Daily Avg | 30-Day Projection | Trend | Confidence |",
        "|---|---|---|---|---|",
    ])

    for s in forecast.services[:15]:  # Top 15 by projected spend
        trend_icon = {
            "growing": "📈",
            "shrinking": "📉",
            "stable": "➡️",
            "volatile": "〰️",
        }.get(s.trend, "")
        lines.append(
            f"| {s.service} | ${s.current_daily_avg:,.4f} | "
            f"${s.projected_30d:,.2f} | {trend_icon} {s.trend} | {s.confidence} |"
        )

    if len(forecast.services) > 15:
        lines.append(f"| *...{len(forecast.services) - 15} more services* | | | | |")

    if forecast.warnings:
        lines.extend(["", "> **Warnings:**"])
        for w in forecast.warnings:
            lines.append(f"> - {w}")

    lines.extend([
        "",
        "> Forecasts use linear regression on historical Cost Explorer data. "
        "Accuracy depends on spend stability. High-volatility services show lower confidence.",
    ])
    return "\n".join(lines)
