"""Terraform remediation generator — produce HCL fixes for detected waste.

Given a list of WasteItems, generates Terraform resource blocks or
data source modifications that right-size or eliminate the waste.
Output is valid HCL that can be applied directly or used as a PR patch.
"""

from __future__ import annotations

from dataclasses import dataclass
from textwrap import dedent

from finops.analyzers.waste import WasteItem


@dataclass
class RemediationBlock:
    resource_id: str
    category: str
    estimated_monthly_savings: float
    hcl: str
    description: str
    risk_level: str  # low | medium | high


def generate_remediation(waste_items: list[WasteItem]) -> list[RemediationBlock]:
    """Generate Terraform HCL remediation blocks for a list of waste items."""
    blocks: list[RemediationBlock] = []
    for item in waste_items:
        block = _dispatch(item)
        if block is not None:
            blocks.append(block)
    return sorted(blocks, key=lambda b: b.estimated_monthly_savings, reverse=True)


def _dispatch(item: WasteItem) -> RemediationBlock | None:
    generators = {
        "compute": _remediate_idle_ec2,
        "storage": _remediate_unattached_ebs,
        "network": _remediate_unassociated_eip,
        "snapshot": _remediate_old_snapshot,
        "database": _remediate_oversized_rds,
    }
    fn = generators.get(item.category)
    if fn is None:
        return None
    return fn(item)


def _remediate_idle_ec2(item: WasteItem) -> RemediationBlock:
    instance_id = item.resource_id
    details = item.details
    current_type = details.get("instance_type", "unknown")
    avg_cpu = details.get("avg_cpu_pct", 0)

    # Recommend smaller instance type based on CPU utilization
    recommended_type = _downsize_instance(current_type, avg_cpu)

    hcl = dedent(f"""\
        # Remediation: Right-size idle EC2 instance {instance_id}
        # Current: {current_type} (avg CPU: {avg_cpu:.1f}%)
        # Recommended: {recommended_type}
        #
        # Option 1: Modify the Terraform resource
        resource "aws_instance" "example" {{
          # ... existing config ...
          instance_type = "{recommended_type}"  # was: {current_type}
        }}

        # Option 2: Schedule stop/start if instance is only needed during business hours
        resource "aws_autoscaling_schedule" "stop_nights" {{
          scheduled_action_name  = "stop-{instance_id}-nights"
          min_size               = 0
          max_size               = 0
          desired_capacity       = 0
          recurrence             = "0 20 * * MON-FRI"  # Stop at 8pm UTC weekdays
          autoscaling_group_name = aws_autoscaling_group.example.name
        }}

        resource "aws_autoscaling_schedule" "start_mornings" {{
          scheduled_action_name  = "start-{instance_id}-mornings"
          min_size               = 1
          max_size               = 1
          desired_capacity       = 1
          recurrence             = "0 8 * * MON-FRI"  # Start at 8am UTC weekdays
          autoscaling_group_name = aws_autoscaling_group.example.name
        }}
    """)

    return RemediationBlock(
        resource_id=instance_id,
        category="compute",
        estimated_monthly_savings=item.estimated_monthly_cost * 0.5,  # ~50% savings from rightsizing
        hcl=hcl,
        description=f"Right-size {instance_id} from {current_type} → {recommended_type} (avg CPU {avg_cpu:.1f}%)",
        risk_level="medium",
    )


