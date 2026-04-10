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
- Compares daily spend against rolling 30-day baseline per service/account
- Uses statistical thresholding (Z-score + IQR) to flag outliers
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
- EC2: Recommends instance family/size based on CPU, memory, network P90 metrics
- RDS: Multi-AZ necessity check, read replica utilization
- EKS: Node group rightsizing based on pod resource requests vs. node capacity

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
cp config/config.example.yaml config/config.yaml

# Configure AWS profiles and thresholds in config.yaml
# Then run:
finops analyze --account prod --output slack,html
```

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
│       ├── collectors/       # AWS API data collection
│       │   ├── cost_explorer.py
│       │   ├── cloudwatch.py
│       │   └── resource_apis.py
│       ├── analyzers/        # Detection logic
│       │   ├── anomaly.py    # Statistical anomaly detection
│       │   ├── waste.py      # Waste identification rules
│       │   └── rightsizing.py
│       ├── ai/               # Claude API integration
│       │   ├── explainer.py  # Natural language explanations
│       │   └── remediator.py # Terraform snippet generation
│       └── reporters/        # Output formatters
│           ├── slack.py
│           ├── html.py
│           └── terraform.py
├── cdk/                      # Lambda deployment CDK stack
├── config/
│   └── config.example.yaml
└── tests/
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
- [x] Cost anomaly detection
- [x] Slack daily digest
- [ ] RDS rightsizing
- [ ] Savings Plan gap analysis
- [ ] Terraform remediation auto-PR
- [ ] Grafana dashboard export
- [ ] Multi-account Organizations support
- [ ] Bedrock model option (no external API)

---

## License

MIT — see [LICENSE](LICENSE).
