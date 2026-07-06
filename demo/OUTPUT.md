# Offline demo — captured output

> **Everything below is real, captured output** from running the demo driver in
> a local venv. **The data is 100% synthetic** — no AWS API and no Slack webhook
> are contacted. `boto3.Session` is swapped for a fake that serves a fabricated
> Cost Explorer / EC2 / CloudWatch dataset, and the Slack digest is rendered in
> `--dry-run` (printed, never sent).

## One command

```bash
# from the repo root, in a venv with the package installed (see demo/README.md)
python demo/run_demo.py
```

The driver imports the **real** `finops` CLI and invokes four commands against
the synthetic backend:

| # | Command                                   | Proves |
|---|-------------------------------------------|--------|
| 1 | `finops anomalies detect --days 45`        | MAD z-score anomaly detection + root-cause attribution |
| 2 | `finops rightsize --days 14 --min-savings 10` | EC2 rightsizing recs with monthly $ savings |
| 3 | `finops scan --days 45 --dry-run`          | Full scan → Slack Block Kit digest (dry-run payload) |
| 4 | `finops scan --days 45 --json-output`      | Same scan as structured JSON |

> Dates are anchored to the machine's "today" (this capture: **2026-07-05**), so
> the injected EC2 spike lands on `today − 4` and the S3 spike on `today − 8`.
> Re-running on another day shifts the dates but not the shape of the results.
> ANSI colour / spinner control codes have been stripped for readability; the
> table box-drawing is verbatim.

---

## 1. `finops anomalies detect --days 45`

MAD (median absolute deviation) robust z-score over the daily series, with each
top anomaly attributed to the usage type / region that actually moved.

```
                                       Statistical Cost Anomalies (last 45d, z≥2.5)
╭──────────┬────────────────────────────────────────┬────────────┬───────────┬──────────┬──────────┬────────┬────────────╮
│ Severity │ Service                                │ Date       │     Spend │ Baseline │ Robust z │ Change │     Impact │
├──────────┼────────────────────────────────────────┼────────────┼───────────┼──────────┼──────────┼────────┼────────────┤
│ CRITICAL │ Amazon Elastic Compute Cloud - Compute │ 2026-07-01 │ $1,850.00 │  $425.45 │    175.7 │  +335% │ +$1,424.55 │
│   HIGH   │ Amazon Simple Storage Service          │ 2026-06-27 │   $268.00 │   $94.97 │     54.2 │  +182% │   +$173.03 │
╰──────────┴────────────────────────────────────────┴────────────┴───────────┴──────────┴──────────┴────────┴────────────╯

Amazon Elastic Compute Cloud - Compute on 2026-07-01 — likely drivers:
  • USAGE_TYPE=USE1-BoxUsage:c5.9xlarge: $1,666.68 vs $231.55/day baseline (+$1,435.13, 100% of the spike)
  • REGION=us-east-1: $1,772.63 vs $338.93/day baseline (+$1,433.70, 100% of the spike)

Amazon Simple Storage Service on 2026-06-27 — likely drivers:
  • USAGE_TYPE=DataTransfer-Out-Bytes: $188.57 vs $19.30/day baseline (+$169.27, 98% of the spike)
  • REGION=us-east-1: $234.78 vs $67.30/day baseline (+$167.47, 97% of the spike)
  • REGION=eu-west-1: $29.83 vs $28.78/day baseline (+$1.05, 1% of the spike)
```

The injected EC2 fleet launch (`c5.9xlarge` in `us-east-1`) is correctly
identified as ~100% of the spike; the noise floor on other usage types is not
flagged.

---

## 2. `finops rightsize --days 14 --min-savings 10`

Sizes on the **worst daily P90 CPU** (not the average), so a busy day vetoes a
downsize. A healthy `m5.large` instance (avg 54% / P90 79%) is in the fixture
and is correctly **not** recommended.

```
                         EC2 Rightsizing — $604/mo potential savings
╭───────────────────┬────────────┬─────────────┬─────────┬─────────┬────────────┬────────────╮
│ Instance          │ Current    │ Recommended │ P90 CPU │ Avg CPU │ Savings/mo │ Confidence │
├───────────────────┼────────────┼─────────────┼─────────┼─────────┼────────────┼────────────┤
│ analytics-worker  │ c5.4xlarge │ c5.xlarge   │     12% │      8% │       $372 │    HIGH    │
│ api-gateway-fleet │ m5.2xlarge │ m5.xlarge   │     34% │     22% │       $140 │   MEDIUM   │
│ nightly-batch     │ r5.xlarge  │ r5.large    │     11% │      6% │        $92 │   MEDIUM   │
╰───────────────────┴────────────┴─────────────┴─────────┴─────────┴────────────┴────────────╯
Sized on worst daily P90 CPU — busiest day must still fit the smaller type.
```