def _remediate_unattached_ebs(item: WasteItem) -> RemediationBlock:
    volume_id = item.resource_id
    details = item.details

    hcl = dedent(f"""\
        # Remediation: Remove unattached EBS volume {volume_id}
        # Size: {details.get('size_gb', 'unknown')} GB, Type: {details.get('volume_type', 'unknown')}
        # Created: {details.get('create_time', 'unknown')}
        #
        # Step 1: Snapshot first (recommended)
        resource "aws_ebs_snapshot" "backup_{volume_id.replace('-', '_')}" {{
          volume_id   = "{volume_id}"
          description = "Final snapshot before deletion — created by finops-intelligence"

          tags = {{
            Name        = "pre-deletion-backup"
            volume-id   = "{volume_id}"
            auto-delete = "true"
          }}
        }}

        # Step 2: Remove the volume from Terraform state and delete
        # Run: terraform state rm aws_ebs_volume.{volume_id.replace('-', '_')}
        # Then: aws ec2 delete-volume --volume-id {volume_id}
        #
        # Or if managed by Terraform:
        # removed {{
        #   from = aws_ebs_volume.example
        #   lifecycle {{ destroy = true }}
        # }}
    """)

    return RemediationBlock(
        resource_id=volume_id,
        category="storage",
        estimated_monthly_savings=item.estimated_monthly_cost,
        hcl=hcl,
        description=f"Delete unattached EBS volume {volume_id} ({details.get('size_gb', '?')}GB {details.get('volume_type', '')})",
        risk_level="low",
    )


def _remediate_unassociated_eip(item: WasteItem) -> RemediationBlock:
    eip_id = item.resource_id

    hcl = dedent(f"""\
        # Remediation: Release unassociated Elastic IP {eip_id}
        # Cost: ~$3.60/month while unassociated
        #
        # If not managed by Terraform:
        #   aws ec2 release-address --allocation-id {eip_id}
        #
        # If managed by Terraform, remove the resource:
        # removed {{
        #   from = aws_eip.example
        #   lifecycle {{ destroy = true }}
        # }}
        #
        # If the EIP is needed but the instance was terminated, re-associate it:
        resource "aws_eip_association" "example" {{
          instance_id   = aws_instance.replacement.id
          allocation_id = "{eip_id}"
        }}
    """)

    return RemediationBlock(
        resource_id=eip_id,
        category="network",
        estimated_monthly_savings=item.estimated_monthly_cost,
        hcl=hcl,
        description=f"Release unassociated Elastic IP {eip_id}",
        risk_level="low",
    )


def _remediate_old_snapshot(item: WasteItem) -> RemediationBlock:
    snapshot_id = item.resource_id
    details = item.details

    hcl = dedent(f"""\
        # Remediation: Delete old EBS snapshot {snapshot_id}
        # Age: {details.get('age_days', 'unknown')} days
        # Volume: {details.get('volume_id', 'unknown')}
        #
        # Implement snapshot lifecycle policy to automate this going forward:
        resource "aws_dlm_lifecycle_policy" "ebs_snapshots" {{
          description        = "EBS snapshot lifecycle — retain 30 days"
          execution_role_arn = aws_iam_role.dlm_lifecycle.arn
          state              = "ENABLED"

          policy_details {{
            resource_types = ["VOLUME"]

            schedule {{
              name = "Daily snapshots — 30 day retention"

              create_rule {{
                interval      = 24
                interval_unit = "HOURS"
                times         = ["03:00"]
              }}

              retain_rule {{
                count = 30
              }}

              tags_to_add = {{
                SnapshotCreator = "DLM"
              }}

              copy_tags = true
            }}

            target_tags = {{
              Snapshot = "true"
            }}
          }}
        }}

        # Delete the old snapshot:
        # aws ec2 delete-snapshot --snapshot-id {snapshot_id}
    """)

    return RemediationBlock(
        resource_id=snapshot_id,
        category="snapshot",
        estimated_monthly_savings=item.estimated_monthly_cost,
        hcl=hcl,
        description=f"Delete {details.get('age_days', '?')}-day-old snapshot {snapshot_id} and add DLM policy",
        risk_level="low",
    )


