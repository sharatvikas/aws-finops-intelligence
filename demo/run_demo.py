#!/usr/bin/env python
"""Offline finops demo driver.

Runs the REAL ``finops`` CLI (collector + MAD anomaly detector + rightsizing +
Slack Block Kit reporter) against a fully synthetic AWS backend. No AWS or
Slack network calls are made: ``boto3.Session`` is swapped for a fake that
serves fabricated Cost Explorer / EC2 / CloudWatch data, and the Slack digest
is rendered in ``--dry-run`` (printed, never sent).

Run:  python demo/run_demo.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make ``demo`` importable and ensure the real package is on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import boto3  # noqa: E402
from click.testing import CliRunner  # noqa: E402

import synthetic_aws  # noqa: E402

# --- Install the synthetic AWS backend --------------------------------------
# Any boto3.Session(...) built by the CLI now returns our fake.
boto3.Session = synthetic_aws.FakeSession  # type: ignore[assignment]

# Make sure no stray Slack credentials cause a real post.
for _var in ("SLACK_WEBHOOK_URL", "SLACK_BOT_TOKEN", "SLACK_CHANNEL"):
    os.environ.pop(_var, None)

from finops.cli import cli  # noqa: E402  (import AFTER patching boto3)

runner = CliRunner()


def _run(argv: list[str], title: str) -> None:
    print("\n" + "=" * 78)
    print(f"$ finops {' '.join(argv)}")
    print(f"# {title}")
    print("=" * 78)
    result = runner.invoke(cli, argv, catch_exceptions=False)
    sys.stdout.write(result.output)
    if result.exit_code != 0:
        print(f"\n[demo] command exited with code {result.exit_code}", file=sys.stderr)
        raise SystemExit(result.exit_code)


def main() -> None:
    print("AWS FinOps Intelligence - OFFLINE DEMO")
    print("Data is SYNTHETIC. No AWS API or Slack webhook is contacted.")
    print(f"Anchor date (today): {synthetic_aws.TODAY.isoformat()}")

    # 1) Statistical anomaly detection with root-cause attribution.
    _run(
        ["anomalies", "detect", "--days", "45"],
        "MAD z-score anomaly detection + usage-type/region root cause",
    )

    # 2) EC2 rightsizing from P90 CPU.
    _run(
        ["rightsize", "--days", "14", "--min-savings", "10"],
        "EC2 rightsizing recommendations with monthly $ savings",
    )

    # 3) Full scan + Slack Block Kit digest (dry-run: printed, not sent).
    _run(
        ["scan", "--days", "45", "--dry-run"],
        "Full scan -> Slack Block Kit digest (dry-run payload printed)",
    )

    # 4) Machine-readable snapshot of the same scan.
    _run(
        ["scan", "--days", "45", "--json-output"],
        "Same scan as structured JSON",
    )

    print("\n" + "=" * 78)
    print("[demo] done - all output above is real, generated offline.")
    print("=" * 78)


if __name__ == "__main__":
    main()
