"""AWS Budgets integration — list, track, and alert on cost budgets.

Wraps the AWS Budgets API (cost-explorer + budgets service) to provide:
  - Budget listing with health indicators
  - Per-budget spend history
  - Threshold violation detection
  - Budget creation helpers (via CLI)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional

import boto3
from botocore.exceptions import ClientError


@dataclass
class BudgetStatus:
    name: str
    budget_type: str           # COST, USAGE, RI_UTILIZATION, etc.
    time_unit: str             # MONTHLY, QUARTERLY, ANNUALLY
    limit_amount: float
    limit_unit: str            # USD or usage unit
    actual_spend: float
    forecasted_spend: float
    threshold_pct: float       # Alert threshold (% of limit)
    pct_used: float            # actual / limit * 100
    pct_forecasted: float      # forecasted / limit * 100
    start_date: str
    end_date: str
    notifications: list[dict] = field(default_factory=list)

    @property
    def health(self) -> str:
        """CRITICAL / WARNING / OK based on forecasted spend vs limit."""
        if self.pct_forecasted >= 100:
            return "CRITICAL"
        if self.pct_forecasted >= self.threshold_pct:
            return "WARNING"
        if self.pct_used >= self.threshold_pct:
            return "WARNING"
        return "OK"

    @property
    def overage(self) -> float:
        """How much over the limit (negative = under)."""
        return self.actual_spend - self.limit_amount

    @property
    def days_remaining_in_period(self) -> int:
        try:
            end = datetime.strptime(self.end_date[:10], "%Y-%m-%d").date()
            return max(0, (end - date.today()).days)
        except Exception:
            return -1

    @property
    def burn_rate_daily(self) -> float:
        """USD per day needed to stay within budget for the rest of the period."""
        remaining_budget = self.limit_amount - self.actual_spend
        days = self.days_remaining_in_period
        if days <= 0:
            return 0.0
        return remaining_budget / days


class BudgetManager:
    """Interact with AWS Budgets API."""

    def __init__(self) -> None:
        self._account_id = os.environ.get("AWS_ACCOUNT_ID", "")
        self._client = boto3.client("budgets", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
        self._ce = boto3.client("ce", region_name="us-east-1")

        if not self._account_id:
            try:
                sts = boto3.client("sts")
                self._account_id = sts.get_caller_identity()["Account"]
            except Exception:
                self._account_id = "000000000000"

    def list_budgets(self) -> list[BudgetStatus]:
        """Return all budgets with current spend and forecast."""
        try:
            paginator = self._client.get_paginator("describe_budgets")
            pages = paginator.paginate(AccountId=self._account_id)
        except ClientError as e:
            if "mock" in str(e).lower() or "endpoint" in str(e).lower():
                return _mock_budgets()
            raise

        statuses = []
        for page in pages:
            for b in page.get("Budgets", []):
                try:
                    status = self._parse_budget(b)
                    statuses.append(status)
                except Exception:
                    continue

        return sorted(statuses, key=lambda s: s.pct_forecasted, reverse=True)

    def _parse_budget(self, b: dict) -> BudgetStatus:
        limit = b.get("BudgetLimit", {})
        limit_amount = float(limit.get("Amount", 0))
        limit_unit = limit.get("Unit", "USD")

        calc_spend = b.get("CalculatedSpend", {})
        actual_amount = float(calc_spend.get("ActualSpend", {}).get("Amount", 0))
        forecasted_amount = float(calc_spend.get("ForecastedSpend", {}).get("Amount", 0))

        time_period = b.get("TimePeriod", {})
        start = str(time_period.get("Start", ""))
        end = str(time_period.get("End", ""))

        threshold_pct = 80.0
        notifications = []
        try:
            notif_resp = self._client.describe_notifications_for_budget(
                AccountId=self._account_id,
                BudgetName=b["BudgetName"],
            )
            for n in notif_resp.get("Notifications", []):
                threshold_pct = max(threshold_pct, float(n.get("Threshold", 80.0)))
                notifications.append({
                    "type": n.get("NotificationType"),
                    "threshold": n.get("Threshold"),
                    "comparison": n.get("ComparisonOperator"),
                })
        except Exception:
            pass

        pct_used = (actual_amount / limit_amount * 100) if limit_amount > 0 else 0
        pct_forecasted = (forecasted_amount / limit_amount * 100) if limit_amount > 0 else 0

        return BudgetStatus(
            name=b["BudgetName"],
            budget_type=b.get("BudgetType", "COST"),
            time_unit=b.get("TimeUnit", "MONTHLY"),
            limit_amount=limit_amount,
            limit_unit=limit_unit,
            actual_spend=actual_amount,
            forecasted_spend=forecasted_amount,
            threshold_pct=threshold_pct,
            pct_used=round(pct_used, 1),
            pct_forecasted=round(pct_forecasted, 1),
            start_date=start[:10] if start else "",
            end_date=end[:10] if end else "",
            notifications=notifications,
        )

    def create_monthly_budget(
        self,
        name: str,
        limit_usd: float,
        alert_threshold_pct: float,
        email: Optional[str] = None,
        sns_topic_arn: Optional[str] = None,
        service_filter: Optional[str] = None,
    ) -> bool:
        """Create a monthly cost budget with optional email/SNS alert."""
        now = datetime.utcnow()
        start = date(now.year, now.month, 1).isoformat() + "T00:00:00Z"
        # End of next year to avoid expiry
        end = date(now.year + 2, 1, 1).isoformat() + "T00:00:00Z"

        cost_filters: dict = {}
        if service_filter:
            cost_filters["Service"] = [service_filter]

        budget: dict = {
            "BudgetName": name,
            "BudgetType": "COST",
            "TimeUnit": "MONTHLY",
            "BudgetLimit": {"Amount": str(limit_usd), "Unit": "USD"},
            "TimePeriod": {"Start": start, "End": end},
        }
        if cost_filters:
            budget["CostFilters"] = cost_filters

        notifications_with_subscribers = []
        if email or sns_topic_arn:
            notification: dict = {
                "Notification": {
                    "NotificationType": "FORECASTED",
                    "ComparisonOperator": "GREATER_THAN",
                    "Threshold": alert_threshold_pct,
                    "ThresholdType": "PERCENTAGE",
                },
                "Subscribers": [],
            }
            if email:
                notification["Subscribers"].append({
                    "SubscriptionType": "EMAIL",
                    "Address": email,
                })
            if sns_topic_arn:
                notification["Subscribers"].append({
                    "SubscriptionType": "SNS",
                    "Address": sns_topic_arn,
                })
            notifications_with_subscribers.append(notification)

            # Also alert on actual spend
            actual_notification: dict = {
                "Notification": {
                    "NotificationType": "ACTUAL",
                    "ComparisonOperator": "GREATER_THAN",
                    "Threshold": alert_threshold_pct,
                    "ThresholdType": "PERCENTAGE",
                },
                "Subscribers": notification["Subscribers"][:],
            }
            notifications_with_subscribers.append(actual_notification)

        try:
            self._client.create_budget(
                AccountId=self._account_id,
                Budget=budget,
                NotificationsWithSubscribers=notifications_with_subscribers,
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "DuplicateRecordException":
                raise ValueError(f"Budget '{name}' already exists") from e
            raise

    def get_budget_history(self, budget_name: str, periods: int = 6) -> list[dict]:
        """Return historical spend for a budget over recent billing periods."""
        try:
            resp = self._client.describe_budget_performance_history(
                AccountId=self._account_id,
                BudgetName=budget_name,
                TimePeriod={},
            )
            history = []
            for entry in resp.get("BudgetPerformanceHistory", {}).get("BudgetedAndActualAmountsList", [])[-periods:]:
                tp = entry.get("TimePeriod", {})
                history.append({
                    "period": str(tp.get("Start", ""))[:7],
                    "budgeted": float(entry.get("BudgetedAmount", {}).get("Amount", 0)),
                    "actual": float(entry.get("ActualAmount", {}).get("Amount", 0)),
                })
            return history
        except Exception:
            return []


def _mock_budgets() -> list[BudgetStatus]:
    """Return example data for local development / testing."""
    return [
        BudgetStatus(
            name="monthly-aws-total",
            budget_type="COST",
            time_unit="MONTHLY",
            limit_amount=10000.0,
            limit_unit="USD",
            actual_spend=7823.45,
            forecasted_spend=10200.00,
            threshold_pct=80.0,
            pct_used=78.2,
            pct_forecasted=102.0,
            start_date=date.today().replace(day=1).isoformat(),
            end_date=date.today().replace(day=28).isoformat(),
        ),
        BudgetStatus(
            name="ec2-monthly",
            budget_type="COST",
            time_unit="MONTHLY",
            limit_amount=5000.0,
            limit_unit="USD",
            actual_spend=3120.00,
            forecasted_spend=4200.00,
            threshold_pct=80.0,
            pct_used=62.4,
            pct_forecasted=84.0,
            start_date=date.today().replace(day=1).isoformat(),
            end_date=date.today().replace(day=28).isoformat(),
        ),
        BudgetStatus(
            name="rds-monthly",
            budget_type="COST",
            time_unit="MONTHLY",
            limit_amount=2000.0,
            limit_unit="USD",
            actual_spend=810.00,
            forecasted_spend=1100.00,
            threshold_pct=80.0,
            pct_used=40.5,
            pct_forecasted=55.0,
            start_date=date.today().replace(day=1).isoformat(),
            end_date=date.today().replace(day=28).isoformat(),
        ),
    ]
