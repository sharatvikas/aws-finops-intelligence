"""Synthetic AWS backend for the offline finops demo.

This module fabricates a realistic multi-service cost dataset and exposes it
through *fake boto3 clients* that mimic the exact wire shapes the real
collectors/analyzers expect:

  * Cost Explorer  -> ``get_cost_and_usage`` / ``get_cost_forecast``
  * EC2            -> ``describe_instances`` / ``describe_volumes`` /
                      ``describe_snapshots`` / ``describe_addresses``
  * CloudWatch     -> ``get_metric_data`` (rightsizing) /
                      ``get_metric_statistics`` (idle detection)
  * RDS            -> ``describe_db_instances``

NOTHING here calls AWS. A ``FakeSession`` dispatches ``client(name)`` to the
right fake so the *real* ``finops`` code runs unmodified against it.

The data is deliberately synthetic:
  * a ~45-day daily-cost time series for 5 services,
  * an injected CRITICAL spike in EC2 (a c5.9xlarge fleet launch in us-east-1),
  * a smaller HIGH spike in S3 (a data-egress burst),
  * 3 under-utilized EC2 instances (+1 healthy one that must NOT be flagged),
  * 1 unattached EBS volume and 1 stale snapshot.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

# Anchor everything to "today" so the CLI's own date windows line up with the
# fabricated series (the collectors compute start/end from date.today()).
TODAY = date.today()
_RNG = random.Random(42)

# ---------------------------------------------------------------------------
# 1. Daily cost time series (per service) + per-dimension breakdowns
# ---------------------------------------------------------------------------

# Full service names, matching what Cost Explorer actually returns.
EC2 = "Amazon Elastic Compute Cloud - Compute"
RDS = "Amazon Relational Database Service"
S3 = "Amazon Simple Storage Service"
LAMBDA = "AWS Lambda"
CW = "AmazonCloudWatch"

_HISTORY_DAYS = 60  # generate a wide window; the CLI slices what it needs

# baseline daily $ per service, and the noise band (+/- fraction)
_SERVICE_BASELINE = {
    EC2: (420.0, 0.03),
    RDS: (182.0, 0.02),
    S3: (96.0, 0.04),
    LAMBDA: (38.0, 0.05),
    CW: (22.0, 0.04),
}

# Normal-day split of each service's spend across usage types (fractions).
_USAGE_TYPE_MIX = {
    EC2: {
        "USE1-BoxUsage:c5.9xlarge": 0.55,
        "USE1-EBS:VolumeUsage.gp3": 0.20,
        "DataTransfer-Out-Bytes": 0.15,
        "USE1-BoxUsage:m5.large": 0.10,
    },
    RDS: {"USE1-InstanceUsage:db.r5.xl": 0.7, "USE1-RDS:StorageUsage": 0.3},
    S3: {
        "USE1-TimedStorage-ByteHrs": 0.45,
        "USE1-Requests-Tier1": 0.35,
        "DataTransfer-Out-Bytes": 0.20,
    },
    LAMBDA: {"USE1-Lambda-GB-Second": 0.8, "USE1-Request": 0.2},
    CW: {"USE1-CW:MetricMonitorUsage": 0.6, "USE1-CW:Requests": 0.4},
}

# Normal-day split across regions.
_REGION_MIX = {
    EC2: {"us-east-1": 0.8, "ap-south-1": 0.2},
    RDS: {"us-east-1": 1.0},
    S3: {"us-east-1": 0.7, "eu-west-1": 0.3},
    LAMBDA: {"us-east-1": 0.6, "ap-south-1": 0.4},
    CW: {"us-east-1": 1.0},
}

# Injected anomalies: service -> (days_ago, spike_total, spike_usage_type, spike_region)
_SPIKES = {
    EC2: (4, 1850.0, "USE1-BoxUsage:c5.9xlarge", "us-east-1"),
    S3: (8, 268.0, "DataTransfer-Out-Bytes", "us-east-1"),
}


def _iso(d: date) -> str:
    return d.isoformat()


def _build_series() -> dict:
    """Return {service: {iso_date: {"total", "usage_types", "regions"}}}."""
    series: dict[str, dict[str, dict]] = {}
    spike_by_date = {
        (TODAY - timedelta(days=ago)): (svc, total, ut, region)
        for svc, (ago, total, ut, region) in _SPIKES.items()
    }

    for svc, (base, noise) in _SERVICE_BASELINE.items():
        series[svc] = {}
        for i in range(_HISTORY_DAYS):
            d = TODAY - timedelta(days=_HISTORY_DAYS - i)
            total = base * (1 + _RNG.uniform(-noise, noise))

            spike = spike_by_date.get(d)
            spike_ut = spike_region = None
            extra = 0.0
            if spike and spike[0] == svc:
                _, spike_total, spike_ut, spike_region = spike
                extra = spike_total - total
                total = spike_total

            usage_types = {
                ut: base * frac * (1 + _RNG.uniform(-noise, noise))
                for ut, frac in _USAGE_TYPE_MIX[svc].items()
            }
            regions = {
                r: base * frac * (1 + _RNG.uniform(-noise, noise))
                for r, frac in _REGION_MIX[svc].items()
            }
            if extra > 0:
                usage_types[spike_ut] = usage_types.get(spike_ut, 0.0) + extra
                regions[spike_region] = regions.get(spike_region, 0.0) + extra

            series[svc][_iso(d)] = {
                "total": round(total, 4),
                "usage_types": {k: round(v, 4) for k, v in usage_types.items()},
                "regions": {k: round(v, 4) for k, v in regions.items()},
            }
    return series


SERIES = _build_series()


def _avg_daily_total() -> float:
    per_day: dict[str, float] = {}
    for svc_days in SERIES.values():
        for d, rec in svc_days.items():
            per_day[d] = per_day.get(d, 0.0) + rec["total"]
    return sum(per_day.values()) / len(per_day)


FORECAST_AMOUNT = round(_avg_daily_total() * 30, 2)


# ---------------------------------------------------------------------------
# 2. EC2 / CloudWatch / RDS fixtures (rightsizing + waste)
# ---------------------------------------------------------------------------

# instance_id -> (name, instance_type, avg_cpu, p90_cpu)
INSTANCES = {
    "i-0a1b2c3d4e5f6a7b": ("analytics-worker", "c5.4xlarge", 7.5, 12.0),
    "i-0c2d3e4f5a6b7c8d": ("api-gateway-fleet", "m5.2xlarge", 22.0, 34.0),
    "i-0e3f4a5b6c7d8e9f": ("nightly-batch", "r5.xlarge", 6.0, 11.0),
    "i-0f4a5b6c7d8e9f01": ("prod-web-1", "m5.large", 54.0, 79.0),  # healthy: NOT flagged
}

_NOW = datetime.now(timezone.utc)

# Unattached EBS volumes (status "available").
VOLUMES = [
    {
        "VolumeId": "vol-0abc1234def56789",
        "Size": 1000,
        "VolumeType": "gp3",
        "CreateTime": _NOW - timedelta(days=63),
        "State": "available",
    },
]

# Old EBS snapshots owned by "self".
SNAPSHOTS = [
    {
        "SnapshotId": "snap-0f1e2d3c4b5a6978",
        "VolumeSize": 600,
        "StartTime": _NOW - timedelta(days=140),
        "Description": "pre-migration-backup",
    },
]


class _Paginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        return list(self._pages)


class FakeCostExplorer:
    """Mimics the ``ce`` client used by CostExplorerCollector."""

    def get_cost_and_usage(self, **request):
        gran = request["Granularity"]
        group_key = request["GroupBy"][0]["Key"]
        tp = request["TimePeriod"]
        start = date.fromisoformat(tp["Start"])
        end = date.fromisoformat(tp["End"])
        service_filter = None
        filt = request.get("Filter", {})
        if "Dimensions" in filt and filt["Dimensions"].get("Key") == "SERVICE":
            service_filter = filt["Dimensions"]["Values"][0]

        if gran == "MONTHLY":
            return {"ResultsByTime": [self._monthly_bucket(start, end)]}
        return {"ResultsByTime": self._daily_buckets(start, end, group_key, service_filter)}

    def _monthly_bucket(self, start: date, end: date) -> dict:
        totals: dict[str, float] = {}
        d = start
        while d < end:
            iso = _iso(d)
            for svc, days in SERIES.items():
                if iso in days:
                    totals[svc] = totals.get(svc, 0.0) + days[iso]["total"]
            d += timedelta(days=1)
        return {
            "TimePeriod": {"Start": _iso(start), "End": _iso(end)},
            "Groups": [_group([svc], amt) for svc, amt in totals.items()],
            "Total": {},
        }

    def _daily_buckets(self, start, end, group_key, service_filter):
        buckets = []
        d = start
        while d < end:
            iso = _iso(d)
            groups = []
            if group_key == "SERVICE":
                for svc, days in SERIES.items():
                    if iso in days:
                        groups.append(_group([svc], days[iso]["total"]))
            elif group_key in ("USAGE_TYPE", "REGION"):
                field = "usage_types" if group_key == "USAGE_TYPE" else "regions"
                svc = service_filter
                if svc and iso in SERIES.get(svc, {}):
                    for value, amt in SERIES[svc][iso][field].items():
                        groups.append(_group([value], amt))
            buckets.append({
                "TimePeriod": {"Start": iso, "End": _iso(d + timedelta(days=1))},
                "Groups": groups,
                "Total": {},
            })
            d += timedelta(days=1)
        return buckets

    def get_cost_forecast(self, **kwargs):
        return {
            "Total": {"Amount": str(FORECAST_AMOUNT), "Unit": "USD"},
            "ForecastResultsByTime": [{"MeanValue": str(FORECAST_AMOUNT)}],
        }


def _group(keys, amount) -> dict:
    return {
        "Keys": keys,
        "Metrics": {"UnblendedCost": {"Amount": str(round(amount, 6)), "Unit": "USD"}},
    }


class FakeEC2:
    """Mimics the ``ec2`` client for rightsizing + waste detection."""

    def get_paginator(self, name: str):
        if name == "describe_instances":
            instances = [
                {
                    "InstanceId": iid,
                    "InstanceType": itype,
                    "State": {"Name": "running"},
                    "Tags": [{"Key": "Name", "Value": name}],
                }
                for iid, (name, itype, _avg, _p90) in INSTANCES.items()
            ]
            return _Paginator([{"Reservations": [{"Instances": instances}]}])
        if name == "describe_volumes":
            return _Paginator([{"Volumes": VOLUMES}])
        if name == "describe_snapshots":
            return _Paginator([{"Snapshots": SNAPSHOTS}])
        raise NotImplementedError(name)

    def describe_addresses(self, **kwargs):
        return {"Addresses": []}  # no unassociated EIPs in this scenario


class FakeCloudWatch:
    """Mimics the ``cloudwatch`` client. Serves both metric APIs."""

    def get_metric_data(self, MetricDataQueries, StartTime, EndTime):
        iid = MetricDataQueries[0]["MetricStat"]["Metric"]["Dimensions"][0]["Value"]
        if iid not in INSTANCES:
            return {"MetricDataResults": [{"Id": "avg_cpu", "Values": []},
                                          {"Id": "p90_cpu", "Values": []}]}
        _name, _itype, avg, p90 = INSTANCES[iid]
        days = (EndTime - StartTime).days or 14
        return {
            "MetricDataResults": [
                {"Id": "avg_cpu", "Values": [avg] * days},
                {"Id": "p90_cpu", "Values": [p90] * days},
            ]
        }

    def get_metric_statistics(self, **kwargs):
        dims = {d["Name"]: d["Value"] for d in kwargs.get("Dimensions", [])}
        iid = dims.get("InstanceId")
        if iid in INSTANCES:
            avg = INSTANCES[iid][2]
            return {"Datapoints": [{"Average": avg} for _ in range(7)]}
        return {"Datapoints": []}


class FakeRDS:
    def get_paginator(self, name: str):
        assert name == "describe_db_instances"
        return _Paginator([{"DBInstances": []}])  # no RDS in this scenario


class FakeSession:
    """Drop-in for boto3.Session: dispatches client(name) to the right fake."""

    def __init__(self, *args, **kwargs):
        self.region_name = kwargs.get("region_name") or "us-east-1"

    def client(self, name, region_name=None, **kwargs):
        if name == "ce":
            return FakeCostExplorer()
        if name == "ec2":
            return FakeEC2()
        if name == "cloudwatch":
            return FakeCloudWatch()
        if name == "rds":
            return FakeRDS()
        raise NotImplementedError(f"No fake for boto3 client {name!r}")
