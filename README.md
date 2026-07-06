# aws-finops-intelligence

> **AI-driven AWS cost intelligence platform.** Ingests Cost Explorer, Trusted Advisor, and resource utilization data to surface anomalies, identify waste, and generate actionable rightsizing recommendations — with natural language explanations and Terraform remediation snippets.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![AWS](https://img.shields.io/badge/AWS-powered-FF9900.svg)](https://aws.amazon.com)
[![Claude API](https://img.shields.io/badge/Claude-API-orange.svg)](https://anthropic.com)

---

## The Problem

AWS bills are complex. Between Reserved Instances, Savings Plans, spot pricing, data transfer, and hundreds of service dimensions, even experienced engineers miss optimization opportunities worth thousands of dollars a month.

This tool does what a FinOps analyst would do — but runs nightly and sends you a Slack digest.

---

## What It Analyzes

### Anomaly Detection
- Rolling **median + MAD robust z-score** per service/account against a trailing baseline — one previous spike can't inflate the baseline and mask the next one
- **Week-over-week seasonality guard**: spend matching its same-weekday norm (weekend batch jobs, Monday scale-ups) is not flagged
- **Severity scoring** (0–100) combining statistical surprise, relative change, and absolute dollar impact
- **Root-cause hints**: re-queries Cost Explorer by `USAGE_TYPE` / `REGION` scoped to the anomalous service and reports which dimension values moved
- AI explains *why* the anomaly likely occurred (correlates with deployments, auto-scaling events)

### Waste Identification
| Category | Detection Method |
|----------|-----------------|
| Idle EC2 instances | < 5% CPU + < 10% network for 7 days |
| Unattached EBS volumes | Not attached to any instance |
| Oversized RDS instances | < 20% CPU + connection count vs. max |
| Old EBS snapshots | Age > 90 days, no lifecycle policy |
| Unused Elastic IPs | Not associated with running instances |
| S3 lifecycle gaps | Objects in Standard storage > 30 days without lifecycle rule |
| Nat Gateway overuse | Compare with Transit Gateway cost |

### Rightsizing
- EC2: sizes on the **worst daily P90 CPU** over 14 days — the busiest day must still fit the smaller instance
- Two sizes down when P90 < 20%, one when P90 < 40%; burstable (t-family) instances never jump more than one size (CPU-credit behaviour changes per size)
- Dollar impact per recommendation (current vs projected monthly cost) with a confidence grade
- RDS: oversized-instance detection via CPU utilization (see waste detection)

### Savings Plan / RI Analysis
- On-Demand coverage gaps where Savings Plans would save >20%
- RI utilization — underused RIs and their effective waste cost
- Convertible RI exchange recommendations

---

## Sample Output

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  AWS FinOps Intelligence Report — 2026-04-08
  Account: prod-account (123456789012)
  Total MTD Spend: $47,293  |  Forecast: $61,800
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ANOMALIES (1)
⚠ EC2 spend +43% vs. baseline ($8,200 vs. $5,700)
  AI: Spike correlates with auto-scaling event on April 6th. ASG
  max_size was increased from 10→50 during the load test but never
  rolled back. 12 c5.2xlarge instances are still running idle.

WASTE DETECTED — Est. Monthly Savings: $3,847
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  $2,100/mo  12 idle EC2 instances (load test ASG not cleaned up)
  $  840/mo  3 unattached gp3 volumes (340GB total)
  $  612/mo  1 oversized RDS db.r5.2xlarge (avg CPU: 4%)
  $  295/mo  47 unassociated Elastic IPs

RIGHTSIZING (top 3)
  analytics-worker: c5.4xlarge → c5.xlarge  (save $280/mo, P90 CPU: 12%)
  batch-processor:  m5.2xlarge → m5.large   (save $195/mo, P90 CPU: 8%)
  api-gateway-ec2:  t3.large   → t3.medium  (save $45/mo,  P90 CPU: 18%)

TERRAFORM REMEDIATION
  See: remediation/2026-04-08/main.tf
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                 aws-finops-intelligence                     │
│                                                             │
│  Collectors                    Analysis                     │
│  ┌───────────────┐    ┌────────────────────────────────┐   │
│  │ Cost Explorer │    │                                │   │
│  │ CloudWatch    │───▶│   Statistical Anomaly Engine   │   │
│  │ Trusted Adv.  │    │   + Claude API Explanation     │   │
│  │ EC2/RDS APIs  │    │                                │   │
│  └───────────────┘    └────────────┬───────────────────┘   │
│                                    │                        │
│  ┌─────────────────────────────────▼──────────────────┐    │
│  │                  Report Generator                  │    │
│  │    Slack Digest │ HTML Report │ Terraform Snippets │    │
│  └────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

---

## Quick Start

```bash
git clone https://github.com/sharatvikas/aws-finops-intelligence.git
cd aws-finops-intelligence
pip install -e ".[dev]"

finops scan            # anomalies + waste + rightsizing in one pass
```

## CLI

Global options work on every command: `--profile <aws-profile>`, `--region <region>`, `-v/--verbose`.

```bash
# Full FinOps scan: anomalies + waste + rightsizing, posted to Slack
finops --profile prod scan --days 45 --threshold 2.5 --slack

# Preview exactly what would be sent (prints the Block Kit JSON, sends nothing)
finops scan --slack --dry-run

# Statistical anomaly detection (median + MAD, weekly seasonality, root causes)
finops anomalies detect --days 45 --threshold 3.0 --json-output
finops anomalies detect --by-account          # group by linked account
finops anomalies list                          # AWS's ML detector, by contrast

# EC2 rightsizing from CloudWatch P90 CPU
finops --region us-west-2 rightsize --days 14 --min-savings 50 --json-output

# Waste scan, spend summary, forecast, budgets, savings plans
finops waste --min-savings 100
finops analyze --top 10
finops forecast --horizon 90
finops budgets list --critical-only
finops savings coverage
```

All `--json-output` modes print clean JSON to stdout (progress spinners go to
stderr), so `finops scan --json-output | jq .total_monthly_savings` just works.

### Anomaly detection example

```
$ finops anomalies detect --days 45
     Statistical Cost Anomalies (last 45d, z≥2.5)
╭──────────┬────────────┬────────────┬───────────┬──────────┬──────────┬────────┬────────────╮
│ Severity │ Service    │ Date       │     Spend │ Baseline │ Robust z │ Change │     Impact │
├──────────┼────────────┼────────────┼───────────┼──────────┼──────────┼────────┼────────────┤
│ CRITICAL │ Amazon EC2 │ 2026-07-03 │ $8,200.00 │ $5,730.00│      6.7 │   +43% │ +$2,470.00 │
╰──────────┴────────────┴────────────┴───────────┴──────────┴──────────┴────────┴────────────╯

Amazon EC2 on 2026-07-03 — likely drivers:
  • USAGE_TYPE=BoxUsage:c5.2xlarge: $2,600.00 vs $310.00/day baseline (+$2,290.00, 93% of the spike)
  • REGION=us-east-1: $2,650.00 vs $380.00/day baseline (+$2,270.00, 92% of the spike)
```

## Slack Digest

Two delivery modes — incoming webhook (`SLACK_WEBHOOK_URL`) or bot token
(`SLACK_BOT_TOKEN` + `SLACK_CHANNEL`). `--dry-run` prints the payload instead
of sending, e.g.:

```json
{
  "text": "AWS FinOps Digest: $47,293 MTD, 1 anomalies, $1,212/mo savings identified",
  "blocks": [
    { "type": "header",
      "text": { "type": "plain_text", "text": "☁️ AWS FinOps Digest — July 05, 2026" } },
    { "type": "section",
      "fields": [
        { "type": "mrkdwn", "text": "*MTD Spend*\n$47,293" },
        { "type": "mrkdwn", "text": "*Month Forecast*\n$61,800" },
        { "type": "mrkdwn", "text": "*Savings Identified*\n$1,212/mo" },
        { "type": "mrkdwn", "text": "*Anomalies*\n1 (1 critical)" } ] },
    { "type": "divider" },
    { "type": "section",
      "text": { "type": "mrkdwn",
        "text": "*Cost Anomalies*\n🔴 *Amazon EC2*: $8,200 on 2026-07-03 (+43% vs baseline, z=6.7)\n    ↳ _USAGE_TYPE=BoxUsage:c5.2xlarge: $2,600.00 vs $310.00/day baseline (+$2,290.00, 93% of the spike)_" } },
    { "type": "section",
      "text": { "type": "mrkdwn",
        "text": "*Rightsizing* ($372/mo potential)\n• `analytics-worker`: c5.4xlarge → *c5.xlarge* — save $372/mo (P90 CPU 12%, HIGH)" } }
  ]
}
```

The digest renders as: header, MTD/forecast/savings/anomaly summary, top
anomalies with root-cause hints, waste findings, rightsizing recommendations
with monthly $ impact, and top services by spend.

### Scheduled Nightly Run (Lambda)

```bash
# Deploy as Lambda + EventBridge rule
cdk deploy FinOpsStack \
  --context slackWebhook=$SLACK_WEBHOOK \
  --context anthropicKey=$ANTHROPIC_API_KEY
```

---

## Project Structure

```
aws-finops-intelligence/
├── src/
│   └── finops/
│       ├── cli.py                    # `finops` entrypoint (click)
│       ├── pricing.py                # static on-demand price table + size ladder
│       ├── collectors/
│       │   └── cost_explorer.py      # daily spend, dimension breakdowns, forecast
│       ├── analyzers/
│       │   ├── anomaly.py            # median+MAD z-score, seasonality, root causes
│       │   ├── rightsizing.py        # EC2 P90-CPU downsize recommendations
│       │   ├── waste.py              # idle EC2, unattached EBS, EIPs, snapshots, RDS
│       │   ├── forecast.py           # linear-regression spend projection
│       │   ├── budgets.py            # AWS Budgets management
│       │   ├── savings_plans.py      # SP coverage + purchase recommendations
│       │   └── aws_anomaly_detection.py  # AWS Cost Anomaly Detection API
│       ├── ai/
│       │   └── explainer.py          # Claude explanations + Terraform remediation
│       └── reporters/
│           ├── slack.py              # Block Kit digest (webhook / bot token / dry-run)
│           └── terraform.py
└── tests/                            # pytest: moto + stubbed boto3 clients
```

---

## Multi-Account Support

Supports AWS Organizations via assume-role chaining. Configure accounts in `config.yaml`:

```yaml
accounts:
  - name: production
    account_id: "123456789012"
    role: arn:aws:iam::123456789012:role/FinOpsReadRole
  - name: staging
    account_id: "987654321098"
    role: arn:aws:iam::987654321098:role/FinOpsReadRole

thresholds:
  anomaly_zscore: 2.5
  idle_cpu_percent: 5
  idle_days: 7
```

---

## Roadmap

- [x] EC2 waste detection
- [x] EBS unattached volume detection
- [x] Cost anomaly detection (robust MAD z-score + seasonality + root-cause hints)
- [x] EC2 rightsizing (P90 CPU based)
- [x] Slack daily digest (webhook + bot token, dry-run)
- [x] Savings Plan gap analysis
- [ ] RDS rightsizing
- [ ] Terraform remediation auto-PR
- [ ] Grafana dashboard export
- [ ] Multi-account Organizations support (per-account anomaly grouping done)
- [ ] Bedrock model option (no external API)

---

## License

MIT — see [LICENSE](LICENSE).
