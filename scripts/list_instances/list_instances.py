#!/usr/bin/env python3
"""List instances across providers (AWS EC2 + Vast.ai). See SKILL.md."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cloudops.bootstrap import require_deps

require_deps()  # exits 4 with install.sh instructions if deps are missing

from cloudops import render
from cloudops.providers import resolve_providers


def main() -> int:
    parser = argparse.ArgumentParser(description="List instances across cloud providers")
    parser.add_argument("--provider", choices=["aws", "vast", "all"], default="all")
    parser.add_argument("--region", help="AWS region (default: your AWS config)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    instances, errors = [], []
    for name, provider, err in resolve_providers(args.provider, region=args.region):
        if err:
            errors.append({"provider": name, "error": err})
            continue
        try:
            instances.extend(provider.list_instances())
        except Exception as exc:
            errors.append({"provider": name, "error": str(exc)})

    if args.json:
        print(json.dumps({"instances": [i.to_dict() for i in instances], "errors": errors}, indent=2))
    else:
        render.print_instances(instances)
        for e in errors:
            render.warn(f"{e['provider']}: {e['error']}")
    return 0 if (not errors or instances) else 1


if __name__ == "__main__":
    sys.exit(main())
