"""finops CLI — analyze AWS costs, detect waste, and generate AI-powered insights."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

console = Console()


@click.group()
@click.version_option(version="0.1.0")
def cli() -> None:
    """AWS FinOps Intelligence — AI-powered cost analysis and waste detection."""
    pass


@cli.command()
@click.option("--days", default=30, show_default=True, help="Look-back window in days")
@click.option("--top", default=10, show_default=True, help="Number of top services to show")
@click.option("--slack", is_flag=True, help="Post digest to Slack")
@click.option("--json-output", is_flag=True, help="Output as JSON instead of rich text")
def analyze(days: int, top: int, slack: bool, json_output: bool) -> None:
    """Run full cost analysis: spend summary, anomalies, and waste detection."""
    from finops.collectors.cost_explorer import CostExplorerCollector
    from finops.analyzers.anomaly import detect_anomalies, detect_service_trend_anomalies
    from finops.analyzers.waste import WasteDetector
    from finops.reporters.slack import post_daily_digest

    with console.status("[bold green]Fetching AWS cost data..."):
        collector = CostExplorerCollector()
        daily_spend = collector.get_daily_spend(days=days)
        service_spend = collector.get_service_spend_summary()
        forecast = collector.get_cost_forecast()

    total_mtd = sum(d.amount for d in daily_spend if d.date >= datetime.now().replace(day=1).date().isoformat())

    with console.status("[bold yellow]Detecting anomalies..."):
        anomalies = detect_anomalies(daily_spend)
        trend_anomalies = detect_service_trend_anomalies(service_spend)
        all_anomalies = anomalies + trend_anomalies

    with console.status("[bold red]Scanning for waste..."):
        detector = WasteDetector()
        waste_items = (
            detector.find_idle_ec2_instances()
            + detector.find_unattached_ebs_volumes()
            + detector.find_unassociated_elastic_ips()
            + detector.find_old_ebs_snapshots()
            + detector.find_oversized_rds()
        )

    top_services = sorted(service_spend, key=lambda s: s.amount_last_30d, reverse=True)[:top]

    if json_output:
        result = {
            "total_mtd": total_mtd,
            "forecast": forecast,
            "anomaly_count": len(all_anomalies),
            "waste_items": len(waste_items),
            "waste_monthly_cost": sum(w.estimated_monthly_cost for w in waste_items),
        }
        click.echo(json.dumps(result, indent=2))
        return

    _print_summary(total_mtd, forecast, all_anomalies, waste_items, top_services)

    if slack:
        with console.status("Posting to Slack..."):
            ok = post_daily_digest(total_mtd, forecast, all_anomalies, waste_items, top_services)
        if ok:
            console.print("[green]✓ Posted to Slack[/green]")
        else:
            console.print("[yellow]⚠ Slack post skipped (SLACK_WEBHOOK_URL not set)[/yellow]")


@cli.command()
@click.option("--service", required=True, help="AWS service name (e.g. Amazon EC2)")
@click.option("--days", default=7, show_default=True, help="Days to analyze")
def explain(service: str, days: int) -> None:
    """Get an AI explanation of cost anomalies for a specific service."""
    from finops.collectors.cost_explorer import CostExplorerCollector
    from finops.analyzers.anomaly import detect_anomalies, CostAnomaly
    from finops.ai.explainer import explain_anomaly

    with console.status(f"Fetching data for {service}..."):
        collector = CostExplorerCollector()
        daily_spend = collector.get_daily_spend(days=days)

    service_spend = [d for d in daily_spend if d.service == service]
    if not service_spend:
        console.print(f"[red]No spend data found for service: {service}[/red]")
        sys.exit(1)

    anomalies = detect_anomalies(service_spend)
    if not anomalies:
        console.print(f"[green]No anomalies detected for {service} in the last {days} days.[/green]")
        return

    anomaly = anomalies[0]
    with console.status("[bold]Calling Claude for AI analysis..."):
        explanation = explain_anomaly(anomaly)

    console.print(Panel(
        explanation,
        title=f"[bold]AI Analysis: {service} Cost Anomaly[/bold]",
        border_style="red" if anomaly.severity == "CRITICAL" else "yellow",
    ))


@cli.command()
@click.option("--service", help="Filter by AWS service type (ec2, rds, ebs)")
@click.option("--min-savings", default=50.0, show_default=True, help="Minimum monthly savings to report (USD)")
@click.option("--terraform", is_flag=True, help="Generate Terraform remediation snippets via AI")
def waste(service: Optional[str], min_savings: float, terraform: bool) -> None:
    """Scan for idle and over-provisioned resources."""
    from finops.analyzers.waste import WasteDetector
    from finops.ai.explainer import generate_remediation_terraform

    detector = WasteDetector()

    with console.status("Scanning for waste..."):
        items = []
        if not service or service == "ec2":
            items += detector.find_idle_ec2_instances()
        if not service or service == "ebs":
            items += detector.find_unattached_ebs_volumes()
            items += detector.find_old_ebs_snapshots()
        if not service or service == "rds":
            items += detector.find_oversized_rds()
        if not service:
            items += detector.find_unassociated_elastic_ips()

    items = [i for i in items if i.estimated_monthly_cost >= min_savings]
    items.sort(key=lambda i: i.estimated_monthly_cost, reverse=True)

    if not items:
        console.print("[green]No waste items found meeting the savings threshold.[/green]")
        return

    total = sum(i.estimated_monthly_cost for i in items)

    table = Table(title=f"Waste Items — ${total:,.0f}/mo potential savings", box=box.ROUNDED)
    table.add_column("Category", style="cyan")
    table.add_column("Resource ID", style="white")
    table.add_column("$/mo", justify="right", style="red")
    table.add_column("Reason", style="yellow")

    for item in items:
        table.add_row(
            item.category,
            item.resource_id,
            f"${item.estimated_monthly_cost:,.0f}",
            item.reason,
        )
    console.print(table)

    if terraform:
        console.print("\n[bold]Generating Terraform remediation...[/bold]")
        for item in items[:3]:  # Limit AI calls
            with console.status(f"Generating fix for {item.resource_id}..."):
                tf = generate_remediation_terraform(item)
            console.print(Panel(tf, title=f"Remediation: {item.resource_id}", border_style="blue"))


@cli.command()
@click.option("--days", default=45, show_default=True, help="Days of history to use for projection")
@click.option("--horizon", default=30, show_default=True, help="Days forward to forecast (30 or 90)")
@click.option("--top", default=10, show_default=True, help="Show top N services by projected spend")
@click.option("--json-output", is_flag=True, help="Output as JSON")
@click.option("--output", default=None, help="Write Markdown report to this file")
def forecast(days: int, horizon: int, top: int, json_output: bool, output) -> None:
    """Project future AWS spend using linear regression on historical data."""
    from finops.collectors.cost_explorer import CostExplorerCollector
    from finops.analyzers.forecast import forecast_costs, format_forecast_report

    with console.status(f"[bold green]Fetching {days} days of spend data..."):
        collector = CostExplorerCollector()
        daily_spend = collector.get_daily_spend(days=days)

    if not daily_spend:
        console.print("[red]No spend data returned from Cost Explorer.[/red]")
        sys.exit(1)

    with console.status("[bold yellow]Running forecast model..."):
        fc = forecast_costs(daily_spend, forecast_days=max(horizon, 90))

    if json_output:
        click.echo(json.dumps({
            "forecast_date": str(fc.forecast_date),
            "total_current_monthly": fc.total_current_monthly,
            "total_projected_30d": fc.total_projected_30d,
            "total_projected_90d": fc.total_projected_90d,
            "month_over_month_change_pct": fc.month_over_month_change_pct,
            "accelerating_services": fc.accelerating_services,
            "services": [
                {
                    "service": s.service,
                    "current_daily_avg": s.current_daily_avg,
                    "projected_30d": s.projected_30d,
                    "trend": s.trend,
                    "confidence": s.confidence,
                    "anomalous": s.anomalous,
                }
                for s in fc.services[:top]
            ],
        }, indent=2))
        return

    # Rich table output
    sign = "+" if fc.month_over_month_change_pct >= 0 else ""
    color = "red" if fc.month_over_month_change_pct > 10 else "green" if fc.month_over_month_change_pct < 0 else "yellow"

    console.print(Panel(
        f"[bold]Current est. monthly:[/bold]  ${fc.total_current_monthly:>12,.2f}\n"
        f"[bold]Projected 30 days:[/bold]     ${fc.total_projected_30d:>12,.2f}  "
        f"[{color}]({sign}{fc.month_over_month_change_pct:.1f}%)[/{color}]\n"
        f"[bold]Projected 90 days:[/bold]     ${fc.total_projected_90d:>12,.2f}\n"
        f"[bold]History used:[/bold]         {fc.history_days:>12} days",
        title="[bold]AWS Cost Forecast[/bold]",
        border_style=color,
    ))

    if fc.accelerating_services:
        console.print(f"\n[bold red]⚠  Accelerating spend:[/bold red] {', '.join(fc.accelerating_services)}")

    table = Table(title=f"Top {top} Services — 30-Day Projection", box=box.ROUNDED)
    table.add_column("Service", style="cyan")
    table.add_column("Daily Avg", justify="right")
    table.add_column("30d Projected", justify="right", style="bold")
    table.add_column("Growth/day", justify="right")
    table.add_column("Trend")
    table.add_column("Confidence")

    trend_icons = {"growing": "📈", "shrinking": "📉", "stable": "➡️", "volatile": "〰️"}

    for s in fc.services[:top]:
        growth_color = "red" if s.daily_growth_rate > 3 else "green" if s.daily_growth_rate < -1 else "white"
        table.add_row(
            s.service,
            f"${s.current_daily_avg:,.2f}",
            f"${s.projected_30d:,.2f}",
            f"[{growth_color}]{s.daily_growth_rate:+.2f}%[/{growth_color}]",
            f"{trend_icons.get(s.trend, '')} {s.trend}",
            s.confidence,
        )
    console.print(table)

    if fc.warnings:
        for w in fc.warnings:
            console.print(f"[yellow]⚠  {w}[/yellow]")

    if output:
        report_md = format_forecast_report(fc)
        with open(output, "w") as f:
            f.write(report_md)
        console.print(f"\n[green]✓ Forecast report written to {output}[/green]")


@cli.command()
@click.option("--month", default=None, help="Month to report (YYYY-MM, default: current month)")
@click.option("--output", default="executive-summary.md", show_default=True, help="Output file path")
def report(month: Optional[str], output: str) -> None:
    """Generate an executive FinOps summary report."""
    from finops.collectors.cost_explorer import CostExplorerCollector
    from finops.analyzers.anomaly import detect_anomalies
    from finops.analyzers.waste import WasteDetector
    from finops.ai.explainer import generate_executive_summary

    with console.status("Gathering data for report..."):
        collector = CostExplorerCollector()
        daily_spend = collector.get_daily_spend(days=30)
        service_spend = collector.get_service_spend_summary()
        forecast = collector.get_cost_forecast()
        anomalies = detect_anomalies(daily_spend)
        detector = WasteDetector()
        waste_items = detector.find_idle_ec2_instances() + detector.find_unattached_ebs_volumes()

    total_mtd = sum(d.amount for d in daily_spend if d.date >= datetime.now().replace(day=1).date().isoformat())

    with console.status("Generating executive summary with Claude..."):
        summary = generate_executive_summary(
            total_mtd=total_mtd,
            forecast=forecast,
            anomalies=anomalies,
            waste_items=waste_items,
            top_services=sorted(service_spend, key=lambda s: s.amount_last_30d, reverse=True)[:5],
        )

    with open(output, "w") as f:
        f.write(summary)

    console.print(f"[green]✓ Report written to {output}[/green]")
    console.print(Panel(summary[:500] + "...", title="Preview", border_style="green"))


def _print_summary(total_mtd, forecast, anomalies, waste_items, top_services) -> None:
    total_waste = sum(w.estimated_monthly_cost for w in waste_items)
    critical = [a for a in anomalies if a.severity == "CRITICAL"]

    console.print(Panel(
        f"[bold]MTD Spend:[/bold]     ${total_mtd:>10,.0f}\n"
        f"[bold]Forecast:[/bold]      ${forecast:>10,.0f}\n"
        f"[bold]Waste (mo):[/bold]    ${total_waste:>10,.0f}\n"
        f"[bold]Anomalies:[/bold]     {len(anomalies):>10} ({len(critical)} critical)",
        title="AWS FinOps Digest",
        border_style="red" if critical else "yellow" if anomalies else "green",
    ))

    if anomalies:
        table = Table(title="Cost Anomalies", box=box.SIMPLE)
        table.add_column("Severity", style="bold")
        table.add_column("Service")
        table.add_column("Date")
        table.add_column("Amount", justify="right")
        table.add_column("Change", justify="right")
        for a in anomalies[:5]:
            color = "red" if a.severity == "CRITICAL" else "yellow" if a.severity == "HIGH" else "white"
            table.add_row(
                f"[{color}]{a.severity}[/{color}]",
                a.service, a.date,
                f"${a.amount:.0f}",
                f"+{a.change_pct:.0f}%",
            )
        console.print(table)

    if top_services:
        table = Table(title="Top Services (30d)", box=box.SIMPLE)
        table.add_column("Service")
        table.add_column("30d Spend", justify="right")
        table.add_column("Change", justify="right")
        for s in top_services:
            arrow = "↑" if s.change_pct > 5 else "↓" if s.change_pct < -5 else "→"
            color = "red" if s.change_pct > 30 else "green" if s.change_pct < -10 else "white"
            table.add_row(
                s.service,
                f"${s.amount_last_30d:,.0f}",
                f"[{color}]{arrow} {s.change_pct:+.0f}%[/{color}]",
            )
        console.print(table)


@cli.group()
def budgets() -> None:
    """Manage and monitor AWS Budgets."""
    pass


@budgets.command("list")
@click.option("--json-output", is_flag=True, help="Output as JSON")
@click.option("--critical-only", is_flag=True, help="Show only budgets at risk (WARNING or CRITICAL)")
def budgets_list(json_output: bool, critical_only: bool) -> None:
    """List all AWS budgets with spend status and health indicators."""
    from finops.analyzers.budgets import BudgetManager

    with console.status("[bold green]Fetching AWS Budgets..."):
        mgr = BudgetManager()
        all_budgets = mgr.list_budgets()

    if critical_only:
        all_budgets = [b for b in all_budgets if b.health != "OK"]

    if not all_budgets:
        console.print("[green]No budgets found (or none at risk).[/green]")
        return

    if json_output:
        click.echo(json.dumps([
            {
                "name": b.name,
                "health": b.health,
                "limit_usd": b.limit_amount,
                "actual_spend": b.actual_spend,
                "forecasted_spend": b.forecasted_spend,
                "pct_used": b.pct_used,
                "pct_forecasted": b.pct_forecasted,
                "days_remaining": b.days_remaining_in_period,
                "daily_allowance": round(b.burn_rate_daily, 2),
            }
            for b in all_budgets
        ], indent=2))
        return

    table = Table(
        title=f"AWS Budgets ({len(all_budgets)} total)",
        box=box.ROUNDED,
        show_header=True,
    )
    table.add_column("Budget", style="cyan", no_wrap=True)
    table.add_column("Limit (USD)", justify="right")
    table.add_column("Actual", justify="right")
    table.add_column("Forecast", justify="right")
    table.add_column("Used %", justify="right")
    table.add_column("Fcst %", justify="right")
    table.add_column("Days Left", justify="right")
    table.add_column("Health", justify="center")

    health_colors = {"OK": "green", "WARNING": "yellow", "CRITICAL": "red"}

    for b in all_budgets:
        color = health_colors[b.health]
        table.add_row(
            b.name,
            f"${b.limit_amount:,.0f}",
            f"${b.actual_spend:,.0f}",
            f"${b.forecasted_spend:,.0f}",
            f"[{color}]{b.pct_used:.1f}%[/{color}]",
            f"[{color}]{b.pct_forecasted:.1f}%[/{color}]",
            str(b.days_remaining_in_period) if b.days_remaining_in_period >= 0 else "—",
            f"[bold {color}]{b.health}[/bold {color}]",
        )

    console.print(table)

    critical = [b for b in all_budgets if b.health == "CRITICAL"]
    if critical:
        console.print("\n[bold red]⚠  CRITICAL budgets (forecast exceeds limit):[/bold red]")
        for b in critical:
            overage = b.forecasted_spend - b.limit_amount
            console.print(
                f"  • [bold]{b.name}[/bold]: forecast ${b.forecasted_spend:,.0f} "
                f"(+${overage:,.0f} over limit). "
                f"Allow ${b.burn_rate_daily:,.2f}/day to stay in budget."
            )


@budgets.command("history")
@click.argument("budget_name")
@click.option("--periods", default=6, show_default=True, help="Number of billing periods to show")
def budgets_history(budget_name: str, periods: int) -> None:
    """Show historical spend vs budget for a specific budget."""
    from finops.analyzers.budgets import BudgetManager

    with console.status(f"Fetching history for '{budget_name}'..."):
        mgr = BudgetManager()
        history = mgr.get_budget_history(budget_name, periods=periods)

    if not history:
        console.print(f"[yellow]No history found for budget '{budget_name}'.[/yellow]")
        return

    table = Table(title=f"Budget History: {budget_name}", box=box.ROUNDED)
    table.add_column("Period", style="cyan")
    table.add_column("Budgeted (USD)", justify="right")
    table.add_column("Actual (USD)", justify="right")
    table.add_column("Utilization", justify="right")
    table.add_column("Status", justify="center")

    for h in history:
        budgeted = h["budgeted"]
        actual = h["actual"]
        pct = (actual / budgeted * 100) if budgeted > 0 else 0
        over = actual > budgeted
        color = "red" if over else "green" if pct < 80 else "yellow"
        table.add_row(
            h["period"],
            f"${budgeted:,.0f}",
            f"${actual:,.0f}",
            f"[{color}]{pct:.1f}%[/{color}]",
            f"[{color}]{'OVER' if over else 'OK'}[/{color}]",
        )

    console.print(table)


@budgets.command("create")
@click.option("--name", required=True, help="Budget name")
@click.option("--limit", required=True, type=float, help="Monthly budget limit in USD")
@click.option("--alert-threshold", default=80.0, show_default=True, help="Alert when forecasted spend reaches this %% of limit")
@click.option("--email", default=None, help="Email address for budget alerts")
@click.option("--sns-topic", default=None, help="SNS topic ARN for budget alerts")
@click.option("--service-filter", default=None, help="Restrict budget to a specific AWS service (e.g. 'Amazon EC2')")
@click.option("--dry-run", is_flag=True, help="Show what would be created without actually creating")
def budgets_create(
    name: str,
    limit: float,
    alert_threshold: float,
    email: Optional[str],
    sns_topic: Optional[str],
    service_filter: Optional[str],
    dry_run: bool,
) -> None:
    """Create a monthly AWS Budget with optional alerts."""
    console.print(Panel(
        f"[bold]Name:[/bold]          {name}\n"
        f"[bold]Monthly Limit:[/bold] ${limit:,.2f} USD\n"
        f"[bold]Alert at:[/bold]      {alert_threshold}% of limit\n"
        f"[bold]Email:[/bold]         {email or '(none)'}\n"
        f"[bold]SNS Topic:[/bold]     {sns_topic or '(none)'}\n"
        f"[bold]Service Filter:[/bold] {service_filter or '(all services)'}",
        title="New Budget",
        border_style="blue",
    ))

    if dry_run:
        console.print("[yellow]Dry run — budget NOT created.[/yellow]")
        return

    from finops.analyzers.budgets import BudgetManager

    with console.status("Creating budget..."):
        mgr = BudgetManager()
        try:
            mgr.create_monthly_budget(
                name=name,
                limit_usd=limit,
                alert_threshold_pct=alert_threshold,
                email=email,
                sns_topic_arn=sns_topic,
                service_filter=service_filter,
            )
        except ValueError as e:
            console.print(f"[red]Error: {e}[/red]")
            raise SystemExit(1)

    console.print(f"[green]✓ Budget '{name}' created (${limit:,.2f}/mo, alert at {alert_threshold}%)[/green]")


@budgets.command("check")
@click.option("--slack", is_flag=True, help="Post alert to Slack if any budget is at risk")
@click.option("--fail-on-critical", is_flag=True, help="Exit non-zero if any budget is CRITICAL")
def budgets_check(slack: bool, fail_on_critical: bool) -> None:
    """CI-friendly budget health check. Exits non-zero on CRITICAL if --fail-on-critical."""
    from finops.analyzers.budgets import BudgetManager

    with console.status("Checking budget health..."):
        mgr = BudgetManager()
        all_budgets = mgr.list_budgets()

    critical = [b for b in all_budgets if b.health == "CRITICAL"]
    warning = [b for b in all_budgets if b.health == "WARNING"]

    if not critical and not warning:
        console.print("[green]✓ All budgets healthy[/green]")
        return

    for b in critical:
        console.print(
            f"[bold red]CRITICAL[/bold red] {b.name}: "
            f"forecast ${b.forecasted_spend:,.0f} / limit ${b.limit_amount:,.0f} "
            f"({b.pct_forecasted:.0f}%)"
        )
    for b in warning:
        console.print(
            f"[bold yellow]WARNING[/bold yellow] {b.name}: "
            f"forecast ${b.forecasted_spend:,.0f} / limit ${b.limit_amount:,.0f} "
            f"({b.pct_forecasted:.0f}%)"
        )

    if slack and (critical or warning):
        webhook = os.environ.get("SLACK_WEBHOOK_URL")
        if webhook:
            import urllib.request
            payload = json.dumps({
                "text": (
                    f":rotating_light: *AWS Budget Alert*\n"
                    + "\n".join(
                        f"• *{b.name}*: {b.pct_forecasted:.0f}% of ${b.limit_amount:,.0f} limit forecast"
                        for b in (critical + warning)
                    )
                )
            }).encode()
            try:
                urllib.request.urlopen(urllib.request.Request(
                    webhook, data=payload, headers={"Content-Type": "application/json"}
                ))
                console.print("[green]✓ Slack alert sent[/green]")
            except Exception as e:
                console.print(f"[yellow]Slack post failed: {e}[/yellow]")

    if fail_on_critical and critical:
        raise SystemExit(1)


@cli.group()
def anomalies() -> None:
    """AWS Cost Anomaly Detection — monitor and respond to ML-detected spend spikes."""


@anomalies.command("list")
@click.option("--days", default=30, show_default=True, help="Look-back window in days")
@click.option("--min-impact", default=0.0, show_default=True, help="Minimum dollar impact to show")
@click.option("--severity", type=click.Choice(["CRITICAL", "HIGH", "MEDIUM", "LOW"]), default=None)
@click.option("--json-output", is_flag=True, help="Output as JSON")
def anomalies_list(days: int, min_impact: float, severity: str | None, json_output: bool) -> None:
    """List AWS ML-detected cost anomalies."""
    from finops.analyzers.aws_anomaly_detection import AnomalyDetectionManager

    with console.status("[bold yellow]Fetching AWS anomaly detections..."):
        mgr = AnomalyDetectionManager()
        all_anomalies = mgr.get_anomalies(days_back=days, min_impact_dollars=min_impact)

    if severity:
        all_anomalies = [a for a in all_anomalies if a.severity == severity]

    if not all_anomalies:
        console.print("[green]No anomalies detected in the selected window.[/green]")
        return

    if json_output:
        import json
        console.print(json.dumps([
            {
                "id": a.anomaly_id,
                "dimension": a.dimension_value,
                "start": a.start_date,
                "end": a.end_date or "ongoing",
                "actual": a.anomalous_spend,
                "expected": a.expected_spend,
                "impact_pct": a.impact_percentage,
                "impact_usd": a.total_impact,
                "severity": a.severity,
                "root_causes": a.root_causes,
            }
            for a in all_anomalies
        ], indent=2))
        return

    severity_color = {"CRITICAL": "red", "HIGH": "yellow", "MEDIUM": "cyan", "LOW": "white"}

    table = Table(title=f"AWS Cost Anomalies (last {days}d)", box=box.ROUNDED)
    table.add_column("Severity", justify="center")
    table.add_column("Service / Dimension")
    table.add_column("Period")
    table.add_column("Actual (USD)", justify="right")
    table.add_column("Expected (USD)", justify="right")
    table.add_column("Impact", justify="right")
    table.add_column("Root Cause")

    for a in all_anomalies:
        color = severity_color.get(a.severity, "white")
        period = f"{a.start_date}"
        if a.end_date:
            period += f" → {a.end_date}"
        else:
            period += " → [bold]ongoing[/bold]"
        rc_str = ""
        if a.root_causes:
            rc = a.root_causes[0]
            parts = [v for v in [rc.get("service"), rc.get("region"), rc.get("usage_type")] if v]
            rc_str = " / ".join(parts[:2])
        table.add_row(
            f"[{color}]{a.severity}[/{color}]",
            a.dimension_value,
            period,
            f"${a.anomalous_spend:,.2f}",
            f"${a.expected_spend:,.2f}",
            f"[{color}]+${a.total_impact:,.2f} (+{a.impact_percentage:.0f}%)[/{color}]",
            rc_str,
        )

    console.print(table)
    ongoing = sum(1 for a in all_anomalies if a.is_ongoing)
    if ongoing:
        console.print(f"[bold red]{ongoing} anomaly(s) still ongoing[/bold red]")


@anomalies.command("setup")
@click.option("--email", default=None, help="Email address for anomaly alerts")
@click.option("--sns-arn", default=None, help="SNS topic ARN for alerts")
@click.option("--threshold", default=100.0, show_default=True, help="Dollar threshold to trigger alert")
@click.option("--frequency", type=click.Choice(["IMMEDIATELY", "DAILY", "WEEKLY"]), default="DAILY")
def anomalies_setup(email: str | None, sns_arn: str | None, threshold: float, frequency: str) -> None:
    """Create default anomaly monitors and alert subscription."""
    from finops.analyzers.aws_anomaly_detection import AnomalyDetectionManager

    mgr = AnomalyDetectionManager()

    with console.status("[bold yellow]Creating SERVICE anomaly monitor..."):
        monitor_arn = mgr.create_service_monitor("AllServicesMonitor")
    console.print(f"[green]✓ Monitor:[/green] {monitor_arn}")

    if email or sns_arn:
        with console.status("[bold yellow]Creating anomaly subscription..."):
            sub_arn = mgr.create_subscription(
                name="FinOps-Anomaly-Alerts",
                monitor_arns=[monitor_arn],
                email=email,
                sns_topic_arn=sns_arn,
                threshold_dollars=threshold,
                frequency=frequency,
            )
        console.print(f"[green]✓ Subscription:[/green] {sub_arn}")
        console.print(f"[green]✓ Alerts when anomaly exceeds ${threshold:,.0f} ({frequency})[/green]")
    else:
        console.print("[yellow]No email or SNS ARN provided — subscription not created.[/yellow]")
        console.print("Run with --email or --sns-arn to enable alerts.")


@anomalies.command("feedback")
@click.argument("anomaly_id")
@click.argument("feedback", type=click.Choice(["ANOMALY", "NOT_ANOMALY", "PLANNED_ACTIVITY"]))
def anomalies_feedback(anomaly_id: str, feedback: str) -> None:
    """Provide feedback to improve AWS anomaly detection accuracy.

    ANOMALY_ID: The anomaly ID (from 'anomalies list --json-output')
    FEEDBACK: ANOMALY (confirm) | NOT_ANOMALY (false positive) | PLANNED_ACTIVITY (expected)
    """
    from finops.analyzers.aws_anomaly_detection import AnomalyDetectionManager

    mgr = AnomalyDetectionManager()
    mgr.provide_feedback(anomaly_id, feedback)
    label = {
        "ANOMALY": "[red]confirmed as a real anomaly[/red]",
        "NOT_ANOMALY": "[green]marked as false positive[/green]",
        "PLANNED_ACTIVITY": "[cyan]marked as planned activity[/cyan]",
    }[feedback]
    console.print(f"[green]✓[/green] Anomaly {anomaly_id} {label} — ML model updated")


@cli.group()
def savings() -> None:
    """Savings Plans coverage analysis and purchase recommendations."""
    pass


@savings.command("coverage")
@click.option("--days", default=30, show_default=True, help="Look-back window in days")
@click.option("--target", default=80.0, show_default=True, help="Target coverage % for recommendations")
@click.option("--json-output", is_flag=True, help="Output raw JSON")
def savings_coverage(days: int, target: float, json_output: bool) -> None:
    """Show current Savings Plans coverage and identify uncovered spend."""
    from finops.analyzers.savings_plans import SavingsPlansAnalyzer

    with console.status("[bold green]Analyzing Savings Plans coverage..."):
        analyzer = SavingsPlansAnalyzer(lookback_days=days, coverage_target_pct=target)
        result = analyzer.analyze()

    if json_output:
        import dataclasses
        console.print_json(json.dumps({
            "account_id": result.account_id,
            "analysis_date": result.analysis_date,
            "avg_coverage_pct": result.avg_coverage_pct,
            "total_uncovered_monthly_usd": result.total_uncovered_monthly_usd,
            "coverage": dataclasses.asdict(result.coverage) if result.coverage else None,
        }))
        return

    # Coverage summary panel
    coverage = result.coverage
    if not coverage:
        console.print("[red]No coverage data available[/red]")
        return

    gap = target - result.avg_coverage_pct
    gap_color = "red" if gap > 20 else "yellow" if gap > 5 else "green"
    panel_text = (
        f"[bold]Coverage:[/bold] {result.avg_coverage_pct:.1f}%  "
        f"(target: {target:.0f}%  [{gap_color}]gap: {gap:+.1f}%[/{gap_color}])\n"
        f"[bold]SP Spend:[/bold]     ${coverage.sp_spend_usd:>12,.2f}\n"
        f"[bold]On-Demand:[/bold]    ${coverage.on_demand_spend_usd:>12,.2f}  "
        f"[dim](uncovered over {days} days)[/dim]\n"
        f"[bold]Total Eligible:[/bold] ${coverage.total_eligible_usd:>12,.2f}\n"
        f"\n[yellow]Potential savings from covering gap:[/yellow] "
        f"${coverage.potential_savings_usd:,.0f}/period (~20% SP discount on uncovered OD)"
    )
    console.print(Panel(panel_text, title="[bold cyan]Savings Plans Coverage[/bold cyan]", border_style="cyan"))

    # Utilization table
    if result.utilization:
        table = Table(title="Existing Savings Plans", box=box.ROUNDED)
        table.add_column("Plan ID", style="cyan")
        table.add_column("Type")
        table.add_column("Commitment/hr", justify="right")
        table.add_column("Used", justify="right")
        table.add_column("Unused", justify="right")
        table.add_column("Utilization %", justify="right")
        table.add_column("Net Savings", justify="right")
        for u in result.utilization:
            pct_color = "red" if u.utilization_pct < 80 else "green"
            table.add_row(
                u.plan_id[:12],
                u.plan_type.replace("SavingsPlans", " SP"),
                f"${u.commitment_usd / (24 * days):.4f}",
                f"${u.used_usd:,.0f}",
                f"[red]${u.unused_usd:,.0f}[/red]" if u.unused_usd > 100 else f"${u.unused_usd:,.0f}",
                f"[{pct_color}]{u.utilization_pct:.1f}%[/{pct_color}]",
                f"[green]${u.net_savings_usd:,.0f}[/green]",
            )
        console.print(table)

    if result.underutilized_plans:
        console.print(
            f"\n[yellow]⚠  {len(result.underutilized_plans)} plan(s) are underutilized (<80%): "
            f"{', '.join(result.underutilized_plans)}[/yellow]"
        )
        console.print("[dim]Consider reducing commitment at renewal to avoid paying for unused capacity.[/dim]")


@savings.command("recommend")
@click.option("--days", default=30, show_default=True, help="Look-back window in days")
@click.option("--target", default=80.0, show_default=True, help="Target coverage %")
@click.option("--top", default=3, show_default=True, help="Top N recommendations to show")
def savings_recommend(days: int, target: float, top: int) -> None:
    """Show purchase recommendations to improve Savings Plans coverage."""
    from finops.analyzers.savings_plans import SavingsPlansAnalyzer

    with console.status("[bold green]Fetching SP purchase recommendations..."):
        analyzer = SavingsPlansAnalyzer(lookback_days=days, coverage_target_pct=target)
        result = analyzer.analyze()

    recs = result.recommendations[:top]
    if not recs:
        console.print("[yellow]No recommendations available. Check that Cost Explorer is enabled.[/yellow]")
        return

    table = Table(title=f"Top {len(recs)} Savings Plan Recommendations", box=box.ROUNDED)
    table.add_column("Plan Type")
    table.add_column("Term", justify="center")
    table.add_column("Payment")
    table.add_column("Commitment/hr", justify="right")
    table.add_column("Monthly Savings", justify="right")
    table.add_column("Savings %", justify="right")
    table.add_column("ROI", justify="right")
    table.add_column("Break-Even", justify="right")

    for rec in recs:
        table.add_row(
            rec.plan_type.replace("SavingsPlans", " SP"),
            f"{rec.term_years}yr",
            rec.payment_option,
            f"${rec.hourly_commitment_usd:.4f}",
            f"[green]${rec.estimated_monthly_savings_usd:,.0f}[/green]",
            f"[green]{rec.estimated_savings_pct:.1f}%[/green]",
            f"{rec.estimated_roi_pct:.0f}%",
            f"{rec.estimated_break_even_months:.1f}mo" if rec.estimated_break_even_months > 0 else "[green]immediate[/green]",
        )
    console.print(table)

    best = result.top_recommendation()
    if best:
        console.print(
            f"\n[bold green]Best recommendation:[/bold green] {best.summary()}"
        )


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
