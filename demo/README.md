# Offline demo

A **fully offline, one-command** demonstration of `aws-finops-intelligence`.
It runs the **real** CLI — cost collector, MAD anomaly detector with root-cause
hints, EC2 rightsizing, and the Slack Block Kit reporter — against a **synthetic
AWS backend**. No AWS credentials, no network, no Slack webhook required.

> **The data is synthetic.** `boto3.Session` is swapped for a fake that serves a
> fabricated Cost Explorer / EC2 / CloudWatch / RDS dataset. The Slack digest is
> rendered in `--dry-run` mode (printed to stdout, never sent). Nothing in this
> demo contacts AWS or Slack.

## Run it

```bash
# 1. Create a venv and install the package (Python 3.12 or 3.14 both work)
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .

# 2. One command:
python demo/run_demo.py
```

That's it. The driver invokes four real CLI commands in sequence and prints
their output:

1. `finops anomalies detect --days 45` — anomalies with severity + root cause
2. `finops rightsize --days 14 --min-savings 10` — rightsizing recs with $ savings
3. `finops scan --days 45 --dry-run` — full scan + Slack Block Kit dry-run payload
4. `finops scan --days 45 --json-output` — the same scan as JSON

See [`OUTPUT.md`](./OUTPUT.md) for a full captured run.

## How it works (why it's a faithful demo)

```
demo/run_demo.py        driver: patches boto3.Session, invokes the real CLI
demo/synthetic_aws.py   the synthetic dataset + fake boto3 clients
```

`run_demo.py` does exactly one substitution before importing the CLI:

```python
import boto3, synthetic_aws
boto3.Session = synthetic_aws.FakeSession   # the only fake
from finops.cli import cli                   # everything below is shipping code
```

`FakeSession.client("ce" | "ec2" | "cloudwatch" | "rds")` returns fakes that
mimic the exact AWS wire shapes the collectors/analyzers expect
(`get_cost_and_usage`, `get_cost_forecast`, `describe_instances`,
`get_metric_data`, `get_metric_statistics`, …). This mirrors how the unit tests
in `tests/` stub boto3 — so the demo exercises the same real code paths, just
driven through the CLI instead of pytest.

## The synthetic scenario

- **~45 days** of daily cost across 5 services (EC2, RDS, S3, Lambda, CloudWatch)
  with small daily noise around a stable baseline.
- **Injected CRITICAL anomaly:** an EC2 `c5.9xlarge` fleet launch in `us-east-1`
  spikes EC2 to **$1,850** on `today − 4` (≈+335% vs a ~$425 baseline).
- **Injected HIGH anomaly:** an S3 data-egress burst to **$268** on `today − 8`
  (≈+182%).
- **3 under-utilized EC2 instances** (c5.4xlarge, m5.2xlarge, r5.xlarge) plus
  **1 healthy instance** (m5.large) that must **not** be flagged.
- **1 unattached EBS volume** (1000 GB gp3) and **1 stale snapshot** (600 GB,
  140 days old) for the waste section.

Dates are anchored to the machine's `today`, so a re-run on a different day
shifts the dates but produces the same structure and severities.

## Going live (not part of this demo)

With real credentials you would instead run, e.g.:

```bash
export AWS_PROFILE=my-finops-readonly
export SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
finops scan --days 45 --slack          # posts the same Block Kit digest for real
```

The demo deliberately stops short of that: it only proves the analytics and
reporting work, offline, on real captured output.
