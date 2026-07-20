"""`cloudops usage` — account-level metrics: AWS month-to-date spend by service
(Cost Explorer; each query costs ~$0.01), running-instance burn rate, and Vast.ai
credit balance."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from .. import render
from ..providers import resolve_providers


def main(argv: "Optional[list[str]]" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cloudops usage", description="Account-level usage and spend metrics")
    parser.add_argument("--provider", choices=["aws", "vast", "all"], default="all")
    parser.add_argument("--region", help="AWS region (default: your AWS config)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    usages = []
    for name, provider, err in resolve_providers(args.provider, region=args.region):
        if err:
            usages.append({"provider": name, "error": err})
            continue
        try:
            usages.append(provider.usage())
        except Exception as exc:
            usages.append({"provider": name, "error": str(exc)})

    if args.json:
        print(json.dumps({"usage": usages}, indent=2))
    else:
        render.print_usage(usages)
    return 0 if any("error" not in u for u in usages) else 1


if __name__ == "__main__":
    sys.exit(main())
