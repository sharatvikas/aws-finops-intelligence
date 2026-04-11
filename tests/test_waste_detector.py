"""Moto-backed tests for WasteDetector — validates AWS API interaction patterns."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws
from finops.analyzers.waste import WasteDetector, WasteItem


@pytest.fixture
def aws_session():
    """Return a boto3 session pointed at moto's fake AWS."""
    return boto3.Session(
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        aws_session_token="testing",
        region_name="us-east-1",
    )


@mock_aws
class TestFindUnattachedEBSVolumes:
    def test_detects_available_volume(self, aws_session):
        ec2 = aws_session.client("ec2", region_name="us-east-1")
        vol = ec2.create_volume(
            AvailabilityZone="us-east-1a",
            Size=100,
            VolumeType="gp3",
        )
        vol_id = vol["VolumeId"]

        detector = WasteDetector(aws_session, region="us-east-1")
        items = detector.find_unattached_ebs_volumes()

        ids = [i.resource_id for i in items]
        assert vol_id in ids

    def test_attached_volume_not_flagged(self, aws_session):
        ec2 = aws_session.client("ec2", region_name="us-east-1")
        # Create an instance first
        ami = ec2.describe_images(
            Filters=[{"Name": "name", "Values": ["amzn2-ami-*"]}]
        )
        # Use a known moto AMI
        instance = ec2.run_instances(
            ImageId="ami-12c6146b",
            MinCount=1,
            MaxCount=1,
            InstanceType="t3.micro",
        )
        instance_id = instance["Instances"][0]["InstanceId"]

        vol = ec2.create_volume(
            AvailabilityZone="us-east-1a",
            Size=50,
            VolumeType="gp3",
        )
        vol_id = vol["VolumeId"]

        ec2.attach_volume(
            VolumeId=vol_id,
            InstanceId=instance_id,
            Device="/dev/xvdf",
        )

        detector = WasteDetector(aws_session, region="us-east-1")
        items = detector.find_unattached_ebs_volumes()

        ids = [i.resource_id for i in items]
        assert vol_id not in ids

    def test_waste_item_fields_populated(self, aws_session):
        ec2 = aws_session.client("ec2", region_name="us-east-1")
        ec2.create_volume(
            AvailabilityZone="us-east-1a",
            Size=200,
            VolumeType="gp3",
        )

        detector = WasteDetector(aws_session, region="us-east-1")
        items = detector.find_unattached_ebs_volumes()

        assert len(items) >= 1
        item = items[0]
        assert item.category == "storage"
        assert item.resource_type == "EBS Volume"
        assert item.estimated_monthly_cost > 0
        assert item.remediation != ""


@mock_aws
class TestFindUnassociatedElasticIPs:
    def test_detects_unassociated_eip(self, aws_session):
        ec2 = aws_session.client("ec2", region_name="us-east-1")
        alloc = ec2.allocate_address(Domain="vpc")
        eip_id = alloc["AllocationId"]

        detector = WasteDetector(aws_session, region="us-east-1")
        items = detector.find_unassociated_elastic_ips()

        ids = [i.resource_id for i in items]
        assert eip_id in ids

    def test_associated_eip_not_flagged(self, aws_session):
        ec2 = aws_session.client("ec2", region_name="us-east-1")
        alloc = ec2.allocate_address(Domain="vpc")
        eip_id = alloc["AllocationId"]

        instance = ec2.run_instances(
            ImageId="ami-12c6146b",
            MinCount=1,
            MaxCount=1,
            InstanceType="t3.micro",
        )
        instance_id = instance["Instances"][0]["InstanceId"]
        ec2.associate_address(InstanceId=instance_id, AllocationId=eip_id)

        detector = WasteDetector(aws_session, region="us-east-1")
        items = detector.find_unassociated_elastic_ips()

        ids = [i.resource_id for i in items]
        assert eip_id not in ids

    def test_eip_monthly_cost_is_nonzero(self, aws_session):
        ec2 = aws_session.client("ec2", region_name="us-east-1")
        ec2.allocate_address(Domain="vpc")

        detector = WasteDetector(aws_session, region="us-east-1")
        items = detector.find_unassociated_elastic_ips()

        assert items[0].estimated_monthly_cost > 0


@mock_aws
class TestDetectAll:
    def test_detect_all_returns_sorted_by_cost(self, aws_session):
        ec2 = aws_session.client("ec2", region_name="us-east-1")
        # Create cheap waste (EIP) and expensive waste (large EBS)
        ec2.allocate_address(Domain="vpc")
        ec2.create_volume(
            AvailabilityZone="us-east-1a",
            Size=1000,  # Large volume — higher cost
            VolumeType="gp3",
        )

        detector = WasteDetector(aws_session, region="us-east-1")
        items = detector.detect_all()

        assert len(items) >= 2
        costs = [i.estimated_monthly_cost for i in items]
        assert costs == sorted(costs, reverse=True)

    def test_detect_all_returns_waste_items(self, aws_session):
        ec2 = aws_session.client("ec2", region_name="us-east-1")
        ec2.allocate_address(Domain="vpc")

        detector = WasteDetector(aws_session, region="us-east-1")
        items = detector.detect_all()

        for item in items:
            assert isinstance(item, WasteItem)
            assert item.resource_id
            assert item.category
            assert item.remediation
