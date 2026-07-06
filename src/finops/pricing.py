"""Static on-demand pricing estimates (us-east-1, Linux) for savings math.

These are deliberately approximate: rightsizing and waste findings only need
directionally-correct monthly dollar figures to rank recommendations. For
billing-grade numbers, use the AWS Pricing API or CUR data.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

HOURS_PER_MONTH = 730

# Hourly on-demand price (USD) per instance type, us-east-1 Linux.
EC2_HOURLY_USD: dict[str, float] = {
    # t3 (burstable)
    "t3.nano": 0.0052, "t3.micro": 0.0104, "t3.small": 0.0208,
    "t3.medium": 0.0416, "t3.large": 0.0832, "t3.xlarge": 0.1664, "t3.2xlarge": 0.3328,
    # m5 (general purpose)
    "m5.large": 0.096, "m5.xlarge": 0.192, "m5.2xlarge": 0.384,
    "m5.4xlarge": 0.768, "m5.8xlarge": 1.536, "m5.12xlarge": 2.304,
    "m5.16xlarge": 3.072, "m5.24xlarge": 4.608,
    # m6i
    "m6i.large": 0.096, "m6i.xlarge": 0.192, "m6i.2xlarge": 0.384,
    "m6i.4xlarge": 0.768, "m6i.8xlarge": 1.536,
    # c5 (compute optimized)
    "c5.large": 0.085, "c5.xlarge": 0.17, "c5.2xlarge": 0.34,
    "c5.4xlarge": 0.68, "c5.9xlarge": 1.53, "c5.12xlarge": 2.04,
    "c5.18xlarge": 3.06, "c5.24xlarge": 4.08,
    # c6i
    "c6i.large": 0.085, "c6i.xlarge": 0.17, "c6i.2xlarge": 0.34,
    "c6i.4xlarge": 0.68, "c6i.8xlarge": 1.36,
    # r5 (memory optimized)
    "r5.large": 0.126, "r5.xlarge": 0.252, "r5.2xlarge": 0.504,
    "r5.4xlarge": 1.008, "r5.8xlarge": 2.016, "r5.12xlarge": 3.024,
    # r6i
    "r6i.large": 0.126, "r6i.xlarge": 0.252, "r6i.2xlarge": 0.504,
    "r6i.4xlarge": 1.008,
}

# Instance size ladder within a family, smallest to largest.
SIZE_LADDER = [
    "nano", "micro", "small", "medium", "large",
    "xlarge", "2xlarge", "4xlarge", "8xlarge", "9xlarge",
    "12xlarge", "16xlarge", "18xlarge", "24xlarge",
]


def ec2_monthly_cost(instance_type: str) -> float | None:
    """Estimated monthly on-demand cost, or None if the type is unknown."""
    hourly = EC2_HOURLY_USD.get(instance_type)
    if hourly is None:
        logger.debug("No pricing data for instance type %s", instance_type)
        return None
    return hourly * HOURS_PER_MONTH


def split_instance_type(instance_type: str) -> tuple[str, str] | None:
    """Split 'c5.2xlarge' -> ('c5', '2xlarge'). None if malformed."""
    parts = instance_type.split(".", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


def downsize_instance_type(instance_type: str, steps: int = 1) -> str | None:
    """Return the instance type `steps` sizes smaller in the same family.

    Only returns a type we have pricing for (so the savings math is real).
    Returns None if already at the smallest priced size or the type is unknown.
    """
    split = split_instance_type(instance_type)
    if split is None:
        return None
    family, size = split
    if size not in SIZE_LADDER:
        return None

    idx = SIZE_LADDER.index(size)
    while steps > 0 and idx > 0:
        idx -= 1
        candidate = f"{family}.{SIZE_LADDER[idx]}"
        if candidate in EC2_HOURLY_USD:
            steps -= 1
            if steps == 0:
                return candidate
    return None
