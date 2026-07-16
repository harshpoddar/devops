"""Interactive terminal CLI: overall usage + instance details, in tables."""
from __future__ import annotations

import sys

from rich.panel import Panel

from . import render
from .providers import resolve_providers
from .render import console


def _gather_usage() -> "list[dict]":
    usages = []
    for name, provider, err in resolve_providers("all"):
        if err:
            usages.append({"provider": name, "error": err})
            continue
        try:
            usages.append(provider.usage())
        except Exception as exc:
            usages.append({"provider": name, "error": str(exc)})
    return usages


def _gather_instances():
    instances, errors = [], []
    for name, provider, err in resolve_providers("all"):
        if err:
            errors.append((name, err))
            continue
        try:
            instances.extend(provider.list_instances())
        except Exception as exc:
            errors.append((name, str(exc)))
    return instances, errors


def main() -> int:
    console.print(
        Panel(
            "[bold]cloudops[/bold] — on-demand compute across AWS + Vast.ai",
            subtitle="interactive CLI",
        )
    )
    while True:
        console.print(
            "\n[bold cyan][1][/bold cyan] Overall usage   "
            "[bold cyan][2][/bold cyan] Instance details   "
            "[bold cyan][q][/bold cyan] Quit"
        )
        try:
            choice = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return 0
        if choice in ("q", "quit", "exit"):
            return 0
        if choice == "1":
            with console.status("Fetching usage from all providers..."):
                usages = _gather_usage()
            render.print_usage(usages)
        elif choice == "2":
            with console.status("Fetching instances from all providers..."):
                instances, errors = _gather_instances()
            render.print_instances(instances)
            for name, err in errors:
                render.warn(f"{name}: {err}")
        else:
            console.print("[dim]Pick 1, 2 or q.[/dim]")


if __name__ == "__main__":
    sys.exit(main())