---

## 3. `finops scan --days 45 --dry-run`

Full scan (anomalies + waste + rightsizing) followed by the **Slack Block Kit
payload printed to stdout** instead of being posted (`--dry-run`).

```
╭──────────────────────────────────────────────────────────────── AWS FinOps Digest ─────────────────────────────────────────────────────────────────╮
│ MTD Spend:     $     4,449                                                                                                                         │
│ Forecast:      $    23,537                                                                                                                         │
│ Waste (mo):    $       110                                                                                                                         │
│ Anomalies:              2 (1 critical)                                                                                                             │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
                                   Cost Anomalies

  Severity   Service                                  Date         Amount   Change
 ──────────────────────────────────────────────────────────────────────────────────
  CRITICAL   Amazon Elastic Compute Cloud - Compute   2026-07-01    $1850    +335%
  HIGH       Amazon Simple Storage Service            2026-06-27     $268    +182%

                      Top Services (30d)

  Service                                  30d Spend   Change
 ─────────────────────────────────────────────────────────────
  Amazon Elastic Compute Cloud - Compute     $14,035   ↑ +12%
  Amazon Relational Database Service          $5,460    → +0%
  Amazon Simple Storage Service               $3,042    ↑ +5%
  AWS Lambda                                  $1,136    → -1%
  AmazonCloudWatch                              $660    → +0%


Rightsizing:
  • analytics-worker: c5.4xlarge → c5.xlarge (save $372/mo, P90 CPU: 12%)
  • api-gateway-fleet: m5.2xlarge → m5.xlarge (save $140/mo, P90 CPU: 34%)
  • nightly-batch: r5.xlarge → r5.large (save $92/mo, P90 CPU: 11%)
```

### Slack Block Kit dry-run payload (printed, NOT sent)

```json
{
  "text": "AWS FinOps Digest: $4,449 MTD, 2 anomalies, $714/mo savings identified",
  "blocks": [
    {
      "type": "header",
      "text": {
        "type": "plain_text",
        "text": "☁️ AWS FinOps Digest — July 05, 2026"
      }
    },
    {
      "type": "section",
      "fields": [
        { "type": "mrkdwn", "text": "*MTD Spend*\n$4,449" },
        { "type": "mrkdwn", "text": "*Month Forecast*\n$23,537" },
        { "type": "mrkdwn", "text": "*Savings Identified*\n$714/mo" },
        { "type": "mrkdwn", "text": "*Anomalies*\n2 (1 critical)" }
      ]
    },
    { "type": "divider" },
    {
      "type": "section",
      "text": {
        "type": "mrkdwn",
        "text": "*Cost Anomalies*\n🔴 *Amazon Elastic Compute Cloud - Compute*: $1,850 on 2026-07-01 (+335% vs baseline, z=175.7)\n    ↳ _USAGE_TYPE=USE1-BoxUsage:c5.9xlarge: $1,666.68 vs $231.55/day baseline (+$1,435.13, 100% of the spike)_\n    ↳ _REGION=us-east-1: $1,772.63 vs $338.93/day baseline (+$1,433.70, 100% of the spike)_\n🟠 *Amazon Simple Storage Service*: $268 on 2026-06-27 (+182% vs baseline, z=54.2)\n    ↳ _USAGE_TYPE=DataTransfer-Out-Bytes: $188.57 vs $19.30/day baseline (+$169.27, 98% of the spike)_\n    ↳ _REGION=us-east-1: $234.78 vs $67.30/day baseline (+$167.47, 97% of the spike)_"
      }
    },
    {
      "type": "section",
      "text": {
        "type": "mrkdwn",
        "text": "*Waste Findings* ($110/mo total)\n• *Unattached EBS* — `vol-0abc1234def56789`: $80/mo\n  _Volume unattached for 63 days_\n• *Old EBS Snapshot* — `snap-0f1e2d3c4b5a6978`: $30/mo\n  _Snapshot is 140 days old (600GB)_"
      }
    },
    {
      "type": "section",
      "text": {
        "type": "mrkdwn",
        "text": "*Rightsizing* ($604/mo potential)\n• `analytics-worker`: c5.4xlarge → *c5.xlarge* — save $372/mo (P90 CPU 12%, HIGH)\n• `api-gateway-fleet`: m5.2xlarge → *m5.xlarge* — save $140/mo (P90 CPU 34%, MEDIUM)\n• `nightly-batch`: r5.xlarge → *r5.large* — save $92/mo (P90 CPU 11%, MEDIUM)"
      }
    },
    {
      "type": "section",
      "text": {
        "type": "mrkdwn",
        "text": "*Top Services by Spend (30d)*\n⚪ Amazon Elastic Compute Cloud - Compute: $14,035 ↑ +12%\n⚪ Amazon Relational Database Service: $5,460 → +0%\n⚪ Amazon Simple Storage Service: $3,042 ↑ +5%\n⚪ AWS Lambda: $1,136 → -1%\n⚪ AmazonCloudWatch: $660 → +0%"
      }
    },
    {
      "type": "context",
      "elements": [
        { "type": "mrkdwn", "text": "aws-finops-intelligence • `finops scan --slack` for the full report" }
      ]
    }
  ]
}
```

