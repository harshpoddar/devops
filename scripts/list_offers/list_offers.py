#!/usr/bin/env python3
"""List available instance types / GPU offers with pricing, filterable by
hardware requirements and price. See SKILL.md."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cloudops.bootstrap import require_deps

require_deps()  # exits 4 with install.sh instructions if deps are missing

from cloudops import render
from cloudops.providers import OfferFilter, resolve_providers


def main() -> int:
    parser = argparse.ArgumentParser(description="Search purchasable instance types / GPU offers with prices")
    parser.add_argument("--provider", choices=["aws", "vast", "all"], default="all")
    parser.add_argument("--region", help="AWS region (default: your AWS config)")
    parser.add_argument("--gpus", type=int, help="minimum number of GPUs")
    parser.add_argument("--gpu-type", help='GPU model substring, e.g. "A100", "RTX 4090", "T4"')
    parser.add_argument("--cuda", type=float, dest="min_cuda", metavar="VER",
                        help="minimum host CUDA version, e.g. 12.8 (Vast only — on AWS "
                             "CUDA comes from the AMI, not the hardware)")
    parser.add_argument("--min-vcpus", type=int, help="minimum vCPUs")
    parser.add_argument("--min-memory", type=float, help="minimum RAM in GB")
    parser.add_argument("--max-hourly", type=float, help="maximum on-demand price in USD/hour")
    parser.add_argument("--limit", type=int, default=15, help="max offers per provider (default 15)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    filters = OfferFilter(
        min_vcpus=args.min_vcpus,
        min_memory_gb=args.min_memory,
        min_gpus=args.gpus,
        gpu_type=args.gpu_type,
        min_cuda=args.min_cuda,
        max_hourly_usd=args.max_hourly,
        limit=args.limit,
    )

    offers, errors = [], []
    for name, provider, err in resolve_providers(args.provider, region=args.region):
        if err:
            errors.append({"provider": name, "error": err})
            continue
        try:
            offers.extend(provider.list_offers(filters))
        except Exception as exc:
            errors.append({"provider": name, "error": str(exc)})
    offers.sort(key=lambda o: (o.hourly_usd is None, o.hourly_usd or 0))

    if args.json:
        print(json.dumps({"offers": [o.to_dict() for o in offers], "errors": errors}, indent=2))
    else:
        render.print_offers(offers)
        for e in errors:
            render.warn(f"{e['provider']}: {e['error']}")
    return 0 if (not errors or offers) else 1


if __name__ == "__main__":
    sys.exit(main())
