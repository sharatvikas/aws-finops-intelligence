"""AWS Cost Explorer collector — fetch spend data by service, account, and time range."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)


class CostExplorerError(RuntimeError):
    """Raised when Cost Explorer data cannot be retrieved."""


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


@dataclass
class DimensionSpend:
    """Daily spend for one value of a Cost Explorer dimension (e.g. one usage type)."""

    dimension: str        # e.g. "USAGE_TYPE", "REGION", "LINKED_ACCOUNT"
    value: str            # e.g. "USE1-BoxUsage:c5.2xlarge"
    date: str
    amount: float


class CostExplorerCollector:
    """Collect cost data from AWS Cost Explorer.

    Cost Explorer is a global API served from us-east-1 regardless of the
    session's default region.
    """

    def __init__(self, session: boto3.Session | None = None) -> None:
        self._session = session or boto3.Session()
        self._ce = self._session.client("ce", region_name="us-east-1")

    def get_daily_spend(
        self,
        days: int = 30,
        group_by: str = "SERVICE",
        service_filter: str | None = None,
    ) -> list[DailySpend]:
        """Get daily spend broken down by a dimension (default: service).

        Args:
            days: Look-back window ending today.
            group_by: Cost Explorer dimension to group by
                ("SERVICE" or "LINKED_ACCOUNT").
            service_filter: Restrict results to a single service name.
        """
        end = date.today()
        start = end - timedelta(days=days)

        request: dict[str, Any] = {
            "TimePeriod": {"Start": str(start), "End": str(end)},
            "Granularity": "DAILY",
            "Metrics": ["UnblendedCost"],
            "GroupBy": [{"Type": "DIMENSION", "Key": group_by}],
        }
        if service_filter:
            request["Filter"] = {
                "Dimensions": {"Key": "SERVICE", "Values": [service_filter]}
            }

        results: list[DailySpend] = []
        for result in self._paginate_cost_and_usage(request):
            for group in result["Groups"]:
                key = group["Keys"][0]
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                unit = group["Metrics"]["UnblendedCost"]["Unit"]
                if amount > 0:
                    is_account = group_by == "LINKED_ACCOUNT"
                    results.append(
                        DailySpend(
                            date=result["TimePeriod"]["Start"],
                            service=(service_filter or "All Services") if is_account else key,
                            amount=amount,
                            unit=unit,
                            account_id=key if is_account else None,
                        )
                    )

        logger.debug("Fetched %d daily spend points (%d days, group_by=%s)", len(results), days, group_by)
        return results

    def get_dimension_daily_spend(
        self,
        dimension: str,
        days: int = 30,
        service_filter: str | None = None,
        top_n: int = 25,
    ) -> list[DimensionSpend]:
        """Daily spend grouped by an arbitrary dimension, optionally scoped to a service.

        Used for root-cause attribution: which USAGE_TYPE / REGION / LINKED_ACCOUNT
        moved when a service-level anomaly fires.
        """
        end = date.today()
        start = end - timedelta(days=days)

        request: dict[str, Any] = {
            "TimePeriod": {"Start": str(start), "End": str(end)},
            "Granularity": "DAILY",
            "Metrics": ["UnblendedCost"],
            "GroupBy": [{"Type": "DIMENSION", "Key": dimension}],
        }
        if service_filter:
            request["Filter"] = {
                "Dimensions": {"Key": "SERVICE", "Values": [service_filter]}
            }

        results: list[DimensionSpend] = []
        for result in self._paginate_cost_and_usage(request):
            for group in result["Groups"]:
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                if amount > 0:
                    results.append(
                        DimensionSpend(
                            dimension=dimension,
                            value=group["Keys"][0],
                            date=result["TimePeriod"]["Start"],
                            amount=amount,
                        )
                    )

        # Keep only the top_n values by total spend to bound downstream work.
        totals: dict[str, float] = {}
        for r in results:
            totals[r.value] = totals.get(r.value, 0.0) + r.amount
        keep = {v for v, _ in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:top_n]}
        return [r for r in results if r.value in keep]

    def get_service_spend_summary(self) -> list[ServiceSpend]:
        """Get MTD and last-30d spend per service for comparison."""
        today = date.today()
        month_start = today.replace(day=1)
        thirty_days_ago = today - timedelta(days=30)
        sixty_days_ago = today - timedelta(days=60)

        def _fetch(start: date, end: date) -> dict[str, float]:
            request: dict[str, Any] = {
                "TimePeriod": {"Start": str(start), "End": str(end)},
                "Granularity": "MONTHLY",
                "Metrics": ["UnblendedCost"],
                "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
            }
            totals: dict[str, float] = {}
            for result in self._paginate_cost_and_usage(request):
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
        except (ClientError, BotoCoreError) as exc:
            # Forecast requires >= 1 full day of usage history in the period;
            # degrade gracefully rather than failing the whole run.
            logger.warning("Cost forecast unavailable: %s", exc)
            return {"forecast_amount": 0, "unit": "USD"}

    def _paginate_cost_and_usage(self, request: dict[str, Any]) -> list[dict[str, Any]]:
        """Call get_cost_and_usage, following NextPageToken; merge ResultsByTime."""
        merged: list[dict[str, Any]] = []
        token: str | None = None
        try:
            while True:
                if token:
                    request["NextPageToken"] = token
                else:
                    request.pop("NextPageToken", None)
                resp = self._ce.get_cost_and_usage(**request)
                merged.extend(resp.get("ResultsByTime", []))
                token = resp.get("NextPageToken")
                if not token:
                    return merged
        except (ClientError, BotoCoreError) as exc:
            logger.error("Cost Explorer request failed: %s", exc)
            raise CostExplorerError(f"Cost Explorer request failed: {exc}") from exc