This is a valid Slack Block Kit message (header + summary fields + divider +
anomaly/waste/rightsizing/top-services sections + context footer). In a real run
`finops scan --slack` (without `--dry-run`) would `POST` this to
`SLACK_WEBHOOK_URL`; here it is printed and nothing is sent.

---

## 4. `finops scan --days 45 --json-output`

The same scan, machine-readable:

```json
{
  "total_mtd": 4448.53,
  "forecast": 23536.96,
  "anomalies": [
    {
      "service": "Amazon Elastic Compute Cloud - Compute",
      "date": "2026-07-01",
      "amount": 1850.0,
      "robust_z": 175.66,
      "change_pct": 334.8,
      "severity": "CRITICAL",
      "root_causes": [
        "USAGE_TYPE=USE1-BoxUsage:c5.9xlarge: $1,666.68 vs $231.55/day baseline (+$1,435.13, 100% of the spike)",
        "REGION=us-east-1: $1,772.63 vs $338.93/day baseline (+$1,433.70, 100% of the spike)"
      ]
    },
    {
      "service": "Amazon Simple Storage Service",
      "date": "2026-06-27",
      "amount": 268.0,
      "robust_z": 54.16,
      "change_pct": 182.2,
      "severity": "HIGH",
      "root_causes": [
        "USAGE_TYPE=DataTransfer-Out-Bytes: $188.57 vs $19.30/day baseline (+$169.27, 98% of the spike)",
        "REGION=us-east-1: $234.78 vs $67.30/day baseline (+$167.47, 97% of the spike)",
        "REGION=eu-west-1: $29.83 vs $28.78/day baseline (+$1.05, 1% of the spike)"
      ]
    }
  ],
  "waste": [
    { "category": "Unattached EBS",   "resource_id": "vol-0abc1234def56789", "monthly_cost": 80.0, "reason": "Volume unattached for 63 days" },
    { "category": "Old EBS Snapshot", "resource_id": "snap-0f1e2d3c4b5a6978", "monthly_cost": 30.0, "reason": "Snapshot is 140 days old (600GB)" }
  ],
  "rightsizing": [
    { "name": "analytics-worker",  "current_type": "c5.4xlarge", "recommended_type": "c5.xlarge", "monthly_savings": 372.3,  "p90_cpu_pct": 12.0 },
    { "name": "api-gateway-fleet", "current_type": "m5.2xlarge", "recommended_type": "m5.xlarge", "monthly_savings": 140.16, "p90_cpu_pct": 34.0 },
    { "name": "nightly-batch",     "current_type": "r5.xlarge",  "recommended_type": "r5.large",  "monthly_savings": 91.98,  "p90_cpu_pct": 11.0 }
  ],
  "total_monthly_savings": 714.44
}
```

---

## What this proves

- The **real** `finops` CLI, Cost Explorer collector (pagination + parsing), MAD
  anomaly detector, root-cause attributor, EC2 rightsizing analyzer, waste
  detector, and Slack Block Kit builder all run end-to-end on localhost.
- Anomalies carry **severity** (CRITICAL/HIGH) and **root cause** (which usage
  type / region moved, with % contribution).
- Rightsizing produces **monthly $ savings** with confidence, and correctly
  leaves a healthy instance untouched.
- The Slack digest is produced as a valid Block Kit payload and shown via
  `--dry-run` — printed, not sent.
- No live AWS or Slack calls: only `boto3.Session` is swapped for a fake; all
  business logic is the shipping code.