def _remediate_oversized_rds(item: WasteItem) -> RemediationBlock:
    db_id = item.resource_id
    details = item.details
    current_class = details.get("instance_class", "db.unknown")
    avg_cpu = details.get("avg_cpu_pct", 0)
    recommended_class = _downsize_db_instance(current_class, avg_cpu)

    hcl = dedent(f"""\
        # Remediation: Right-size oversized RDS instance {db_id}
        # Current: {current_class} (avg CPU: {avg_cpu:.1f}%)
        # Recommended: {recommended_class}
        #
        # Note: Requires a maintenance window; brief restart expected.
        resource "aws_db_instance" "example" {{
          # ... existing config ...
          instance_class = "{recommended_class}"  # was: {current_class}

          # Optional: Enable auto minor version upgrade to reduce maintenance burden
          auto_minor_version_upgrade = true

          # Optional: Convert to Aurora Serverless v2 for variable workloads
          # (requires migrating to aws_rds_cluster resource)
        }}

        # Estimated monthly savings: ~${item.estimated_monthly_cost * 0.4:.0f}/month
    """)

    return RemediationBlock(
        resource_id=db_id,
        category="database",
        estimated_monthly_savings=item.estimated_monthly_cost * 0.4,
        hcl=hcl,
        description=f"Right-size {db_id} from {current_class} → {recommended_class} (avg CPU {avg_cpu:.1f}%)",
        risk_level="high",  # DB resizes need careful testing
    )


def format_remediation_report(blocks: list[RemediationBlock]) -> str:
    """Format remediation blocks as a Markdown report with embedded HCL."""
    if not blocks:
        return "## Terraform Remediation\n\nNo automated remediations available.\n"

    total_savings = sum(b.estimated_monthly_savings for b in blocks)
    lines = [
        "## Terraform Waste Remediation",
        "",
        f"**Estimated monthly savings if all applied:** ${total_savings:,.2f}",
        "",
        "| Resource | Category | Savings/mo | Risk |",
        "|---|---|---|---|",
    ]
    for b in blocks:
        lines.append(
            f"| `{b.resource_id}` | {b.category} | ${b.estimated_monthly_savings:,.2f} | {b.risk_level} |"
        )
    lines.append("")

    for b in blocks:
        lines.extend([
            f"### {b.description}",
            "",
            f"**Risk level:** {b.risk_level}  |  **Est. savings:** ${b.estimated_monthly_savings:,.2f}/mo",
            "",
            "```hcl",
            b.hcl.rstrip(),
            "```",
            "",
        ])

    return "\n".join(lines)


# ─── Instance sizing helpers ──────────────────────────────────────────────────

_EC2_DOWNSIZE_MAP: dict[str, str] = {
    "m5.2xlarge": "m5.xlarge", "m5.xlarge": "m5.large", "m5.large": "t3.large",
    "m6i.2xlarge": "m6i.xlarge", "m6i.xlarge": "m6i.large", "m6i.large": "t3.large",
    "c5.2xlarge": "c5.xlarge", "c5.xlarge": "c5.large",
    "r5.2xlarge": "r5.xlarge", "r5.xlarge": "r5.large",
    "t3.2xlarge": "t3.xlarge", "t3.xlarge": "t3.large", "t3.large": "t3.medium",
    "t3.medium": "t3.small", "t3.small": "t3.micro",
}

_RDS_DOWNSIZE_MAP: dict[str, str] = {
    "db.m5.2xlarge": "db.m5.xlarge", "db.m5.xlarge": "db.m5.large",
    "db.m5.large": "db.t3.large", "db.m6g.xlarge": "db.m6g.large",
    "db.r5.2xlarge": "db.r5.xlarge", "db.r5.xlarge": "db.r5.large",
    "db.t3.large": "db.t3.medium", "db.t3.medium": "db.t3.small",
}


def _downsize_instance(current: str, avg_cpu: float) -> str:
    if avg_cpu < 2.0:
        # Very idle — try two steps down
        first = _EC2_DOWNSIZE_MAP.get(current, current)
        return _EC2_DOWNSIZE_MAP.get(first, first)
    return _EC2_DOWNSIZE_MAP.get(current, current)


def _downsize_db_instance(current: str, avg_cpu: float) -> str:
    if avg_cpu < 5.0:
        first = _RDS_DOWNSIZE_MAP.get(current, current)
        return _RDS_DOWNSIZE_MAP.get(first, first)
    return _RDS_DOWNSIZE_MAP.get(current, current)
