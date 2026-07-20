"""Unified `cloudops` CLI.

Bare `cloudops` opens the interactive menu (usage + instance tables). With a
subcommand it dispatches to the matching operation in ``cloudops.commands`` —
e.g. `cloudops spawn …`, `cloudops offers …`, `cloudops terminate …`. Every
subcommand also accepts ``--json`` and forwards its own ``--help``.

This is a proper pip console entry point, so it always runs under the skill's
own venv python (absolute-path shebang) — `source .venv/bin/activate && cloudops …`
just works.
"""
from __future__ import annotations

import importlib
import sys

from rich.panel import Panel

from . import render
from .providers import resolve_providers
from .render import console

# subcommand -> (module path, one-line help). Aliases below share a module.
_COMMANDS = {
    "instances": ("cloudops.commands.list_instances", "list running/stopped instances"),
    "offers": ("cloudops.commands.list_offers", "search instance types / GPU offers with prices"),
    "spawn": ("cloudops.commands.spawn_instance", "create an instance (quote + approval required)"),
    "start": ("cloudops.commands.start_instance", "start a stopped instance (billing resumes)"),
    "stop": ("cloudops.commands.stop_instance", "stop a running instance (keeps its disk)"),
    "clone": ("cloudops.commands.clone_instance", "clone an instance's config (--with-data for a replica)"),
    "terminate": ("cloudops.commands.terminate_instance", "destroy an instance"),
    "usage": ("cloudops.commands.account_metrics", "account spend / burn rate / balances"),
    "dashboard": ("cloudops.dashboard", "launch the read-only local web dashboard"),
}
_ALIASES = {"list": "instances", "ls": "instances", "metrics": "usage",
            "destroy": "terminate", "rm": "terminate"}


def _print_help() -> None:
    console.print(Panel("[bold]cloudops[/bold] — on-demand compute across AWS + Vast.ai",
                        subtitle="run `cloudops` with no args for the interactive menu"))
    console.print("\n[bold]Usage:[/bold] cloudops <command> [options]   "
                  "([dim]each command takes --help and --json[/dim])\n")
    for name, (_, blurb) in _COMMANDS.items():
        console.print(f"  [cyan]{name:<11}[/cyan] {blurb}")
    console.print("\n[dim]Aliases: list/ls→instances, metrics→usage, destroy/rm→terminate[/dim]")


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


def _interactive_menu() -> int:
    console.print(
        Panel(
            "[bold]cloudops[/bold] — on-demand compute across AWS + Vast.ai",
            subtitle="interactive CLI  ·  `cloudops --help` for subcommands",
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


def main(argv: "list[str] | None" = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return _interactive_menu()

    sub = argv[0]
    if sub in ("-h", "--help", "help"):
        _print_help()
        return 0

    sub = _ALIASES.get(sub, sub)
    if sub not in _COMMANDS:
        render.warn(f"Unknown command '{argv[0]}'.")
        _print_help()
        return 2

    module_name, _ = _COMMANDS[sub]
    mod = importlib.import_module(module_name)
    rest = argv[1:]
    if sub == "dashboard":
        if rest:
            render.warn("`cloudops dashboard` takes no arguments; ignoring: " + " ".join(rest))
        return mod.main()
    return mod.main(rest)


if __name__ == "__main__":
    sys.exit(main())
