"""The quote → approval → create flow shared by spawn_instance and clone_instance.

This is the cost-safety chokepoint: every path that creates a billed instance
goes through here. Exit codes: 0 created, 1 error, 2 cost guard exceeded,
3 approval missing or denied.
"""
from __future__ import annotations

import json
import sys
from typing import Callable, Optional

from . import config, render
from .providers import CloudOpsError, Provider


def run_spawn_flow(
    provider: Provider,
    provider_name: str,
    spec: dict,
    *,
    quote_only: bool = False,
    yes: bool = False,
    max_hourly: Optional[float] = None,
    as_json: bool = False,
    audit_fields: Optional[dict] = None,
    pre_spawn: Optional[Callable[[dict], None]] = None,
    post_spawn: Optional[Callable] = None,
) -> int:
    """pre_spawn(spec) runs after approval but before creation (may mutate the
    spec, e.g. swap in a freshly snapshotted AMI). post_spawn(result) runs after
    creation and returns a status string; its failure never fails the flow —
    the instance already exists and is reported."""
    try:
        quote = provider.quote(spec)
    except CloudOpsError as exc:
        if as_json:
            print(json.dumps({"error": str(exc)}))
        else:
            render.warn(str(exc))
        return 1

    if not as_json:
        render.print_quote(quote)

    if max_hourly is not None and quote.hourly_usd is not None and quote.hourly_usd > max_hourly:
        msg = f"Quote ${quote.hourly_usd}/hr exceeds --max-hourly ${max_hourly} — aborting."
        print(json.dumps({"quote": quote.to_dict(), "error": msg}) if as_json else msg)
        return 2

    if quote_only:
        if as_json:
            print(json.dumps({"quote": quote.to_dict()}, indent=2))
        return 0

    if not yes:
        if not sys.stdin.isatty():
            msg = ("Refusing to create without approval: show this quote to the user, "
                   "and re-run with --yes once they explicitly approve the cost.")
            print(json.dumps({"quote": quote.to_dict(), "error": msg}) if as_json else msg)
            return 3
        answer = input(
            f"Approve spending {render.money(quote.hourly_usd, 4)}/hr "
            f"(~{render.money(quote.monthly_usd)}/mo)? Type 'yes' to create: "
        )
        if answer.strip().lower() != "yes":
            print("Aborted — nothing was created.")
            return 3

    config.audit(
        "spawn_approved",
        provider=provider_name,
        quote=quote.to_dict(),
        approved_via="--yes flag" if yes else "interactive prompt",
        **(audit_fields or {}),
    )
    try:
        if pre_spawn:
            pre_spawn(spec)
        result = provider.spawn(spec)
    except CloudOpsError as exc:
        if as_json:
            print(json.dumps({"quote": quote.to_dict(), "error": str(exc)}))
        else:
            render.warn(str(exc))
        return 1
    config.audit(
        "spawn_created",
        provider=provider_name,
        instance_id=result.instance_id,
        hourly_usd=quote.hourly_usd,
        **(audit_fields or {}),
    )

    post_note = None
    if post_spawn:
        try:
            post_note = post_spawn(result)
        except Exception as exc:
            post_note = (f"WARNING: instance {result.instance_id} was created, but the "
                         f"post-create step failed: {exc}")

    if as_json:
        payload = {"quote": quote.to_dict(), "result": result.to_dict()}
        if post_note:
            payload["post_action"] = post_note
        print(json.dumps(payload, indent=2))
    else:
        if post_note:
            render.console.print(post_note)
        render.console.print(
            f"[green]Created[/green] {result.provider} instance [bold]{result.instance_id}[/bold] "
            f"({result.status}). {result.connect_hint}"
        )
        render.console.print(
            "[yellow]Remember:[/yellow] this bills until terminated — "
            "scripts/terminate_instance when done."
        )
    return 0
