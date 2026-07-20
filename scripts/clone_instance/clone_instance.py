#!/usr/bin/env python3
"""Clone an existing instance: recreate its configuration (type/image/settings)
as a NEW instance. By default only the template is cloned; pass --with-data to
also replicate disk contents:

AWS --with-data: snapshots the source into an AMI (all attached EBS volumes)
and launches the clone from it — an exact replica. The AMI + snapshots persist
and bill storage until deregistered. No source downtime by default
(crash-consistent); --reboot-source for a filesystem-consistent snapshot.
Vast --with-data: spawns the clone, waits for it to boot, then Vast-side rsyncs
--data-path (default /workspace) from source to clone. Both must be reachable.

Vast without --with-data: rebuilds the same GPU count/model + image + disk on
the cheapest matching offer (or --offer-id) — useful when a stopped instance
can't restart because the host rented its GPUs out.

Creates a billed instance, so it follows the same contract as spawn_instance:
--quote first, then user approval, then --yes.
Exit codes: 0 created, 2 cost guard exceeded, 3 approval missing/denied.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cloudops.bootstrap import require_deps

require_deps()  # exits 4 with install.sh instructions if deps are missing

from cloudops import render
from cloudops.providers import CloudOpsError, get_provider
from cloudops.spawn_flow import make_ssh_verify_post_spawn, run_spawn_flow


def main() -> int:
    parser = argparse.ArgumentParser(description="Clone an instance's configuration into a new instance")
    parser.add_argument("--provider", choices=["aws", "vast"], required=True)
    parser.add_argument("--id", required=True, help="source instance id to clone")
    parser.add_argument("--region", help="AWS region (default: your AWS config)")
    parser.add_argument("--offer-id", help="Vast: target offer id (default: cheapest matching GPU)")
    parser.add_argument("--cuda", type=float, metavar="VER",
                        help="Vast: require host CUDA >= VER on the clone "
                             "(default: the source host's CUDA version)")
    parser.add_argument("--name", help="name for the clone (default: <source-name>-clone)")
    parser.add_argument("--disk", type=int, dest="disk_gb", help="override disk size GB")
    parser.add_argument("--with-data", action="store_true",
                        help="also replicate disk contents: AWS snapshots the source into an AMI "
                             "first; Vast copies --data-path to the clone after it boots")
    parser.add_argument("--data-path", default="/workspace",
                        help="Vast only: directory to copy with --with-data (default /workspace)")
    parser.add_argument("--reboot-source", action="store_true",
                        help="AWS only: reboot the source for a filesystem-consistent snapshot "
                             "(default: no reboot, crash-consistent)")
    parser.add_argument("--open-port", type=int, action="append", dest="open_ports", metavar="PORT",
                        help="expose a TCP port on the clone (repeatable; see spawn_instance)")
    parser.add_argument("--ssh-key", help="Vast: SSH public key to register/attach and verify with "
                                          "(default: ~/.ssh/id_ed25519.pub, then id_rsa.pub)")
    parser.add_argument("--ssh-wait-timeout", type=int, default=720, metavar="SEC",
                        help="Vast: max seconds to wait for SSH login on the clone before reporting "
                             "(default 720)")
    parser.add_argument("--no-ssh-wait", action="store_true",
                        help="skip the post-spawn SSH login check on the clone")
    parser.add_argument("--max-hourly", type=float, help="hard guard: abort if the quote exceeds this USD/hour")
    parser.add_argument("--quote", action="store_true", help="print the cost quote and exit — creates nothing")
    parser.add_argument("--yes", action="store_true",
                        help="skip the approval prompt (only after the user approved the quoted cost)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    try:
        provider = get_provider(args.provider, region=args.region)
        spec = provider.clone_spec(args.id)
    except CloudOpsError as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}))
        else:
            render.warn(str(exc))
        return 1

    if args.offer_id:
        spec["offer_id"] = args.offer_id
    if args.cuda is not None:
        spec["cuda"] = args.cuda
    if args.name:
        spec["name"] = args.name
    if args.disk_gb:
        spec["disk_gb"] = args.disk_gb
    if args.open_ports:
        spec["open_ports"] = args.open_ports
    spec["ssh_pubkey_path"] = args.ssh_key

    # data_copy(result) -> note: the --with-data replication step (Vast side),
    # chained to run once the clone is up.
    pre_spawn = data_copy = None
    if args.with_data:
        if args.provider == "aws":
            def pre_spawn(spec_):
                if not args.json:
                    render.console.print(
                        "Snapshotting the source into an AMI (can take several minutes; "
                        "the AMI + snapshots bill storage until deregistered)..."
                    )
                spec_["ami"] = provider.snapshot_image(args.id, reboot=args.reboot_source)
        else:
            def data_copy(result):
                if not args.json:
                    render.console.print("Ensuring the clone is up before copying data...")
                provider.wait_for_status(result.instance_id, "running", timeout_seconds=600)
                msg = provider.copy_data(args.id, result.instance_id, args.data_path, args.data_path)
                return (f"Data copy {args.id}:{args.data_path} → {result.instance_id}:"
                        f"{args.data_path} started; it runs on Vast's side and large "
                        f"directories take a while ({msg}).")
    elif not args.json:
        render.console.print(
            f"Cloning [bold]{args.id}[/bold] — configuration only; "
            "[yellow]disk contents are NOT copied[/yellow] (use --with-data for that)."
        )

    # Verify SSH on the clone before reporting success (Vast; AWS no-ops), then
    # run the data copy. --no-ssh-wait skips the check but still copies data.
    if not args.no_ssh_wait:
        post_spawn = make_ssh_verify_post_spawn(
            provider,
            timeout_seconds=args.ssh_wait_timeout,
            pubkey_path=args.ssh_key,
            as_json=args.json,
            then=data_copy,
        )
    else:
        post_spawn = data_copy

    return run_spawn_flow(
        provider,
        args.provider,
        spec,
        quote_only=args.quote,
        yes=args.yes,
        max_hourly=args.max_hourly,
        as_json=args.json,
        audit_fields={"cloned_from": args.id, "with_data": bool(args.with_data)},
        pre_spawn=pre_spawn,
        post_spawn=post_spawn,
    )


if __name__ == "__main__":
    sys.exit(main())
