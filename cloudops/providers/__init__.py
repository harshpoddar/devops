"""Provider registry. Add a new backend by dropping in a module and listing it here."""
from __future__ import annotations

from typing import Optional

from .base import (  # noqa: F401  (re-exported for convenience)
    CloudOpsError,
    Instance,
    MissingCredentials,
    Offer,
    OfferFilter,
    Provider,
    Quote,
    SpawnResult,
)

PROVIDER_NAMES = ("aws", "vast")


def get_provider(name: str, region: Optional[str] = None) -> Provider:
    name = name.lower()
    if name == "aws":
        from .aws import AWSProvider

        return AWSProvider(region=region)
    if name == "vast":
        from .vast import VastProvider

        return VastProvider()
    raise CloudOpsError(f"Unknown provider '{name}'. Supported: {', '.join(PROVIDER_NAMES)}")


def resolve_providers(name: str, region: Optional[str] = None):
    """Expand 'aws' | 'vast' | 'all' into [(name, provider_or_None, error_or_None)].

    A provider that fails to initialize (usually missing credentials) is returned
    with its error message instead of aborting the others.
    """
    names = PROVIDER_NAMES if name == "all" else (name.lower(),)
    resolved = []
    for n in names:
        try:
            resolved.append((n, get_provider(n, region=region), None))
        except Exception as exc:
            resolved.append((n, None, str(exc)))
    return resolved
