#!/usr/bin/env python3
"""Stop a running instance without destroying it.

Cost notes surfaced to the user:
- AWS: compute billing stops; EBS storage keeps billing until terminated.
- Vast: GPU billing stops; disk storage keeps billing until destroyed, and a
  restart is NOT guaranteed — the host may rent your GPUs to someone else.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cloudops.bootstrap import require_deps

require_deps()  # exits 4 with install.sh instructions if deps are missing

from cloudops import config, render
from cloudops.providers import CloudOpsError, get_provider

COST_NOTE = {
    "aws": "Compute billing stopped; EBS storage still bills until the instance is terminated.",
    "vast": ("GPU billing stopped; disk storage still bills until destroyed. Restart is not "
             "guaranteed — the host may rent these GPUs to someone else (clone_instance can "
             "recreate the setup elsewhere, without disk contents)."),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Stop a running cloud instance (keeps its disk)")
    parser.add_argument("--provider", choices=["aws", "vast"], required=True)
    parser.add_argument("--id", required=True, help="instance id (i-... for AWS, numeric for Vast)")
    parser.add_argument("--region", help="AWS region (default: your AWS config)")
    parser.add_argument("--force", action="store_true",
                        help="allow acting on an AWS instance this skill did not create")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

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
                    "Refusing to stop without confirmation (it interrupts running work): "
                    "confirm with the user, then re-run with --yes."
                )
            answer = input(
                f"Stop {instance.provider} {instance.id} ({instance.instance_type}, "
                f"currently {instance.status})? Type 'yes': "
            )
            if answer.strip().lower() != "yes":
                print("Aborted.")
                return 3
        provider.stop(args.id)
    except CloudOpsError as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}))
        else:
            render.warn(str(exc))
        return 1

    config.audit("stopped", provider=args.provider, instance_id=args.id)
    note = COST_NOTE[args.provider]
    if args.json:
        print(json.dumps({"stopped": args.id, "provider": args.provider, "note": note}))
    else:
        render.console.print(f"[green]Stop requested[/green] for {args.provider} {args.id}. {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
