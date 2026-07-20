"""Vast.ai backend — driven through the official ``vastai`` CLI, not raw REST.

Why the CLI: Vast is mid-migration from ``/api/v0`` to ``/api/v1`` and the raw
endpoints return HTTP 410 intermittently as they move (which is exactly what
broke the old ``requests``-based implementation). The ``vastai`` CLI is
maintained by Vast and tracks that migration for us, so we shell out to it and
parse its ``--raw`` JSON output.

Self-contained: we invoke the ``vastai`` that lives next to *this* interpreter
(the skill's own ``.venv/bin/vastai``, installed via ``pip install -e .``),
never whatever might be on the user's PATH.

Auth: a single API key, from ``VAST_API_KEY``, ``~/.vast_api_key`` or
``~/.config/vastai/vast_api_key`` (see ``config.vast_api_key``). We pass it to
every CLI call with ``--api-key`` so behaviour never depends on the CLI's own
stored key.

SSH: Vast injects an account-registered SSH public key into each container at
boot (asynchronously — it can lag minutes). ``spawn`` registers the local public
key on the account (idempotent) and attaches it to the instance, then
``wait_for_ssh`` polls an actual login until it succeeds before we report
success. That directly avoids the "Permission denied (publickey)" race that came
from testing before the key had propagated.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .. import config
from .base import (
    HOURS_PER_MONTH,
    CloudOpsError,
    Instance,
    MissingCredentials,
    Offer,
    OfferFilter,
    Provider,
    Quote,
    SpawnResult,
)

DEFAULT_IMAGE = "pytorch/pytorch:latest"
SEARCH_FETCH_LIMIT = 512  # fetch broad, filter client-side (server-side matching is too rigid)
SSH_USER = "root"  # Vast containers expose SSH as root
# Local public keys we try, in order, when the caller doesn't name one.
DEFAULT_PUBKEY_NAMES = ("id_ed25519.pub", "id_rsa.pub", "id_ecdsa.pub")


def _vastai_bin() -> str:
    """Path to the ``vastai`` CLI inside this skill's own venv.

    Scripts run under ``<skill>/.venv/bin/python``, so ``vastai`` lives in the
    same ``bin`` dir. We locate it via ``sys.prefix`` (the venv root — reliable
    even though ``sys.executable`` is a symlink to the base Python, which is why
    resolving it would point *out* of the venv). Only if it is genuinely absent
    do we fall back to PATH, and failing that we raise an actionable error.
    """
    import shutil

    for cand in (Path(sys.prefix) / "bin" / "vastai", Path(sys.executable).parent / "vastai"):
        if cand.exists():
            return str(cand)
    found = shutil.which("vastai")
    if found:
        return found
    raise CloudOpsError(
        "The `vastai` CLI is not installed in this skill's environment. "
        "Run ./install.sh (it pip-installs vastai into .venv), then retry."
    )


def _default_pubkey_path() -> Optional[Path]:
    ssh_dir = Path.home() / ".ssh"
    for name in DEFAULT_PUBKEY_NAMES:
        candidate = ssh_dir / name
        if candidate.is_file():
            return candidate
    return None


def _read_pubkey(pubkey_path: Optional[str]) -> "tuple[Optional[str], Optional[str]]":
    """Return (public_key_text, private_key_path) for the chosen key, or (None, None).

    ``pubkey_path`` may point at either the .pub file or its private counterpart;
    we normalise to the .pub for the key text and derive the private key beside it.
    """
    if pubkey_path:
        p = Path(pubkey_path).expanduser()
        pub = p if str(p).endswith(".pub") else Path(str(p) + ".pub")
    else:
        pub = _default_pubkey_path()
    if not pub or not pub.is_file():
        return None, None
    text = pub.read_text().strip()
    priv = str(pub)[:-4]  # strip the ".pub"
    priv_path = priv if Path(priv).is_file() else None
    return (text or None), priv_path


def _norm_key(pubkey: str) -> str:
    """Key identity ignoring the trailing comment: '<type> <body>'."""
    parts = pubkey.split()
    return " ".join(parts[:2]) if len(parts) >= 2 else pubkey.strip()


class VastProvider(Provider):
    name = "vast"

    def __init__(self):
        self.api_key = config.vast_api_key()
        if not self.api_key:
            raise MissingCredentials(
                "No Vast.ai API key found. Create one at https://cloud.vast.ai/account/ "
                "then `export VAST_API_KEY=<key>` or write it to ~/.vast_api_key"
            )
        self._bin = _vastai_bin()

    # ------------------------------------------------------------------ CLI plumbing

    def _cli(self, *args, parse: bool = True, timeout: int = 180):
        """Run ``vastai <args> --api-key … [--raw]`` and return parsed JSON (or text).

        stdout carries the machine output; the CLI writes deprecation notices and
        errors to stderr, which we only surface when a call fails.
        """
        cmd = [self._bin, *[str(a) for a in args], "--api-key", self.api_key]
        if parse:
            cmd.append("--raw")
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise CloudOpsError(f"vastai {' '.join(map(str, args[:2]))} timed out after {timeout}s") from exc
        except OSError as exc:
            raise CloudOpsError(f"Could not run the vastai CLI ({self._bin}): {exc}") from exc

        out, err = (proc.stdout or "").strip(), (proc.stderr or "").strip()
        if proc.returncode != 0:
            blob = f"{err}\n{out}".lower()
            if any(t in blob for t in ("401", "unauthor", "invalid api key", "api key")):
                raise MissingCredentials(
                    "Vast.ai rejected the API key. Check VAST_API_KEY or "
                    "~/.config/vastai/vast_api_key."
                )
            raise CloudOpsError(
                f"vastai {' '.join(map(str, args[:2]))} failed (exit {proc.returncode}): "
                f"{err or out or 'no output'}"
            )
        if not parse:
            return out
        return self._parse_json(out, args)

    @staticmethod
    def _parse_json(out: str, args):
        if not out:
            return {}
        try:
            return json.loads(out)
        except ValueError:
            # A few commands print a human confirmation line before the JSON even
            # with --raw; recover by parsing from the first bracket/brace.
            starts = [i for i in (out.find("["), out.find("{")) if i != -1]
            if starts:
                try:
                    return json.loads(out[min(starts):])
                except ValueError:
                    pass
            raise CloudOpsError(
                f"vastai {' '.join(map(str, args[:2]))} returned non-JSON output: {out[:200]}"
            )

    # ------------------------------------------------------------------ instances

    def _instances_raw(self) -> "list[dict]":
        data = self._cli("show", "instances")
        if isinstance(data, list):
            return data
        return data.get("instances", []) if isinstance(data, dict) else []

    @staticmethod
    def _ssh_endpoint(row: dict) -> "tuple[Optional[str], Optional[int]]":
        host, port = row.get("ssh_host"), row.get("ssh_port")
        return (host, int(port)) if host and port else (None, None)

    def list_instances(self) -> "list[Instance]":
        out = []
        for row in self._instances_raw():
            gpu = (row.get("gpu_name") or "").replace("_", " ") or None
            n = row.get("num_gpus") or 1
            launched = None
            if row.get("start_date"):
                launched = datetime.fromtimestamp(row["start_date"], tz=timezone.utc).isoformat()
            host, port = self._ssh_endpoint(row)
            ssh = f"{host}:{port}" if host and port else None
            ports = {}
            if isinstance(row.get("ports"), dict):
                # Docker-style mapping: {"8888/tcp": [{"HostIp":…, "HostPort": "40123"}]}
                for container_port, binds in row["ports"].items():
                    if isinstance(binds, list) and binds:
                        ports[container_port] = binds[0].get("HostPort")
            out.append(
                Instance(
                    provider="vast",
                    id=str(row.get("id")),
                    name=row.get("label") or row.get("template_name")
                    or row.get("image_uuid") or row.get("image") or "",
                    status=row.get("actual_status") or row.get("intended_status") or "unknown",
                    instance_type=f"{n}x {gpu}" if gpu else "unknown",
                    region=row.get("geolocation") or "",
                    ip=ssh or row.get("public_ipaddr"),
                    hourly_usd=round(row["dph_total"], 4) if row.get("dph_total") else None,
                    launched_at=launched,
                    gpu=gpu,
                    managed=True,  # everything under this API key belongs to the user
                    ports=ports or None,
                )
            )
        return out

    def _raw_instance(self, instance_id: str) -> dict:
        for row in self._instances_raw():
            if str(row.get("id")) == str(instance_id):
                return row
        raise CloudOpsError(f"No Vast instance with id {instance_id} found.")

    # ------------------------------------------------------------------ offers

    @staticmethod
    def _row_to_offer(row: dict) -> Offer:
        gpu_name = (row.get("gpu_name") or "").replace("_", " ")
        gpus = row.get("num_gpus") or 0
        gpu_mem_gb = (row.get("gpu_ram") or 0) / 1024
        vcpus = row.get("cpu_cores_effective") or row.get("cpu_cores")
        mem_gb = (row.get("cpu_ram") or 0) / 1024
        hourly = row.get("dph_total")
        reliability = row.get("reliability2") or row.get("reliability")
        return Offer(
            provider="vast",
            id=str(row.get("id")),
            description=f"{gpus}x {gpu_name} ({gpu_mem_gb:.0f} GB VRAM each)",
            vcpus=round(vcpus, 1) if vcpus else None,
            memory_gb=round(mem_gb, 1),
            gpus=gpus,
            gpu_type=gpu_name or None,
            gpu_memory_gb=round(gpu_mem_gb, 1) if gpu_mem_gb else None,
            hourly_usd=round(hourly, 4) if hourly is not None else None,
            region=row.get("geolocation") or "",
            extra={
                "reliability": round(reliability, 4) if reliability else None,
                "download_mbps": round(row.get("inet_down") or 0),
                "cuda": row.get("cuda_max_good"),
                "max_disk_gb": round(row.get("disk_space") or 0),
                "storage_usd_per_gb_month": row.get("storage_cost"),
            },
        )

    def _search(self, terms: "list[str]", order: str = "dph_total",
                limit: int = SEARCH_FETCH_LIMIT) -> "list[dict]":
        """Run `vastai search offers '<query>'`. We pass ``-n`` (no default query)
        and supply every filter term ourselves so results are deterministic."""
        query = " ".join(t for t in terms if t)
        args = ["search", "offers"]
        if query:
            args.append(query)
        args += ["-t", "on-demand", "-n", "-o", order, "--limit", str(limit)]
        data = self._cli(*args)
        if isinstance(data, list):
            return data
        return data.get("offers", []) if isinstance(data, dict) else []

    def list_offers(self, filters: OfferFilter) -> "list[Offer]":
        # Mirror the CLI's own defaults (verified/rentable/not-rented/not-external):
        # without "rented=false" the marketplace lists machines someone else is
        # already on — they look cheap but can't actually be created.
        base = ["rentable=true", "rented=false", "verified=true", "external=false"]
        if filters.min_cuda is not None:
            base.append(f"cuda_max_good>={filters.min_cuda}")
        if filters.min_gpus:
            base.append(f"num_gpus>={filters.min_gpus}")
        rows = self._search(base)
        offers = self._filter_offers(rows, filters)
        # The cheapest-512 window misses pricey GPUs (H100/H200/B200): retry with
        # a server-side gpu_name match, then a sweep from the expensive end.
        if not offers and filters.gpu_type:
            t = filters.gpu_type.strip()
            variants = sorted({t, t.upper(), t.replace(" ", "_"), t.upper().replace(" ", "_")})
            rows = self._search(base + [f"gpu_name in [{', '.join(json.dumps(v) for v in variants)}]"])
            offers = self._filter_offers(rows, filters)
        if not offers and (filters.gpu_type or filters.min_gpus):
            rows = self._search(base, order="dph_total-")  # trailing '-' = descending
            offers = self._filter_offers(rows, filters)
            offers.sort(key=lambda o: o.hourly_usd or 0)
        return offers

    def _filter_offers(self, rows: "list[dict]", filters: OfferFilter) -> "list[Offer]":
        wanted_gpu = (filters.gpu_type or "").lower().replace("_", " ")
        offers = []
        for row in rows:
            offer = self._row_to_offer(row)
            if filters.min_gpus and offer.gpus < filters.min_gpus:
                continue
            if wanted_gpu and wanted_gpu not in (offer.gpu_type or "").lower():
                continue
            if filters.min_cuda is not None and (offer.extra.get("cuda") or 0) < filters.min_cuda:
                continue
            if filters.min_vcpus and (offer.vcpus or 0) < filters.min_vcpus:
                continue
            if filters.min_memory_gb and (offer.memory_gb or 0) < filters.min_memory_gb:
                continue
            if filters.max_hourly_usd is not None and (offer.hourly_usd or 0) > filters.max_hourly_usd:
                continue
            offers.append(offer)
            if len(offers) >= filters.limit:
                break
        return offers

    # ------------------------------------------------------------------ spawn

    def _find_offer(self, spec: dict) -> Offer:
        min_cuda = spec.get("cuda")
        if spec.get("offer_id"):
            wanted = str(spec["offer_id"])
            # Look the offer up by ask_contract_id (the id list_offers returns);
            # don't add rentable/rented filters so we can report *why* it's gone.
            rows = self._search([f"ask_contract_id={int(wanted)}"], limit=5)
            for row in rows:
                if str(row.get("id")) != wanted and str(row.get("ask_contract_id")) != wanted:
                    continue
                if row.get("rented") or row.get("rentable") is False:
                    raise CloudOpsError(
                        f"Vast offer {wanted} exists but is no longer rentable "
                        "(someone else took it) — re-run list_offers and pick a fresh one."
                    )
                offer = self._row_to_offer(row)
                offer_cuda = offer.extra.get("cuda")
                if min_cuda is not None and (offer_cuda or 0) < min_cuda:
                    raise CloudOpsError(
                        f"Vast offer {wanted} only supports CUDA {offer_cuda}, below the "
                        f"required {min_cuda} — pick an offer with a high enough `cuda` "
                        "from list_offers (or drop/lower --cuda)."
                    )
                return offer
            raise CloudOpsError(
                f"Vast offer {wanted} no longer exists — offers churn quickly; "
                "re-run list_offers and pick a fresh one."
            )
        matches = self.list_offers(
            OfferFilter(
                min_gpus=spec.get("gpus") or 1,
                gpu_type=spec.get("gpu_type"),
                min_cuda=min_cuda,
                max_hourly_usd=spec.get("max_hourly"),
                limit=1,
            )
        )
        if not matches:
            raise CloudOpsError("No Vast offer matches those requirements — relax the filters.")
        return matches[0]

    def quote(self, spec: dict) -> Quote:
        offer = self._find_offer(spec)
        disk_gb = int(spec.get("disk_gb") or 20)
        storage_rate = offer.extra.get("storage_usd_per_gb_month")
        storage_monthly = round(disk_gb * storage_rate, 2) if storage_rate else None
        monthly = None
        if offer.hourly_usd is not None:
            monthly = round(offer.hourly_usd * HOURS_PER_MONTH + (storage_monthly or 0), 2)
        return Quote(
            provider="vast",
            description=f"Vast.ai offer {offer.id}: {offer.description} in {offer.region}",
            hourly_usd=offer.hourly_usd,
            monthly_usd=monthly,
            details={
                "offer_id": offer.id,
                "image": spec.get("image") or DEFAULT_IMAGE,
                "disk_gb": disk_gb,
                "storage_monthly_usd_est": storage_monthly,
                "cuda": offer.extra.get("cuda"),  # host's max CUDA — check against the workload BEFORE approving
                **({"cuda_required_min": spec["cuda"]} if spec.get("cuda") is not None else {}),
                "reliability": offer.extra.get("reliability"),
                "download_mbps": offer.extra.get("download_mbps"),
                "billing_note": "GPU time bills while running; disk storage bills while the instance exists (even stopped).",
                **({"open_ports": spec["open_ports"],
                    "port_note": "Vast maps each exposed port to a random public host port — "
                                 "see `ports` in list_instances --json once running."}
                   if spec.get("open_ports") else {}),
            },
        )

    def spawn(self, spec: dict) -> SpawnResult:
        offer = self._find_offer(spec)
        pubkey, _ = _read_pubkey(spec.get("ssh_pubkey_path"))
        key_note = ""
        if pubkey:
            # Register the key account-level (idempotent) so Vast injects it at boot —
            # the robust path that avoids the async per-instance propagation race.
            try:
                if self.ensure_account_ssh_key(pubkey):
                    key_note = "Registered your local SSH public key on the Vast account. "
            except CloudOpsError:
                pass  # non-fatal; attach below and/or the account may already have a key
        else:
            key_note = ("No local SSH public key found (~/.ssh/id_ed25519.pub etc.) — "
                        "SSH won't be verifiable; register one at https://cloud.vast.ai/account/. ")

        args = [
            "create", "instance", offer.id,
            "--image", spec.get("image") or DEFAULT_IMAGE,
            "--disk", str(float(spec.get("disk_gb") or 20)),
            "--ssh",
        ]
        open_ports = [int(p) for p in (spec.get("open_ports") or [])]
        if open_ports:
            args += ["--env", " ".join(f"-p {p}:{p}" for p in open_ports)]  # Docker publish syntax
        if spec.get("onstart"):
            args += ["--onstart-cmd", spec["onstart"]]
        if spec.get("name"):
            args += ["--label", spec["name"]]

        data = self._cli(*args)
        if not (isinstance(data, dict) and data.get("success")):
            raise CloudOpsError(f"Vast spawn failed: {data}")
        new_id = str(data.get("new_contract") or data.get("new_contract_id") or data.get("id") or "")

        if pubkey and new_id:
            try:
                self.attach_ssh_key(new_id, pubkey)  # belt-and-braces alongside the account key
            except CloudOpsError:
                pass

        hint = (
            f"{key_note}The instance is booting. spawn_instance verifies SSH before "
            "reporting success; otherwise run list_instances for the ssh host:port."
        )
        if open_ports:
            hint += (f" Exposed ports {open_ports} get random public host ports — "
                     "see `ports` in list_instances --json.")
        return SpawnResult(
            provider="vast",
            instance_id=new_id,
            status="loading",
            connect_hint=hint,
            details={"offer_id": offer.id, "image": spec.get("image") or DEFAULT_IMAGE,
                     **({"open_ports": open_ports} if open_ports else {})},
        )

    def terminate(self, instance_id: str) -> None:
        self._cli("destroy", "instance", int(instance_id), "-y")

    # ------------------------------------------------------------------ start/stop/clone

    def _set_state(self, instance_id: str, action: str) -> None:
        data = self._cli(action, "instance", int(instance_id))
        if isinstance(data, dict) and data.get("success") is False:
            raise CloudOpsError(
                f"Vast could not {action} instance {instance_id}: {data.get('msg') or data}"
            )

    def start(self, instance_id: str) -> None:
        # May fail if the host rented these GPUs to someone else meanwhile; in that
        # case clone_instance can recreate the setup on another host.
        self._set_state(instance_id, "start")

    def stop(self, instance_id: str) -> None:
        self._set_state(instance_id, "stop")

    def clone_spec(self, instance_id: str) -> dict:
        row = self._raw_instance(instance_id)
        gpu = (row.get("gpu_name") or "").replace("_", " ")
        open_ports = []
        for pair in row.get("extra_env") or []:
            # port mappings appear as ["-p 8080:8080", "1"] entries
            if isinstance(pair, (list, tuple)) and pair and str(pair[0]).startswith("-p "):
                try:
                    open_ports.append(int(str(pair[0]).split()[1].split(":")[0]))
                except (ValueError, IndexError):
                    continue
        return {
            "gpu_type": gpu or None,
            "gpus": row.get("num_gpus") or 1,
            # Floor the clone's host CUDA at the source's — the auto-picked offer
            # must run whatever the source's image/driver stack was built for.
            "cuda": row.get("cuda_max_good"),
            "image": row.get("image_uuid") or row.get("image"),
            "onstart": row.get("onstart") or None,
            "disk_gb": int(row.get("disk_space") or 20),
            "open_ports": open_ports or None,
            "name": f"{row.get('label') or 'vast-' + str(instance_id)}-clone",
        }

    def copy_data(self, src_id: str, dst_id: str, src_path: str, dst_path: str) -> str:
        """Vast-managed copy between two of your instances (runs on their side)."""
        out = self._cli("copy", f"{src_id}:{src_path}", f"{dst_id}:{dst_path}", parse=False)
        return out or "copy request accepted"

    # ------------------------------------------------------------------ SSH keys + verification

    def ensure_account_ssh_key(self, pubkey: str) -> bool:
        """Register ``pubkey`` on the Vast account if it isn't already. Returns
        True when a new key was added, False when it was already present."""
        want = _norm_key(pubkey)
        existing = self._cli("show", "ssh-keys")
        rows = existing if isinstance(existing, list) else (existing.get("ssh_keys", []) if isinstance(existing, dict) else [])
        for k in rows:
            body = k.get("public_key") or k.get("ssh_key") if isinstance(k, dict) else k
            if isinstance(body, str) and _norm_key(body) == want:
                return False
        self._cli("create", "ssh-key", pubkey, "-y")
        return True

    def attach_ssh_key(self, instance_id: str, pubkey: str) -> None:
        self._cli("attach", "ssh", int(instance_id), pubkey)

    def ssh_command(self, instance: Instance) -> Optional[str]:
        if instance.ip and ":" in instance.ip:
            host, _, port = instance.ip.rpartition(":")
            if host and port.isdigit():
                return f"ssh -p {port} {SSH_USER}@{host}"
        return None

    @staticmethod
    def _ssh_cmd(host: str, port: int, identity: Optional[str] = None) -> str:
        ident = f"-i {identity} " if identity else ""
        return f"ssh {ident}-p {port} {SSH_USER}@{host}"

    @staticmethod
    def _try_ssh(host: str, port: int, identity: Optional[str]) -> "tuple[bool, str]":
        cmd = [
            "ssh", "-p", str(port),
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=12",
            "-o", "LogLevel=ERROR",
        ]
        if identity:
            cmd += ["-i", identity, "-o", "IdentitiesOnly=yes"]
        cmd += [f"{SSH_USER}@{host}", "true"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            return False, "connection timed out"
        except OSError as exc:
            return False, f"ssh client error: {exc}"
        if proc.returncode == 0:
            return True, "ok"
        err = (proc.stderr or "").strip()
        return False, (err.splitlines()[-1] if err else f"ssh exit {proc.returncode}")

    def wait_for_ssh(
        self,
        instance_id: str,
        *,
        timeout_seconds: int = 720,
        poll_seconds: int = 15,
        pubkey_path: Optional[str] = None,
    ) -> dict:
        """Poll until an SSH login to the instance actually succeeds.

        Handles Vast's asynchronous key injection: we keep trying (default up to
        12 minutes) rather than giving up after the first denials. On timeout we
        return ok=False *with* the ssh command and an explicit "do not destroy"
        note, because the key often lands shortly after."""
        _, identity = _read_pubkey(pubkey_path)
        start = time.monotonic()
        endpoint: "tuple[Optional[str], Optional[int]]" = (None, None)
        last_detail = "instance had no SSH endpoint yet"
        while time.monotonic() - start < timeout_seconds:
            try:
                row = self._raw_instance(instance_id)
            except CloudOpsError as exc:
                last_detail = str(exc)
                time.sleep(poll_seconds)
                continue
            host, port = self._ssh_endpoint(row)
            if host and port:
                endpoint = (host, port)
                ok, detail = self._try_ssh(host, port, identity)
                if ok:
                    waited = int(time.monotonic() - start)
                    return {
                        "ok": True,
                        "ssh": self._ssh_cmd(host, port, identity),
                        "endpoint": f"{host}:{port}",
                        "detail": f"SSH login verified after {waited}s.",
                    }
                last_detail = detail
            else:
                last_detail = f"instance status '{row.get('actual_status')}' — no SSH endpoint yet"
            time.sleep(poll_seconds)

        host, port = endpoint
        ssh_cmd = self._ssh_cmd(host, port, identity) if host and port else None
        return {
            "ok": False,
            "ssh": ssh_cmd,
            "endpoint": f"{host}:{port}" if host and port else None,
            "detail": (
                f"SSH not confirmed within {timeout_seconds}s (last: {last_detail}). "
                "Vast injects the SSH key asynchronously and it can lag several minutes — "
                "do NOT destroy the instance; wait and retry the ssh command shortly, or "
                "check https://cloud.vast.ai/account/ has your key registered."
            ),
        }

    # ------------------------------------------------------------------ usage

    def usage(self) -> dict:
        user = self._cli("show", "user")
        instances = self.list_instances()
        running = [i for i in instances if i.status == "running"]
        return {
            "provider": "vast",
            "balance_usd": round(float(user.get("credit") or 0), 2),
            "running_instances": len(running),
            "total_instances": len(instances),
            "burn_usd_per_hour": round(sum(i.hourly_usd or 0 for i in running), 4),
        }
