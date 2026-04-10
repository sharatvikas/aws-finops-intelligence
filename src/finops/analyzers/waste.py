"""Waste detection — find idle EC2s, unattached EBS, oversized RDS, orphaned resources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any

import boto3


@dataclass
class WasteItem:
    category: str
    resource_id: str
    resource_type: str
    region: str
    estimated_monthly_cost: float
    reason: str
    remediation: str
    details: dict[str, Any]


class WasteDetector:
    """Identify wasted AWS resources across an account."""

    def __init__(self, session: boto3.Session, region: str = "us-east-1") -> None:
        self._session = session
        self._region = region
        self._ec2 = session.client("ec2", region_name=region)
        self._cw = session.client("cloudwatch", region_name=region)
        self._rds = session.client("rds", region_name=region)

    def detect_all(self) -> list[WasteItem]:
        """Run all waste detection checks."""
        results = []
        results.extend(self.find_idle_ec2_instances())
        results.extend(self.find_unattached_ebs_volumes())
        results.extend(self.find_unassociated_elastic_ips())
        results.extend(self.find_old_ebs_snapshots())
        results.extend(self.find_oversized_rds())
        return sorted(results, key=lambda w: w.estimated_monthly_cost, reverse=True)

    def find_idle_ec2_instances(self, idle_threshold_pct: float = 5.0, idle_days: int = 7) -> list[WasteItem]:
        """Find EC2 instances with < threshold% average CPU over idle_days."""
        items = []
        paginator = self._ec2.get_paginator("describe_instances")

        for page in paginator.paginate(
            Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
        ):
            for reservation in page["Reservations"]:
                for instance in reservation["Instances"]:
                    iid = instance["InstanceId"]
                    itype = instance["InstanceType"]

                    avg_cpu = self._get_avg_cpu(iid, days=idle_days)
                    if avg_cpu is None or avg_cpu > idle_threshold_pct:
                        continue

                    monthly_cost = _estimate_ec2_cost(itype)
                    name = _get_tag(instance.get("Tags", []), "Name") or iid

                    items.append(WasteItem(
                        category="Idle EC2",
                        resource_id=iid,
                        resource_type=f"aws_instance ({itype})",
                        region=self._region,
                        estimated_monthly_cost=monthly_cost,
                        reason=f"Average CPU {avg_cpu:.1f}% over last {idle_days} days",
                        remediation=f"Stop or terminate {iid}. If needed, convert to Savings Plan.",
                        details={"name": name, "instance_type": itype, "avg_cpu": avg_cpu},
                    ))

        return items

    def find_unattached_ebs_volumes(self) -> list[WasteItem]:
        """Find EBS volumes not attached to any instance."""
        items = []
        paginator = self._ec2.get_paginator("describe_volumes")

        for page in paginator.paginate(
            Filters=[{"Name": "status", "Values": ["available"]}]
        ):
            for vol in page["Volumes"]:
                vid = vol["VolumeId"]
                size_gb = vol["Size"]
                vol_type = vol["VolumeType"]
                created = vol["CreateTime"]
                age_days = (datetime.now(timezone.utc) - created.replace(tzinfo=timezone.utc)).days

                # Estimate cost: gp3 ~$0.08/GB/month, io1 ~$0.125/GB/month
                cost_per_gb = 0.08 if vol_type in ("gp2", "gp3") else 0.125
                monthly_cost = size_gb * cost_per_gb

                items.append(WasteItem(
                    category="Unattached EBS",
                    resource_id=vid,
                    resource_type=f"aws_ebs_volume ({vol_type}, {size_gb}GB)",
                    region=self._region,
                    estimated_monthly_cost=monthly_cost,
                    reason=f"Volume unattached for {age_days} days",
                    remediation=f"Delete volume {vid} after verifying no data is needed. Create a snapshot first if unsure.",
                    details={"size_gb": size_gb, "vol_type": vol_type, "age_days": age_days},
                ))

        return items

    def find_unassociated_elastic_ips(self) -> list[WasteItem]:
        """Find Elastic IPs not associated with any resource."""
        items = []
        resp = self._ec2.describe_addresses()

        for addr in resp["Addresses"]:
            if addr.get("AssociationId"):
                continue  # In use

            allocation_id = addr.get("AllocationId", "unknown")
            public_ip = addr.get("PublicIp", "")

            items.append(WasteItem(
                category="Unassociated Elastic IP",
                resource_id=allocation_id,
                resource_type="aws_eip",
                region=self._region,
                estimated_monthly_cost=3.65,  # ~$0.005/hr for unassociated EIP
                reason=f"EIP {public_ip} not associated with any instance or NAT Gateway",
                remediation=f"Release EIP {allocation_id} if not needed: aws ec2 release-address --allocation-id {allocation_id}",
                details={"public_ip": public_ip},
            ))

        return items

    def find_old_ebs_snapshots(self, age_days: int = 90) -> list[WasteItem]:
        """Find EBS snapshots older than age_days with no lifecycle policy indicator."""
        items = []
        cutoff = datetime.now(timezone.utc) - timedelta(days=age_days)

        paginator = self._ec2.get_paginator("describe_snapshots")
        for page in paginator.paginate(OwnerIds=["self"]):
            for snap in page["Snapshots"]:
                start = snap["StartTime"].replace(tzinfo=timezone.utc)
                if start > cutoff:
                    continue

                snap_id = snap["SnapshotId"]
                size_gb = snap["VolumeSize"]
                age = (datetime.now(timezone.utc) - start).days
                monthly_cost = size_gb * 0.05  # $0.05/GB/month for snapshots

                items.append(WasteItem(
                    category="Old EBS Snapshot",
                    resource_id=snap_id,
                    resource_type="aws_ebs_snapshot",
                    region=self._region,
                    estimated_monthly_cost=monthly_cost,
                    reason=f"Snapshot is {age} days old ({size_gb}GB)",
                    remediation=f"Delete snapshot {snap_id} if not needed for compliance/DR. Set up DLM lifecycle policy to automate.",
                    details={"size_gb": size_gb, "age_days": age, "description": snap.get("Description", "")},
                ))

        return items

    def find_oversized_rds(self, cpu_threshold: float = 20.0) -> list[WasteItem]:
        """Find RDS instances with very low CPU utilization."""
        items = []
        paginator = self._rds.get_paginator("describe_db_instances")

        for page in paginator.paginate():
            for db in page["DBInstances"]:
                if db["DBInstanceStatus"] != "available":
                    continue

                db_id = db["DBInstanceIdentifier"]
                db_class = db["DBInstanceClass"]
                avg_cpu = self._get_rds_avg_cpu(db_id, days=7)

                if avg_cpu is None or avg_cpu > cpu_threshold:
                    continue

                monthly_cost = _estimate_rds_cost(db_class, db.get("MultiAZ", False))
                next_class = _suggest_smaller_rds(db_class)
                savings = monthly_cost - _estimate_rds_cost(next_class, db.get("MultiAZ", False)) if next_class else 0

                items.append(WasteItem(
                    category="Oversized RDS",
                    resource_id=db_id,
                    resource_type=f"aws_db_instance ({db_class})",
                    region=self._region,
                    estimated_monthly_cost=savings,
                    reason=f"Average CPU {avg_cpu:.1f}% over 7 days — class {db_class} is oversized",
                    remediation=f"Modify to {next_class}: aws rds modify-db-instance --db-instance-identifier {db_id} --db-instance-class {next_class} --apply-immediately",
                    details={"db_class": db_class, "avg_cpu": avg_cpu, "suggested_class": next_class, "multi_az": db.get("MultiAZ")},
                ))

        return items

    def _get_avg_cpu(self, instance_id: str, days: int = 7) -> float | None:
        """Get average EC2 CPU utilization via CloudWatch."""
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)

        resp = self._cw.get_metric_statistics(
            Namespace="AWS/EC2",
            MetricName="CPUUtilization",
            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
            StartTime=start,
            EndTime=end,
            Period=86400,  # Daily average
            Statistics=["Average"],
        )
        points = resp.get("Datapoints", [])
        if not points:
            return None
        return sum(p["Average"] for p in points) / len(points)

    def _get_rds_avg_cpu(self, db_id: str, days: int = 7) -> float | None:
        """Get average RDS CPU utilization."""
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)

        resp = self._cw.get_metric_statistics(
            Namespace="AWS/RDS",
            MetricName="CPUUtilization",
            Dimensions=[{"Name": "DBInstanceIdentifier", "Value": db_id}],
            StartTime=start,
            EndTime=end,
            Period=86400,
            Statistics=["Average"],
        )
        points = resp.get("Datapoints", [])
        if not points:
            return None
        return sum(p["Average"] for p in points) / len(points)


def _get_tag(tags: list, key: str) -> str | None:
    for t in tags:
        if t["Key"] == key:
            return t["Value"]
    return None


def _estimate_ec2_cost(instance_type: str) -> float:
    """Very rough monthly cost estimates for common instance types."""
    costs = {
        "t3.micro": 8, "t3.small": 15, "t3.medium": 30, "t3.large": 61,
        "t3.xlarge": 122, "t3.2xlarge": 244,
        "m5.large": 70, "m5.xlarge": 140, "m5.2xlarge": 281, "m5.4xlarge": 561,
        "c5.large": 62, "c5.xlarge": 124, "c5.2xlarge": 248, "c5.4xlarge": 496,
        "r5.large": 91, "r5.xlarge": 182, "r5.2xlarge": 364,
    }
    return costs.get(instance_type, 100)


def _estimate_rds_cost(db_class: str, multi_az: bool = False) -> float:
    """Rough monthly RDS cost estimates."""
    costs = {
        "db.t3.micro": 15, "db.t3.small": 29, "db.t3.medium": 58,
        "db.m5.large": 140, "db.m5.xlarge": 280, "db.m5.2xlarge": 560,
        "db.r5.large": 182, "db.r5.xlarge": 364, "db.r5.2xlarge": 729,
    }
    cost = costs.get(db_class, 200)
    return cost * 2 if multi_az else cost


def _suggest_smaller_rds(db_class: str) -> str | None:
    """Suggest the next smaller RDS instance class."""
    downgrades = {
        "db.m5.2xlarge": "db.m5.xlarge",
        "db.m5.xlarge": "db.m5.large",
        "db.m5.large": "db.t3.medium",
        "db.r5.2xlarge": "db.r5.xlarge",
        "db.r5.xlarge": "db.r5.large",
        "db.r5.large": "db.m5.large",
    }
    return downgrades.get(db_class)
