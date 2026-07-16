#!/usr/bin/env python3
"""Terminate/destroy an instance. AWS instances not created by this skill
(missing the managed-by=cloudops-skill tag) require --force. See SKILL.md."""
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Terminate a cloud instance")
    parser.add_argument("--provider", choices=["aws", "vast"], required=True)
    parser.add_argument("--id", required=True, help="instance id (i-... for AWS, numeric for Vast)")
    parser.add_argument("--region", help="AWS region (default: your AWS config)")
    parser.add_argument("--force", action="store_true",
                        help="allow terminating an AWS instance this skill did not create")
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
                    "Refusing to terminate without confirmation: confirm with the user, "
                    "then re-run with --yes."
                )
            answer = input(
                f"Terminate {instance.provider} {instance.id} "
                f"({instance.instance_type}, {instance.status})? Type 'yes': "
            )
            if answer.strip().lower() != "yes":
                print("Aborted.")
                return 3
        provider.terminate(args.id)
    except CloudOpsError as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}))
        else:
            render.warn(str(exc))
        return 1

    config.audit("terminated", provider=args.provider, instance_id=args.id)
    if args.json:
        print(json.dumps({"terminated": args.id, "provider": args.provider}))
    else:
        render.console.print(f"[green]Terminated[/green] {args.provider} instance {args.id}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
