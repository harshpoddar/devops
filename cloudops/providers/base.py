"""Shared datatypes and the provider interface every backend implements."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Optional

HOURS_PER_MONTH = 730


class CloudOpsError(Exception):
    """Base error for anything cloudops-specific; scripts print these without a traceback."""


class MissingCredentials(CloudOpsError):
    """Raised when a provider has no usable credentials."""


@dataclass
class Instance:
    provider: str
    id: str
    name: str
    status: str
    instance_type: str
    region: str
    ip: Optional[str] = None
    hourly_usd: Optional[float] = None
    launched_at: Optional[str] = None
    gpu: Optional[str] = None
    managed: bool = False  # created by this skill (tag / ownership)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Offer:
    """A purchasable machine shape: an EC2 instance type or a Vast.ai ask."""

    provider: str
    id: str
    description: str
    vcpus: Optional[float] = None
    memory_gb: Optional[float] = None
    gpus: int = 0
    gpu_type: Optional[str] = None
    gpu_memory_gb: Optional[float] = None
    hourly_usd: Optional[float] = None
    region: Optional[str] = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OfferFilter:
    min_vcpus: Optional[int] = None
    min_memory_gb: Optional[float] = None
    min_gpus: Optional[int] = None
    gpu_type: Optional[str] = None  # substring match, e.g. "A100", "RTX 4090"
    max_hourly_usd: Optional[float] = None
    limit: int = 15


@dataclass
class Quote:
    provider: str
    description: str
    hourly_usd: Optional[float]
    monthly_usd: Optional[float]  # hourly * 730 + storage estimate
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SpawnResult:
    provider: str
    instance_id: str
    status: str
    connect_hint: str = ""
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class Provider(ABC):
    name: str = "abstract"

    @abstractmethod
    def list_instances(self) -> "list[Instance]":
        ...

    @abstractmethod
    def list_offers(self, filters: OfferFilter) -> "list[Offer]":
        ...

    @abstractmethod
    def quote(self, spec: dict) -> Quote:
        """Price a spawn spec WITHOUT creating anything."""

    @abstractmethod
    def spawn(self, spec: dict) -> SpawnResult:
        ...

    @abstractmethod
    def terminate(self, instance_id: str) -> None:
        ...

    @abstractmethod
    def usage(self) -> dict:
        """Account-level metrics: spend, balance, burn rate, instance counts."""

    def describe_instance(self, instance_id: str) -> Optional[Instance]:
        for inst in self.list_instances():
            if inst.id == str(instance_id):
                return inst
        return None
