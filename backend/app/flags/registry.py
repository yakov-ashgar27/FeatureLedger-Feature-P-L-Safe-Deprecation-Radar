"""Pick the flag provider: FLAG_PROVIDER, else LaunchDarkly/Statsig when their keys are set, else sandbox."""
from __future__ import annotations

import os

from ..config import config
from .base import FlagProvider, FlagProviderError

_override: FlagProvider | None = None


def set_flag_provider(p: FlagProvider | None) -> None:
    """Tests inject a provider here."""
    global _override
    _override = p


def provider_name() -> str:
    if _override is not None:
        return _override.name
    if config.flag_provider:
        return config.flag_provider
    if os.environ.get("LAUNCHDARKLY_API_TOKEN"):
        return "launchdarkly"
    if os.environ.get("STATSIG_CONSOLE_API_KEY"):
        return "statsig"
    return "sandbox"


def get_flag_provider() -> FlagProvider:
    if _override is not None:
        return _override
    name = provider_name()
    if name == "sandbox":
        from .sandbox import SandboxFlagProvider
        return SandboxFlagProvider()
    from ..connectors.http import HttpxTransport
    if name == "launchdarkly":
        token = os.environ.get("LAUNCHDARKLY_API_TOKEN")
        if not token:
            raise FlagProviderError("FLAG_PROVIDER=launchdarkly needs LAUNCHDARKLY_API_TOKEN")
        from .launchdarkly import LaunchDarklyProvider
        t = HttpxTransport(os.environ.get("LAUNCHDARKLY_BASE_URL", "https://app.launchdarkly.com"),
                           headers={"Authorization": token, "LD-API-Version": "20240415"})
        return LaunchDarklyProvider(t, os.environ.get("LAUNCHDARKLY_PROJECT_KEY", "default"),
                                    os.environ.get("LAUNCHDARKLY_ENVIRONMENT_KEY", "production"),
                                    os.environ.get("LAUNCHDARKLY_CONTEXT_KIND", "account"))
    if name == "statsig":
        key = os.environ.get("STATSIG_CONSOLE_API_KEY")
        if not key:
            raise FlagProviderError("FLAG_PROVIDER=statsig needs STATSIG_CONSOLE_API_KEY")
        from .statsig import StatsigProvider
        t = HttpxTransport(os.environ.get("STATSIG_BASE_URL", "https://statsigapi.net/console/v1"),
                           headers={"STATSIG-API-KEY": key, "STATSIG-API-VERSION": "20240601"})
        return StatsigProvider(t, os.environ.get("STATSIG_ID_TYPE", "accountID"))
    raise FlagProviderError(f"unknown FLAG_PROVIDER {name!r}")
