"""Vast.ai backend — GPU rental marketplace, driven over its REST API.

Auth is a single API key (https://cloud.vast.ai/account/), read from the
VAST_API_KEY env var or ~/.vast_api_key. SSH access to instances uses the SSH
public key registered on that account page.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

import requests

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

# Vast is mid-migration from /api/v0 to /api/v1 (verified 2026-07-16):
# instance LISTING is v1-only (v0 returns 410); search/create/state/delete/
# users/copy still live on v0 only. Paths below carry their version explicitly.
API_ROOT = "https://console.vast.ai/api"
DEFAULT_IMAGE = "pytorch/pytorch:latest"
SEARCH_FETCH_LIMIT = 512  # fetch broad, filter client-side (server-side eq-only matching is too rigid)


class VastProvider(Provider):
    name = "vast"

    def __init__(self):
        self.api_key = config.vast_api_key()
        if not self.api_key:
            raise MissingCredentials(
                "No Vast.ai API key found. Create one at https://cloud.vast.ai/account/ "
                "then `export VAST_API_KEY=<key>` or write it to ~/.vast_api_key"
            )

    def _request(self, method: str, path: str, **kwargs):
        params = kwargs.pop("params", {})
        params.setdefault("api_key", self.api_key)  # older endpoints only accept the query param
        try:
            resp = requests.request(
                method,
                f"{API_ROOT}{path}",
                headers={"Accept": "application/json", "Authorization": f"Bearer {self.api_key}"},
                params=params,
                timeout=60,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise CloudOpsError(f"Vast.ai API unreachable: {exc}") from exc
        if resp.status_code == 401:
            raise MissingCredentials("Vast.ai rejected the API key (401). Check VAST_API_KEY.")
        if resp.status_code >= 400:
            raise CloudOpsError(f"Vast.ai API error {resp.status_code} on {path}: {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise CloudOpsError(f"Vast.ai returned non-JSON for {path}") from exc

    # ------------------------------------------------------------------ instances

    def list_instances(self) -> "list[Instance]":
        data = self._request("GET", "/v1/instances/", params={"owner": "me"})
        out = []
        for row in data.get("instances", []):
            gpu = (row.get("gpu_name") or "").replace("_", " ") or None
            n = row.get("num_gpus") or 1
            launched = None
            if row.get("start_date"):
                launched = datetime.fromtimestamp(row["start_date"], tz=timezone.utc).isoformat()
            ssh = None
            if row.get("ssh_host") and row.get("ssh_port"):
                ssh = f"{row['ssh_host']}:{row['ssh_port']}"
            ports = {}
            if isinstance(row.get("ports"), dict):
                # Docker-style mapping: {"8888/tcp": [{"HostIp":..., "HostPort": "40123"}]}
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

    def _search(self, query: dict) -> "list[dict]":
        data = self._request("GET", "/v0/bundles/", params={"q": json.dumps(query)})
        return data.get("offers", [])

    def list_offers(self, filters: OfferFilter) -> "list[Offer]":
        base_query = {
            "rentable": {"eq": True},
            "verified": {"eq": True},
            "external": {"eq": False},
            "type": "on-demand",
            "order": [["dph_total", "asc"]],
            "limit": SEARCH_FETCH_LIMIT,
        }
        rows = self._search(base_query)
        offers = self._filter_offers(rows, filters)
        # The cheapest-512 window misses expensive GPUs (H100/H200/B200): retry
        # with a server-side gpu_name match, then with a sweep from the pricey end.
        if not offers and filters.gpu_type:
            t = filters.gpu_type.strip()
            variants = sorted({t, t.upper(), t.replace(" ", "_"), t.upper().replace(" ", "_")})
            rows = self._search({**base_query, "gpu_name": {"in": variants}})
            offers = self._filter_offers(rows, filters)
        if not offers and (filters.gpu_type or filters.min_gpus):
            rows = self._search({**base_query, "order": [["dph_total", "desc"]]})
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
        if spec.get("offer_id"):
            wanted = str(spec["offer_id"])
            rows = self._search(
                {"id": {"eq": int(wanted)}, "type": "on-demand", "limit": 5}
            )
            for row in rows:
                if str(row.get("id")) == wanted or str(row.get("ask_contract_id")) == wanted:
                    return self._row_to_offer(row)
            raise CloudOpsError(
                f"Vast offer {wanted} is gone or no longer rentable — offers churn quickly; "
                "re-run list_offers and pick a fresh one."
            )
        matches = self.list_offers(
            OfferFilter(
                min_gpus=spec.get("gpus") or 1,
                gpu_type=spec.get("gpu_type"),
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
        env = {}
        open_ports = [int(p) for p in (spec.get("open_ports") or [])]
        for port in open_ports:
            env[f"-p {port}:{port}"] = "1"  # Docker publish syntax, how Vast exposes ports
        body = {
            "client_id": "me",
            "image": spec.get("image") or DEFAULT_IMAGE,
            "disk": float(spec.get("disk_gb") or 20),
            "runtype": "ssh",
            "onstart": spec.get("onstart") or "",
            "env": env,
        }
        if spec.get("name"):
            body["label"] = spec["name"]
        data = self._request("PUT", f"/v0/asks/{offer.id}/", json=body)
        if not data.get("success"):
            raise CloudOpsError(f"Vast spawn failed: {data}")
        hint = (
            "Run list_instances for the ssh host:port once running. SSH uses the key "
            "registered at https://cloud.vast.ai/account/ — add one there if you haven't."
        )
        if open_ports:
            hint += (f" Exposed ports {open_ports} get random public host ports — "
                     "see `ports` in list_instances --json.")
        return SpawnResult(
            provider="vast",
            instance_id=str(data.get("new_contract")),
            status="loading",
            connect_hint=hint,
            details={"offer_id": offer.id, "image": body["image"],
                     **({"open_ports": open_ports} if open_ports else {})},
        )

    def terminate(self, instance_id: str) -> None:
        self._request("DELETE", f"/v0/instances/{int(instance_id)}/", json={})

    # ------------------------------------------------------------------ start/stop/clone

    def _raw_instance(self, instance_id: str) -> dict:
        data = self._request("GET", "/v1/instances/", params={"owner": "me"})
        for row in data.get("instances", []):
            if str(row.get("id")) == str(instance_id):
                return row
        raise CloudOpsError(f"No Vast instance with id {instance_id} found.")

    def _set_state(self, instance_id: str, state: str) -> None:
        data = self._request("PUT", f"/v0/instances/{int(instance_id)}/", json={"state": state})
        if isinstance(data, dict) and data.get("success") is False:
            raise CloudOpsError(
                f"Vast could not set instance {instance_id} to '{state}': "
                f"{data.get('msg') or data}"
            )

    def start(self, instance_id: str) -> None:
        # May fail if the host has rented these GPUs to someone else meanwhile;
        # in that case clone_instance can recreate the setup on another host.
        self._set_state(instance_id, "running")

    def stop(self, instance_id: str) -> None:
        self._set_state(instance_id, "stopped")

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
            "image": row.get("image_uuid") or row.get("image"),  # v1 uses image_uuid
            "onstart": row.get("onstart") or None,
            "disk_gb": int(row.get("disk_space") or 20),
            "open_ports": open_ports or None,
            "name": f"{row.get('label') or 'vast-' + str(instance_id)}-clone",
        }

    def copy_data(self, src_id: str, dst_id: str, src_path: str, dst_path: str) -> str:
        """Vast-managed rsync between two of your instances (runs on their side)."""
        data = self._request(
            "PUT",
            "/v0/commands/rsync/",
            json={"client_id": "me", "src_id": int(src_id), "dst_id": int(dst_id),
                  "src_path": src_path, "dst_path": dst_path},
        )
        if isinstance(data, dict) and data.get("success") is False:
            raise CloudOpsError(f"Vast data copy failed: {data.get('msg') or data}")
        return (data or {}).get("msg") or "copy request accepted"

    # ------------------------------------------------------------------ usage

    def usage(self) -> dict:
        user = self._request("GET", "/v0/users/current/")
        instances = self.list_instances()
        running = [i for i in instances if i.status == "running"]
        return {
            "provider": "vast",
            "balance_usd": round(float(user.get("credit") or 0), 2),
            "running_instances": len(running),
            "total_instances": len(instances),
            "burn_usd_per_hour": round(sum(i.hourly_usd or 0 for i in running), 4),
        }
