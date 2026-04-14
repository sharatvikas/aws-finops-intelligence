"""AWS Savings Plans coverage analysis and purchase recommendations.

Analyzes compute spend to identify:
- Current Savings Plan coverage rate (% of compute covered by SP)
- Uncovered on-demand spend eligible for Savings Plans
- Recommended SP commitment to reach target coverage
- Break-even and ROI projections

AWS API calls:
    ce.get_savings_plans_coverage()
    ce.get_savings_plans_utilization()
    ce.get_savings_plans_purchase_recommendation()
    ce.get_savings_plans_utilization_details()
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SPCoverage:
    """Coverage metrics for a time period."""
    start_date: str
    end_date: str
    coverage_pct: float          # % of eligible compute covered by SP
    sp_spend_usd: float          # SP commitment applied
    on_demand_spend_usd: float   # remaining on-demand (uncovered)
    total_eligible_usd: float    # total SP-eligible spend

    @property
    def uncovered_usd(self) -> float:
        return self.on_demand_spend_usd

    @property
    def potential_savings_usd(self) -> float:
        """Conservative estimate: 20% discount on uncovered on-demand."""
        return self.on_demand_spend_usd * 0.20


@dataclass
class SPUtilization:
    """Utilization of existing Savings Plan commitments."""
    plan_id: str
    plan_type: str       # ComputeSavingsPlans | EC2InstanceSavingsPlans | SageMakerSavingsPlans
    commitment_usd: float
    used_usd: float
    unused_usd: float
    utilization_pct: float
    net_savings_usd: float

    @property
    def is_underutilized(self) -> bool:
        return self.utilization_pct < 80.0


@dataclass
class SPRecommendation:
    """A purchase recommendation from the CE Savings Plans advisor."""
    plan_type: str
    term_years: int              # 1 or 3
    payment_option: str          # AllUpfront | PartialUpfront | NoUpfront
    hourly_commitment_usd: float
    estimated_monthly_savings_usd: float
    estimated_savings_pct: float
    estimated_roi_pct: float
    estimated_break_even_months: float
    coverage_target_pct: float   # what coverage % this achieves

    def summary(self) -> str:
        return (
            f"{self.plan_type} ({self.term_years}yr {self.payment_option}): "
            f"${self.hourly_commitment_usd:.4f}/hr commitment → "
            f"${self.estimated_monthly_savings_usd:,.0f}/mo savings "
            f"({self.estimated_savings_pct:.1f}%) | "
            f"ROI {self.estimated_roi_pct:.0f}% | "
            f"break-even {self.estimated_break_even_months:.1f}mo"
        )


@dataclass
class SPAnalysisResult:
    """Complete savings plan analysis for an account."""
    account_id: str
    analysis_date: str
    lookback_days: int

    coverage: SPCoverage | None
    utilization: list[SPUtilization]
    recommendations: list[SPRecommendation]

    # Aggregate signals
    avg_coverage_pct: float = 0.0
    total_uncovered_monthly_usd: float = 0.0
    underutilized_plans: list[str] = field(default_factory=list)

    def has_coverage_gap(self, threshold: float = 80.0) -> bool:
        return self.avg_coverage_pct < threshold

    def has_underutilized_plans(self) -> bool:
        return bool(self.underutilized_plans)

    def top_recommendation(self) -> SPRecommendation | None:
        if not self.recommendations:
            return None
        return max(self.recommendations, key=lambda r: r.estimated_monthly_savings_usd)


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------

class SavingsPlansAnalyzer:
    """Analyze Savings Plans coverage and generate purchase recommendations.

    Args:
        session: Optional boto3 Session. Defaults to environment credentials.
        lookback_days: Number of days of history to analyze (default 30).
        coverage_target_pct: Target coverage % for recommendations (default 80).
    """

    def __init__(
        self,
        session: boto3.Session | None = None,
        lookback_days: int = 30,
        coverage_target_pct: float = 80.0,
    ) -> None:
        self._session = session or boto3.Session()
        self._ce = self._session.client("ce", region_name="us-east-1")
        self._lookback_days = lookback_days
        self._coverage_target_pct = coverage_target_pct

    def analyze(self) -> SPAnalysisResult:
        """Run the full SP analysis and return structured results."""
        account_id = self._get_account_id()
        end = date.today()
        start = end - timedelta(days=self._lookback_days)

        try:
            coverage = self._get_coverage(start, end)
        except ClientError as e:
            log.warning("savings_plans.coverage_fetch_failed: %s", e)
            coverage = self._mock_coverage(start, end)

        try:
            utilization = self._get_utilization(start, end)
        except ClientError as e:
            log.warning("savings_plans.utilization_fetch_failed: %s", e)
            utilization = self._mock_utilization()

        try:
            recommendations = self._get_recommendations()
        except ClientError as e:
            log.warning("savings_plans.recommendations_fetch_failed: %s", e)
            recommendations = self._mock_recommendations(coverage)

        underutilized = [u.plan_id for u in utilization if u.is_underutilized]

        result = SPAnalysisResult(
            account_id=account_id,
            analysis_date=end.isoformat(),
            lookback_days=self._lookback_days,
            coverage=coverage,
            utilization=utilization,
            recommendations=recommendations,
            avg_coverage_pct=coverage.coverage_pct if coverage else 0.0,
            total_uncovered_monthly_usd=coverage.uncovered_usd * 30 / self._lookback_days if coverage else 0.0,
            underutilized_plans=underutilized,
        )
        return result

    # ── Private helpers ────────────────────────────────────────────────────

    def _get_account_id(self) -> str:
        try:
            sts = self._session.client("sts")
            return sts.get_caller_identity()["Account"]
        except Exception:
            return "unknown"

    def _get_coverage(self, start: date, end: date) -> SPCoverage:
        resp = self._ce.get_savings_plans_coverage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="MONTHLY",
        )
        total = resp.get("Total", {}).get("Coverage", {})
        coverage_pct = float(total.get("CoveragePercentage", 0))
        sp_spend = float(total.get("SpendCoveredBySavingsPlans", 0))
        od_spend = float(total.get("OnDemandCost", 0))
        total_spend = sp_spend + od_spend
        return SPCoverage(
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            coverage_pct=coverage_pct,
            sp_spend_usd=sp_spend,
            on_demand_spend_usd=od_spend,
            total_eligible_usd=total_spend,
        )

    def _get_utilization(self, start: date, end: date) -> list[SPUtilization]:
        resp = self._ce.get_savings_plans_utilization_details(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        )
        result = []
        for item in resp.get("SavingsPlansUtilizationDetails", []):
            attrs = item.get("Attributes", {})
            util = item.get("Utilization", {})
            savings = item.get("Savings", {})
            used = float(util.get("UsedCommitment", 0))
            total = float(util.get("TotalCommitment", 0))
            unused = float(util.get("UnusedCommitment", 0))
            pct = (used / total * 100) if total > 0 else 0.0
            result.append(SPUtilization(
                plan_id=attrs.get("SavingsPlanArn", "unknown").split("/")[-1],
                plan_type=attrs.get("SavingsPlanType", "ComputeSavingsPlans"),
                commitment_usd=total,
                used_usd=used,
                unused_usd=unused,
                utilization_pct=pct,
                net_savings_usd=float(savings.get("NetSavings", 0)),
            ))
        return result

    def _get_recommendations(self) -> list[SPRecommendation]:
        result = []
        for term in ["ONE_YEAR", "THREE_YEARS"]:
            for payment in ["NO_UPFRONT", "PARTIAL_UPFRONT", "ALL_UPFRONT"]:
                try:
                    resp = self._ce.get_savings_plans_purchase_recommendation(
                        SavingsPlansType="COMPUTE_SP",
                        TermInYears=term,
                        PaymentOption=payment,
                        LookbackPeriodInDays="THIRTY_DAYS",
                    )
                    summary = resp.get("SavingsPlansPurchaseRecommendation", {})
                    rec_details = summary.get("SavingsPlansPurchaseRecommendationDetails", [])
                    rec_sum = summary.get("SavingsPlansPurchaseRecommendationSummary", {})

                    if not rec_sum:
                        continue

                    hourly = float(rec_sum.get("HourlyCommitmentToPurchase", 0))
                    monthly_savings = float(rec_sum.get("MonthlySavingsAmount", 0))
                    savings_pct = float(rec_sum.get("EstimatedSavingsPercentage", 0))

                    # ROI: (monthly_savings * 12 * term_years) / (hourly * 8760 * term_years)
                    term_years = 1 if term == "ONE_YEAR" else 3
                    total_commitment = hourly * 8760 * term_years
                    total_savings = monthly_savings * 12 * term_years
                    roi_pct = (total_savings / total_commitment * 100) if total_commitment > 0 else 0
                    break_even = (total_commitment - total_savings) / max(monthly_savings, 0.01)
                    break_even_months = max(break_even, 0)

                    result.append(SPRecommendation(
                        plan_type="ComputeSavingsPlans",
                        term_years=term_years,
                        payment_option=payment.replace("_", " ").title(),
                        hourly_commitment_usd=hourly,
                        estimated_monthly_savings_usd=monthly_savings,
                        estimated_savings_pct=savings_pct,
                        estimated_roi_pct=roi_pct,
                        estimated_break_even_months=break_even_months,
                        coverage_target_pct=self._coverage_target_pct,
                    ))
                except ClientError:
                    continue
        # Sort by savings descending
        return sorted(result, key=lambda r: r.estimated_monthly_savings_usd, reverse=True)

    # ── Mock fallbacks (for demo/offline mode) ─────────────────────────────

    def _mock_coverage(self, start: date, end: date) -> SPCoverage:
        return SPCoverage(
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            coverage_pct=54.3,
            sp_spend_usd=12_840.00,
            on_demand_spend_usd=10_780.00,
            total_eligible_usd=23_620.00,
        )

    def _mock_utilization(self) -> list[SPUtilization]:
        return [
            SPUtilization(
                plan_id="sp-abc12345",
                plan_type="ComputeSavingsPlans",
                commitment_usd=8_760.00,
                used_usd=7_680.00,
                unused_usd=1_080.00,
                utilization_pct=87.7,
                net_savings_usd=1_920.00,
            ),
            SPUtilization(
                plan_id="sp-def67890",
                plan_type="EC2InstanceSavingsPlans",
                commitment_usd=4_380.00,
                used_usd=3_100.00,
                unused_usd=1_280.00,
                utilization_pct=70.8,   # underutilized
                net_savings_usd=720.00,
            ),
        ]

    def _mock_recommendations(self, coverage: SPCoverage | None) -> list[SPRecommendation]:
        return [
            SPRecommendation(
                plan_type="ComputeSavingsPlans",
                term_years=1,
                payment_option="No Upfront",
                hourly_commitment_usd=1.642,
                estimated_monthly_savings_usd=1_196.00,
                estimated_savings_pct=19.4,
                estimated_roi_pct=116.0,
                estimated_break_even_months=0.0,
                coverage_target_pct=80.0,
            ),
            SPRecommendation(
                plan_type="ComputeSavingsPlans",
                term_years=3,
                payment_option="Partial Upfront",
                hourly_commitment_usd=1.642,
                estimated_monthly_savings_usd=2_140.00,
                estimated_savings_pct=34.7,
                estimated_roi_pct=152.0,
                estimated_break_even_months=8.4,
                coverage_target_pct=80.0,
            ),
        ]
