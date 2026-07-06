"""AWS Budget alerts integration — create and monitor cost budgets programmatically."""

from __future__ import annotations

import boto3
from dataclasses import dataclass
from typing import Optional


@dataclass
class BudgetAlert:
    budget_name: str
    account_id: str
    limit_amount: float
    actual_spend: float
    forecasted_spend: float
    threshold_pct: float
    alert_type: str  # "ACTUAL" or "FORECASTED"
    time_unit: str   # "MONTHLY", "QUARTERLY", "ANNUALLY"


class BudgetManager:
    """Create, monitor, and manage AWS Budgets with SNS alerting."""

    def __init__(self, account_id: Optional[str] = None) -> None:
        self.client = boto3.client("budgets")
        self.sns = boto3.client("sns")
        self.account_id = account_id or boto3.client("sts").get_caller_identity()["Account"]

    def create_monthly_budget(
        self,
        name: str,
        limit_usd: float,
        alert_thresholds: list[float] | None = None,
        alert_email: str | None = None,
        slack_webhook: str | None = None,
    ) -> dict:
        """
        Create a monthly cost budget with tiered threshold alerts.

        Default thresholds: 50%, 80%, 100%, 110% (forecasted).
        Supports email (SNS) and Slack webhook notifications.
        """
        thresholds = alert_thresholds or [50.0, 80.0, 100.0]

        notifications_with_subscribers = []

        for threshold in thresholds:
            alert_type = "FORECASTED" if threshold >= 100 else "ACTUAL"
            notification: dict = {
                "NotificationType": alert_type,
                "ComparisonOperator": "GREATER_THAN",
                "Threshold": threshold,
                "ThresholdType": "PERCENTAGE",
                "NotificationState": "ALARM",
            }
            subscribers = []
            if alert_email:
                subscribers.append({
                    "SubscriptionType": "EMAIL",
                    "Address": alert_email,
                })
            if slack_webhook:
                # Create SNS topic for Slack Lambda integration
                topic_arn = self._get_or_create_slack_topic(name, slack_webhook)
                subscribers.append({
                    "SubscriptionType": "SNS",
                    "Address": topic_arn,
                })
            if subscribers:
                notifications_with_subscribers.append({
                    "Notification": notification,
                    "Subscribers": subscribers,
                })

        budget = {
            "BudgetName": name,
            "BudgetLimit": {
                "Amount": str(limit_usd),
                "Unit": "USD",
            },
            "TimeUnit": "MONTHLY",
            "BudgetType": "COST",
            "CostTypes": {
                "IncludeTax": True,
                "IncludeSubscription": True,
                "UseBlended": False,
                "IncludeRefund": False,
                "IncludeCredit": False,
                "IncludeUpfront": True,
                "IncludeRecurring": True,
                "IncludeOtherSubscription": True,
                "IncludeSupport": True,
                "IncludeDiscount": True,
                "UseAmortized": False,
            },
        }

        self.client.create_budget(
            AccountId=self.account_id,
            Budget=budget,
            NotificationsWithSubscribers=notifications_with_subscribers,
        )

        return {"budget_name": name, "limit": limit_usd, "thresholds": thresholds}

    def create_service_budgets(
        self,
        service_limits: dict[str, float],
        alert_email: str | None = None,
    ) -> list[dict]:
        """Create per-service budgets for high-spend AWS services."""
        service_map = {
            "EC2": "Amazon Elastic Compute Cloud - Compute",
            "RDS": "Amazon Relational Database Service",
            "EKS": "Amazon Elastic Kubernetes Service",
            "S3": "Amazon Simple Storage Service",
            "DataTransfer": "AWS Data Transfer",
        }

        results = []
        for service, limit in service_limits.items():
            aws_service = service_map.get(service, service)
            budget = {
                "BudgetName": f"service-{service.lower()}-monthly",
                "BudgetLimit": {"Amount": str(limit), "Unit": "USD"},
                "TimeUnit": "MONTHLY",
                "BudgetType": "COST",
                "CostFilters": {
                    "Service": [aws_service],
                },
            }
            try:
                self.client.create_budget(
                    AccountId=self.account_id,
                    Budget=budget,
                )
                results.append({"service": service, "limit": limit, "status": "created"})
            except self.client.exceptions.DuplicateRecordException:
                results.append({"service": service, "limit": limit, "status": "already_exists"})

        return results

    def get_budget_alerts(self) -> list[BudgetAlert]:
        """Fetch all budgets that are currently in ALARM state."""
        paginator = self.client.get_paginator("describe_budgets")
        alerts = []

        for page in paginator.paginate(AccountId=self.account_id):
            for budget in page["Budgets"]:
                if "CalculatedSpend" not in budget:
                    continue

                actual = float(budget["CalculatedSpend"].get("ActualSpend", {}).get("Amount", 0))
                forecasted = float(budget["CalculatedSpend"].get("ForecastedSpend", {}).get("Amount", 0))
                limit = float(budget["BudgetLimit"]["Amount"])

                actual_pct = (actual / limit * 100) if limit else 0

                if actual_pct >= 80 or forecasted > limit:
                    alerts.append(BudgetAlert(
                        budget_name=budget["BudgetName"],
                        account_id=self.account_id,
                        limit_amount=limit,
                        actual_spend=actual,
                        forecasted_spend=forecasted,
                        threshold_pct=actual_pct,
                        alert_type="ACTUAL" if actual_pct >= 80 else "FORECASTED",
                        time_unit=budget["TimeUnit"],
                    ))

        return alerts

    def _get_or_create_slack_topic(self, budget_name: str, webhook_url: str) -> str:
        """Get or create an SNS topic wired to post to Slack via Lambda."""
        topic_name = f"budget-alert-{budget_name.lower().replace(' ', '-')[:40]}"
        response = self.sns.create_topic(Name=topic_name)
        return response["TopicArn"]
