"""`cloudops start` — start a stopped instance. Billing resumes at the instance's rate.

Vast caveat: a start can fail if the host has rented your GPUs to someone else
while the instance was stopped — in that case use `cloudops clone` to recreate
the setup on another host (disk contents are not carried over).
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from .. import config, render
from ..providers import CloudOpsError, get_provider


def main(argv: "Optional[list[str]]" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cloudops start", description="Start a stopped cloud instance (billing resumes)")
    parser.add_argument("--provider", choices=["aws", "vast"], required=True)
    parser.add_argument("--id", required=True, help="instance id (i-... for AWS, numeric for Vast)")
    parser.add_argument("--region", help="AWS region (default: your AWS config)")
    parser.add_argument("--force", action="store_true",
                        help="allow acting on an AWS instance this skill did not create")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    try:
        provider = get_provider(args.provider, region=args.region)
        instance = provider.describe_instance(args.id)
        if instance is None:
            raise CloudOpsError(f"No {args.provider} instance with id {args.id} found.")
        if args.provider == "aws" and not instance.managed and not args.force:
            raise CloudOpsError(
                f"{args.id} was not created by this skill (no {config.MANAGED_TAG_KEY}="
                f"{config.MANAGED_TAG_VALUE} tag). Pass --force if you really mean it."
            )
        if not args.yes:
            if not sys.stdin.isatty():
                raise CloudOpsError(
                    "Refusing to start without confirmation (billing resumes): confirm "
                    "with the user, then re-run with --yes."
                )
            answer = input(
                f"Start {instance.provider} {instance.id} ({instance.instance_type}, "
                f"currently {instance.status})? Billing resumes. Type 'yes': "
            )
            if answer.strip().lower() != "yes":
                print("Aborted.")
                return 3
        provider.start(args.id)
    except CloudOpsError as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}))
        else:
            render.warn(str(exc))
        return 1

    config.audit("started", provider=args.provider, instance_id=args.id)
    hint = "Run `cloudops instances` to see its state and connection details."
    if args.provider == "vast":
        hint += (" If Vast reports no capacity, the host rented out your GPUs — "
                 "`cloudops clone` can recreate the setup on another host.")
    if args.json:
        print(json.dumps({"started": args.id, "provider": args.provider, "hint": hint}))
    else:
        render.console.print(f"[green]Start requested[/green] for {args.provider} {args.id}. {hint}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
