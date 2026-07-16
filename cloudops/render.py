"""Rich-based table rendering shared by the scripts and the interactive CLI."""
from __future__ import annotations

from typing import Optional

from rich import box
from rich.console import Console
from rich.table import Table

console = Console()

_STATUS_STYLES = {
    "running": "green",
    "pending": "yellow",
    "loading": "yellow",
    "created": "yellow",
    "stopping": "yellow",
    "stopped": "red",
    "exited": "red",
}


def money(value: Optional[float], decimals: int = 2) -> str:
    return f"${value:,.{decimals}f}" if value is not None else "—"


def warn(message: str) -> None:
    console.print(f"[yellow]! {message}[/yellow]")


def _status(status: str) -> str:
    style = _STATUS_STYLES.get(status, "white")
    return f"[{style}]{status}[/{style}]"


def print_instances(instances) -> None:
    if not instances:
        console.print("[dim]No instances found.[/dim]")
        return
    table = Table(title=f"Instances ({len(instances)})", box=box.SIMPLE_HEAVY, header_style="bold")
    for col in ("Provider", "ID", "Name", "Status", "Type", "Region", "IP / SSH", "$/hr", "Launched", "Managed"):
        table.add_column(col, overflow="fold")
    for i in instances:
        table.add_row(
            i.provider,
            i.id,
            i.name or "—",
            _status(i.status),
            i.instance_type,
            i.region or "—",
            i.ip or "—",
            money(i.hourly_usd, 3),
            (i.launched_at or "—")[:16],
            "yes" if i.managed else "no",
        )
    console.print(table)


def print_offers(offers) -> None:
    if not offers:
        console.print("[dim]No offers match those filters.[/dim]")
        return
    table = Table(title=f"Offers ({len(offers)})", box=box.SIMPLE_HEAVY, header_style="bold")
    for col in ("Provider", "ID / Type", "GPUs", "GPU", "VRAM", "vCPU", "RAM", "$/hr", "~$/mo", "Region", "Notes"):
        table.add_column(col, overflow="fold")
    for o in offers:
        monthly = o.hourly_usd * 730 if o.hourly_usd is not None else None
        notes = []
        if o.extra.get("reliability"):
            notes.append(f"rel {o.extra['reliability']:.2f}")
        if o.extra.get("download_mbps"):
            notes.append(f"↓{o.extra['download_mbps']} Mbps")
        table.add_row(
            o.provider,
            o.id,
            str(o.gpus) if o.gpus else "—",
            o.gpu_type or "—",
            f"{o.gpu_memory_gb:.0f}G" if o.gpu_memory_gb else "—",
            f"{o.vcpus:g}" if o.vcpus else "—",
            f"{o.memory_gb:.0f}G" if o.memory_gb else "—",
            money(o.hourly_usd, 3),
            money(monthly, 0),
            o.region or "—",
            ", ".join(notes) or "—",
        )
    console.print(table)


def print_usage(usages: "list[dict]") -> None:
    table = Table(title="Overall usage", box=box.SIMPLE_HEAVY, header_style="bold")
    for col in ("Provider", "Running", "Burn $/hr", "Month-to-date", "Balance"):
        table.add_column(col)
    for u in usages:
        if u.get("error"):
            table.add_row(u["provider"], "—", "—", "—", f"[red]{u['error'][:70]}[/red]")
            continue
        table.add_row(
            u["provider"],
            str(u.get("running_instances", "—")),
            money(u.get("burn_usd_per_hour"), 3),
            money(u.get("month_to_date_usd")),
            money(u.get("balance_usd")),
        )
    console.print(table)
    for u in usages:
        if u.get("by_service"):
            sub = Table(title="AWS month-to-date by service", box=box.SIMPLE, header_style="bold")
            sub.add_column("Service")
            sub.add_column("USD", justify="right")
            for row in u["by_service"]:
                sub.add_row(row["service"], money(row["usd"]))
            console.print(sub)
        if u.get("cost_explorer_error"):
            warn(f"aws cost explorer: {u['cost_explorer_error'][:150]}")


def print_quote(quote) -> None:
    table = Table(title="Cost quote", box=box.SIMPLE_HEAVY, show_header=False)
    table.add_column("field", style="bold")
    table.add_column("value", overflow="fold")
    table.add_row("Provider", quote.provider)
    table.add_row("What", quote.description)
    table.add_row("Hourly", money(quote.hourly_usd, 4))
    table.add_row("~Monthly (730 h)", money(quote.monthly_usd))
    for key, value in quote.details.items():
        table.add_row(key, str(value) if value is not None else "—")
    console.print(table)
