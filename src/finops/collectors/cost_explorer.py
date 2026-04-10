"""AWS Cost Explorer collector — fetch spend data by service, account, and time range."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import boto3


@dataclass
class DailySpend:
    date: str
    service: str
    amount: float
    unit: str
    account_id: str | None = None


@dataclass
class ServiceSpend:
    service: str
    amount_mtd: float
    amount_last_30d: float
    amount_prev_30d: float
    change_pct: float
    unit: str = "USD"


class CostExplorerCollector:
    """Collect cost data from AWS Cost Explorer."""

    def __init__(self, session: boto3.Session) -> None:
        self._ce = session.client("ce", region_name="us-east-1")

    def get_daily_spend(self, days: int = 30) -> list[DailySpend]:
        """Get daily spend broken down by service for the last N days."""
        end = date.today()
        start = end - timedelta(days=days)

        resp = self._ce.get_cost_and_usage(
            TimePeriod={"Start": str(start), "End": str(end)},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )

        results = []
        for result in resp["ResultsByTime"]:
            for group in result["Groups"]:
                service = group["Keys"][0]
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                unit = group["Metrics"]["UnblendedCost"]["Unit"]
                if amount > 0:
                    results.append(DailySpend(
                        date=result["TimePeriod"]["Start"],
                        service=service,
                        amount=amount,
                        unit=unit,
                    ))

        return results

    def get_service_spend_summary(self) -> list[ServiceSpend]:
        """Get MTD and last-30d spend per service for comparison."""
        today = date.today()
        month_start = today.replace(day=1)
        thirty_days_ago = today - timedelta(days=30)
        sixty_days_ago = today - timedelta(days=60)

        def _fetch(start: date, end: date) -> dict[str, float]:
            resp = self._ce.get_cost_and_usage(
                TimePeriod={"Start": str(start), "End": str(end)},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
                GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
            )
            totals: dict[str, float] = {}
            for result in resp["ResultsByTime"]:
                for group in result["Groups"]:
                    svc = group["Keys"][0]
                    amt = float(group["Metrics"]["UnblendedCost"]["Amount"])
                    totals[svc] = totals.get(svc, 0) + amt
            return totals

        mtd = _fetch(month_start, today)
        last30 = _fetch(thirty_days_ago, today)
        prev30 = _fetch(sixty_days_ago, thirty_days_ago)

        services = set(list(last30.keys()) + list(prev30.keys()))
        results = []
        for svc in services:
            l30 = last30.get(svc, 0)
            p30 = prev30.get(svc, 0)
            change = ((l30 - p30) / p30 * 100) if p30 > 0 else 0
            if l30 > 1.0:  # Skip tiny amounts
                results.append(ServiceSpend(
                    service=svc,
                    amount_mtd=mtd.get(svc, 0),
                    amount_last_30d=l30,
                    amount_prev_30d=p30,
                    change_pct=change,
                ))

        return sorted(results, key=lambda s: s.amount_last_30d, reverse=True)

    def get_cost_forecast(self) -> dict[str, Any]:
        """Get end-of-month cost forecast."""
        today = date.today()
        month_end = (today.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)

        try:
            resp = self._ce.get_cost_forecast(
                TimePeriod={"Start": str(today + timedelta(days=1)), "End": str(month_end)},
                Metric="UNBLENDED_COST",
                Granularity="MONTHLY",
            )
            return {
                "forecast_amount": float(resp["Total"]["Amount"]),
                "unit": resp["Total"]["Unit"],
                "confidence_low": float(resp.get("ForecastResultsByTime", [{}])[0].get("MeanValue", 0)),
            }
        except Exception:
            return {"forecast_amount": 0, "unit": "USD"}
