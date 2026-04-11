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


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
