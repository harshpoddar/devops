"""AWS EC2 backend (boto3). Uses the standard AWS credential chain — whatever
`aws configure` / `aws sso login` / env vars set up is what this uses."""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import date, timedelta
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

PRICING_API_REGION = "us-east-1"  # the Pricing API only lives in a few regions
GP3_USD_PER_GB_MONTH = 0.08  # ballpark for the storage line of a quote
AL2023_SSM = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-{arch}"
DLAMI_SSM_X86 = (
    "/aws/service/deeplearning/ami/x86_64/"
    "base-oss-nvidia-driver-gpu-amazon-linux-2023/latest/ami-id"
)


@contextmanager
def _aws_errors():
    import botocore.exceptions as bex

    try:
        yield
    except (bex.NoCredentialsError, bex.CredentialRetrievalError) as exc:
        raise MissingCredentials(
            "No AWS credentials found. Run `aws configure` (access keys) or "
            "`aws configure sso` + `aws sso login` (recommended). If the aws "
            "command itself is missing, run ./install.sh first."
        ) from exc
    except (bex.UnauthorizedSSOTokenError, bex.SSOTokenLoadError, bex.TokenRetrievalError) as exc:
        raise MissingCredentials(
            "Your AWS SSO session has expired — run `aws sso login` and retry."
        ) from exc
    except bex.ProfileNotFound as exc:
        raise MissingCredentials(
            f"{exc} Check AWS_PROFILE or run `aws configure --profile <name>`."
        ) from exc
    except bex.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("ExpiredToken", "ExpiredTokenException", "RequestExpired"):
            raise MissingCredentials(
                f"AWS credentials expired ({code}) — run `aws sso login`, "
                "or refresh your access keys with `aws configure`."
            ) from exc
        if code in ("UnauthorizedOperation", "AccessDenied", "AccessDeniedException"):
            raise CloudOpsError(
                f"AWS denied this call ({code}): your IAM identity lacks permission for it — "
                f"an AWS admin needs to grant it. Detail: {exc}"
            ) from exc
        raise CloudOpsError(str(exc)) from exc
    except bex.BotoCoreError as exc:
        raise CloudOpsError(f"AWS SDK error: {exc}") from exc


