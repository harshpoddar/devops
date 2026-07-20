"""`cloudops spawn` — spawn an instance. ALWAYS quotes the cost first and requires
approval.

Contract (see SKILL.md):
  1. Run with --quote to price the spawn without creating anything.
  2. Show the user the cost. Only after they explicitly approve, re-run with --yes.
  3. Interactive humans get a y/N prompt instead; non-TTY runs without --yes are refused.

Exit codes: 0 created, 2 cost guard (--max-hourly) exceeded, 3 approval missing/denied.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from .. import render
from ..providers import CloudOpsError, get_provider
from ..spawn_flow import make_ssh_verify_post_spawn, run_spawn_flow


def main(argv: "Optional[list[str]]" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cloudops spawn",
        description="Spawn a cloud instance (cost quote + approval required)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--provider", choices=["aws", "vast"], required=True)
    # AWS
    parser.add_argument("--type", dest="instance_type", help="AWS instance type, e.g. t3.medium, g5.xlarge")
    parser.add_argument("--region", help="AWS region (default: your AWS config)")
    parser.add_argument("--ami", help="AMI id (default: AL2023, or Deep Learning AMI for GPU types)")
    parser.add_argument("--key-name", help="EC2 key pair name for SSH")
    parser.add_argument("--security-group", action="append", dest="security_groups",
                        help="security group id (repeatable)")
    parser.add_argument("--subnet-id")
    parser.add_argument("--ttl-hours", type=float,
                        help="intended lifetime — recorded as a tag for reaping/review, not auto-enforced yet")
    # Vast
    parser.add_argument("--offer-id", help="Vast.ai offer id from `cloudops offers`")
    parser.add_argument("--gpu-type", help='auto-pick cheapest Vast offer matching this GPU, e.g. "RTX 4090"')
    parser.add_argument("--gpus", type=int, help="minimum GPUs when auto-picking a Vast offer")
    parser.add_argument("--cuda", type=float, metavar="VER",
                        help="Vast: require host CUDA >= VER (e.g. 13). Filters auto-picks "
                             "and rejects an explicit --offer-id below it.")
    parser.add_argument("--image", help="docker image for Vast (default pytorch/pytorch:latest)")
    parser.add_argument("--onstart", help="shell command to run on start (Vast)")
    # Common
    parser.add_argument("--open-port", type=int, action="append", dest="open_ports", metavar="PORT",
                        help="expose a TCP port (repeatable). AWS: dedicated security group, open "
                             "to 0.0.0.0/0. Vast: Docker port mapped to a random public host port.")
    parser.add_argument("--disk", type=int, dest="disk_gb", help="root/scratch disk GB (aws default 30, vast 20)")
    parser.add_argument("--name", help="instance name / label")
    # SSH verification (Vast): after creating, poll an actual SSH login before
    # declaring success, so we never hand over a box the key hasn't reached yet.
    parser.add_argument("--ssh-key", help="path to the SSH public key to register/attach and verify "
                                          "with (default: ~/.ssh/id_ed25519.pub, then id_rsa.pub)")
    parser.add_argument("--ssh-wait-timeout", type=int, default=720, metavar="SEC",
                        help="Vast: max seconds to wait for SSH login to work before reporting "
                             "(default 720; Vast key injection can lag minutes)")
    parser.add_argument("--no-ssh-wait", action="store_true",
                        help="skip the post-spawn SSH login check (report as soon as created)")
    parser.add_argument("--max-hourly", type=float, help="hard guard: abort if the quote exceeds this USD/hour")
    parser.add_argument("--quote", action="store_true", help="print the cost quote and exit — creates nothing")
    parser.add_argument("--yes", action="store_true",
                        help="skip the approval prompt (only after the user approved the quoted cost)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    if args.provider == "aws" and not args.instance_type:
        parser.error("--type is required for --provider aws (e.g. --type g5.xlarge)")
    if args.provider == "vast" and not (args.offer_id or args.gpu_type):
        parser.error("--offer-id or --gpu-type is required for --provider vast")

    spec = {
        "instance_type": args.instance_type,
        "ami": args.ami,
        "key_name": args.key_name,
        "security_group_ids": args.security_groups,
        "subnet_id": args.subnet_id,
        "ttl_hours": args.ttl_hours,
        "offer_id": args.offer_id,
        "gpu_type": args.gpu_type,
        "gpus": args.gpus,
        "cuda": args.cuda,
        "image": args.image,
        "onstart": args.onstart,
        "disk_gb": args.disk_gb,
        "name": args.name,
        "max_hourly": args.max_hourly,
        "open_ports": args.open_ports,
        "ssh_pubkey_path": args.ssh_key,
    }

    try:
        provider = get_provider(args.provider, region=args.region)
    except CloudOpsError as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}))
        else:
            render.warn(str(exc))
        return 1

    post_spawn = None
    if not args.no_ssh_wait:
        if not args.json and not args.quote:
            render.console.print(
                f"[dim]After creation, will verify SSH login (up to {args.ssh_wait_timeout}s) "
                "before reporting success…[/dim]"
            )
        post_spawn = make_ssh_verify_post_spawn(
            provider,
            timeout_seconds=args.ssh_wait_timeout,
            pubkey_path=args.ssh_key,
            as_json=args.json,
        )

    return run_spawn_flow(
        provider,
        args.provider,
        spec,
        quote_only=args.quote,
        yes=args.yes,
        max_hourly=args.max_hourly,
        as_json=args.json,
        post_spawn=post_spawn,
    )


if __name__ == "__main__":
    sys.exit(main())