class AWSProvider(Provider):
    name = "aws"

    def __init__(self, region: Optional[str] = None):
        import boto3

        with _aws_errors():
            self.session = boto3.session.Session(region_name=region)
            self.region = self.session.region_name or "us-east-1"
        self._clients: dict = {}

    def _client(self, service: str, region: Optional[str] = None):
        key = (service, region or self.region)
        if key not in self._clients:
            self._clients[key] = self.session.client(service, region_name=key[1])
        return self._clients[key]

    # ------------------------------------------------------------------ instances

    def list_instances(self) -> "list[Instance]":
        out = []
        with _aws_errors():
            paginator = self._client("ec2").get_paginator("describe_instances")
            for page in paginator.paginate():
                for reservation in page["Reservations"]:
                    for inst in reservation["Instances"]:
                        state = inst["State"]["Name"]
                        if state in ("terminated", "shutting-down"):
                            continue
                        tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                        itype = inst["InstanceType"]
                        launched = inst.get("LaunchTime")
                        out.append(
                            Instance(
                                provider="aws",
                                id=inst["InstanceId"],
                                name=tags.get("Name", ""),
                                status=state,
                                instance_type=itype,
                                region=self.region,
                                ip=inst.get("PublicIpAddress") or inst.get("PrivateIpAddress"),
                                hourly_usd=self._hourly_price(itype) if state == "running" else None,
                                launched_at=launched.isoformat() if launched else None,
                                managed=tags.get(config.MANAGED_TAG_KEY) == config.MANAGED_TAG_VALUE,
                            )
                        )
        return out

    # ------------------------------------------------------------------ pricing

    def _hourly_price(self, instance_type: str) -> Optional[float]:
        """On-demand Linux price via the Pricing API, cached for a week."""
        cache_name = f"aws-pricing-{self.region}"
        cached = config.cache_get(cache_name, 7 * 24 * 3600) or {}
        if instance_type in cached:
            return cached[instance_type]
        price = None
        try:
            resp = self._client("pricing", region=PRICING_API_REGION).get_products(
                ServiceCode="AmazonEC2",
                Filters=[
                    {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
                    {"Type": "TERM_MATCH", "Field": "regionCode", "Value": self.region},
                    {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Linux"},
                    {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
                    {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
                    {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
                ],
                MaxResults=20,
            )
            for raw in resp.get("PriceList", []):
                product = json.loads(raw)
                for term in product.get("terms", {}).get("OnDemand", {}).values():
                    for dim in term.get("priceDimensions", {}).values():
                        usd = float(dim.get("pricePerUnit", {}).get("USD") or 0)
                        if usd > 0:
                            price = usd
                            break
                    if price:
                        break
                if price:
                    break
        except Exception:
            return None
        if price is not None:
            cached[instance_type] = price
            config.cache_put(cache_name, cached)
        return price

    # ------------------------------------------------------------------ offers

    def list_offers(self, filters: OfferFilter) -> "list[Offer]":
        with _aws_errors():
            types = []
            paginator = self._client("ec2").get_paginator("describe_instance_types")
            for page in paginator.paginate():
                types.extend(page["InstanceTypes"])

        wanted_gpu = (filters.gpu_type or "").lower().replace("_", " ")
        candidates = []
        for t in types:
            vcpus = t.get("VCpuInfo", {}).get("DefaultVCpus") or 0
            mem_gb = (t.get("MemoryInfo", {}).get("SizeInMiB") or 0) / 1024
            gpu_info = t.get("GpuInfo")
            gpus, gpu_name, gpu_mem = 0, None, None
            if gpu_info and gpu_info.get("Gpus"):
                gpus = sum(g.get("Count", 0) for g in gpu_info["Gpus"])
                g0 = gpu_info["Gpus"][0]
                gpu_name = f"{g0.get('Manufacturer', '')} {g0.get('Name', '')}".strip()
                gpu_mem = (g0.get("MemoryInfo", {}).get("SizeInMiB") or 0) / 1024
            if filters.min_vcpus and vcpus < filters.min_vcpus:
                continue
            if filters.min_memory_gb and mem_gb < filters.min_memory_gb:
                continue
            if filters.min_gpus and gpus < filters.min_gpus:
                continue
            if wanted_gpu and (not gpu_name or wanted_gpu not in gpu_name.lower()):
                continue
            candidates.append((t["InstanceType"], vcpus, mem_gb, gpus, gpu_name, gpu_mem))

        # Smallest hardware first, so the pricing lookups (one API call per type,
        # cached afterwards) stay bounded and land on the cheapest matches.
        candidates.sort(key=lambda c: (c[3], c[1], c[2]))
        offers = []
        for itype, vcpus, mem_gb, gpus, gpu_name, gpu_mem in candidates[: max(filters.limit * 3, 30)]:
            price = self._hourly_price(itype)
            if filters.max_hourly_usd is not None and (price is None or price > filters.max_hourly_usd):
                continue
            desc = f"{vcpus} vCPU, {mem_gb:.0f} GB RAM"
            if gpus:
                desc += f", {gpus}x {gpu_name} ({gpu_mem:.0f} GB)"
            offers.append(
                Offer(
                    provider="aws",
                    id=itype,
                    description=desc,
                    vcpus=vcpus,
                    memory_gb=round(mem_gb, 1),
                    gpus=gpus,
                    gpu_type=gpu_name,
                    gpu_memory_gb=round(gpu_mem, 1) if gpu_mem else None,
                    hourly_usd=price,
                    region=self.region,
                )
            )
            if len(offers) >= filters.limit:
                break
        offers.sort(key=lambda o: (o.hourly_usd is None, o.hourly_usd or 0))
        return offers

    # ------------------------------------------------------------------ spawn

    def _instance_type_info(self, instance_type: str) -> dict:
        with _aws_errors():
            resp = self._client("ec2").describe_instance_types(InstanceTypes=[instance_type])
        return resp["InstanceTypes"][0]

    def _resolve_ami(self, spec: dict, info: dict) -> "tuple[str, str]":
        if spec.get("ami"):
            return spec["ami"], "user-supplied"
        archs = info.get("ProcessorInfo", {}).get("SupportedArchitectures", ["x86_64"])
        arch = "arm64" if "x86_64" not in archs else "x86_64"
        ssm = self._client("ssm")
        has_gpu = bool(info.get("GpuInfo"))
        if has_gpu and arch == "x86_64":
            try:
                ami = ssm.get_parameter(Name=DLAMI_SSM_X86)["Parameter"]["Value"]
                return ami, "Deep Learning Base GPU AMI (AL2023, NVIDIA driver preinstalled)"
            except Exception:
                pass
        with _aws_errors():
            ami = ssm.get_parameter(Name=AL2023_SSM.format(arch=arch))["Parameter"]["Value"]
        note = "Amazon Linux 2023"
        if has_gpu:
            note += " — WARNING: no NVIDIA driver preinstalled; pass --ami for a GPU image"
        return ami, note

    def quote(self, spec: dict) -> Quote:
        itype = spec.get("instance_type")
        if not itype:
            raise CloudOpsError("AWS spawn needs an instance type (--type), e.g. t3.medium or g5.xlarge")
        info = self._instance_type_info(itype)
        vcpus = info.get("VCpuInfo", {}).get("DefaultVCpus")
        mem_gb = (info.get("MemoryInfo", {}).get("SizeInMiB") or 0) / 1024
        gpu_info = info.get("GpuInfo")
        gpu_part = ""
        if gpu_info and gpu_info.get("Gpus"):
            g0 = gpu_info["Gpus"][0]
            n = sum(g.get("Count", 0) for g in gpu_info["Gpus"])
            gpu_part = f", {n}x {g0.get('Manufacturer', '')} {g0.get('Name', '')}"
        price = self._hourly_price(itype)
        disk_gb = int(spec.get("disk_gb") or 30)
        storage_monthly = round(disk_gb * GP3_USD_PER_GB_MONTH, 2)
        ami, ami_note = self._resolve_ami(spec, info)
        monthly = round(price * HOURS_PER_MONTH + storage_monthly, 2) if price is not None else None
        return Quote(
            provider="aws",
            description=f"AWS {itype} in {self.region} ({vcpus} vCPU, {mem_gb:.0f} GB RAM{gpu_part})",
            hourly_usd=price,
            monthly_usd=monthly,
            details={
                "instance_type": itype,
                "region": self.region,
                "ami": ami,
                "ami_note": ami_note,
                "disk_gb": disk_gb,
                "storage_monthly_usd_est": storage_monthly,
                "billing_note": "Compute bills while running; EBS storage bills until the volume is deleted.",
            },
        )

    def spawn(self, spec: dict) -> SpawnResult:
        q = self.quote(spec)
        ami = q.details["ami"]
        with _aws_errors():
            ec2 = self._client("ec2")
            images = ec2.describe_images(ImageIds=[ami]).get("Images", [])
            root_device = images[0].get("RootDeviceName", "/dev/xvda") if images else "/dev/xvda"
            name = spec.get("name") or f"cloudops-{spec['instance_type']}"
            tags = [
                {"Key": "Name", "Value": name},
                {"Key": config.MANAGED_TAG_KEY, "Value": config.MANAGED_TAG_VALUE},
            ]
            if spec.get("ttl_hours"):
                tags.append({"Key": "cloudops-ttl-hours", "Value": str(spec["ttl_hours"])})
            kwargs = dict(
                ImageId=ami,
                InstanceType=spec["instance_type"],
                MinCount=1,
                MaxCount=1,
                BlockDeviceMappings=[
                    {
                        "DeviceName": root_device,
                        "Ebs": {
                            "VolumeSize": int(spec.get("disk_gb") or 30),
                            "VolumeType": "gp3",
                            "DeleteOnTermination": True,
                        },
                    }
                ],
                TagSpecifications=[
                    {"ResourceType": "instance", "Tags": tags},
                    {"ResourceType": "volume", "Tags": tags},
                ],
            )
            if spec.get("key_name"):
                kwargs["KeyName"] = spec["key_name"]
            if spec.get("security_group_ids"):
                kwargs["SecurityGroupIds"] = spec["security_group_ids"]
            if spec.get("subnet_id"):
                kwargs["SubnetId"] = spec["subnet_id"]
            inst = ec2.run_instances(**kwargs)["Instances"][0]
        hint = "Run list_instances to see its state and IP once running."
        if not spec.get("key_name"):
            hint += " No --key-name was given, so SSH needs another path (e.g. SSM Session Manager)."
        return SpawnResult(
            provider="aws",
            instance_id=inst["InstanceId"],
            status=inst["State"]["Name"],
            connect_hint=hint,
            details={"region": self.region, "ami": ami, "name": name},
        )

    def terminate(self, instance_id: str) -> None:
        with _aws_errors():
            self._client("ec2").terminate_instances(InstanceIds=[instance_id])

    # ------------------------------------------------------------------ usage

    def usage(self) -> dict:
        result: dict = {"provider": "aws", "region": self.region}
        instances = self.list_instances()
        running = [i for i in instances if i.status == "running"]
        result["running_instances"] = len(running)
        result["total_instances"] = len(instances)
        result["burn_usd_per_hour"] = round(sum(i.hourly_usd or 0 for i in running), 4)
        try:
            today = date.today()
            resp = self._client("ce", region="us-east-1").get_cost_and_usage(
                TimePeriod={
                    "Start": today.replace(day=1).isoformat(),
                    "End": (today + timedelta(days=1)).isoformat(),
                },
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
                GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
            )
            groups = resp["ResultsByTime"][0].get("Groups", [])
            by_service = sorted(
                ((g["Keys"][0], float(g["Metrics"]["UnblendedCost"]["Amount"])) for g in groups),
                key=lambda kv: -kv[1],
            )
            result["month_to_date_usd"] = round(sum(v for _, v in by_service), 2)
            result["by_service"] = [
                {"service": k, "usd": round(v, 2)} for k, v in by_service[:10] if v >= 0.01
            ]
        except Exception as exc:  # Cost Explorer may be disabled or denied
            result["cost_explorer_error"] = str(exc)
        return result
