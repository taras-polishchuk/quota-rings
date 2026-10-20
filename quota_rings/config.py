"""Configuration: TOML in, validated account objects out.

The config file is designed to be committable. It never holds a credential, only
a *reference* to one (``env:NAME``, ``file:/path``, ``command:...``) -- see
:mod:`quota_rings.secrets`.

Multiple accounts per provider are the normal case, not a special one: every
provider entry is a list item with its own ``id``, its own label, its own price
and its own credential reference. Two Claude entries with two different tokens
are two different subscriptions.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .model import BILLING_UNKNOWN
from .providers import SUPPORTED_IDS
from .secrets import SecretError, SecretRef

DEFAULT_TTL_SECONDS = 60
DEFAULT_PORT = 8790
DEFAULT_HOST = "127.0.0.1"

VALID_BILLING = {"subscription", "payg", "free", "unknown"}


class ConfigError(RuntimeError):
    """Config is missing, malformed, or names something that does not exist."""


@dataclass
class Account:
    """One configured account. Holds references, never values."""

    id: str
    provider: str
    label: str
    vendor: str
    billing: str = BILLING_UNKNOWN
    plan: str | None = None
    price: float | None = None
    currency: str = "USD"
    price_period: str = "month"
    enabled: bool = True
    token_ref: SecretRef = field(default_factory=lambda: SecretRef("empty"))
    secondary_token_ref: SecretRef = field(default_factory=lambda: SecretRef("empty"))
    options: dict[str, Any] = field(default_factory=dict)
    allow_vendor_reset: bool = False
    _token_cache: str | None = None
    _secondary_cache: str | None = None

    # -- credential access (server side only) -------------------------------

    def resolve_token(self) -> str:
        if self._token_cache is None:
            self._token_cache = self.token_ref.resolve()
        return self._token_cache

    def resolve_secondary_token(self) -> str:
        if self._secondary_cache is None:
            try:
                self._secondary_cache = self.secondary_token_ref.resolve()
            except SecretError:
                self._secondary_cache = ""
        return self._secondary_cache

    def token_source(self) -> str:
        """Value-free description of where the credential comes from."""
        return self.token_ref.describe()


@dataclass
class Settings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    timeout_seconds: float = 20.0
    # Vendor quota resets are off unless the operator turns them on globally AND
    # per account. Two independent switches, because this spends a finite
    # vendor resource.
    enable_vendor_actions: bool = False
    # Bind to a non-loopback address only with an explicit opt-in.
    allow_remote_bind: bool = False
    accounts: list[Account] = field(default_factory=list)
    config_path: str | None = None


def _account_from_table(raw: dict[str, Any], index: int) -> Account:
    provider = str(raw.get("provider", "")).strip().lower()
    if not provider:
        raise ConfigError(f"account #{index + 1}: 'provider' is required")
    if provider not in SUPPORTED_IDS:
        raise ConfigError(
            f"account #{index + 1}: unknown provider {provider!r}; "
            f"known providers: {', '.join(sorted(SUPPORTED_IDS))}"
        )

    account_id = str(raw.get("id") or f"{provider}-{index + 1}").strip()
    label = str(raw.get("label") or account_id)

    billing = str(raw.get("billing", BILLING_UNKNOWN)).strip().lower()
    if billing not in VALID_BILLING:
        raise ConfigError(
            f"account {account_id!r}: billing must be one of {sorted(VALID_BILLING)}, got {billing!r}"
        )

    price = raw.get("price")
    if price is not None:
        try:
            price = float(price)
        except (TypeError, ValueError):
            raise ConfigError(f"account {account_id!r}: price must be a number or absent") from None
        if price < 0:
            raise ConfigError(f"account {account_id!r}: price cannot be negative")

    try:
        token_ref = SecretRef.parse(raw.get("token"))
        secondary_ref = SecretRef.parse(raw.get("management_token"))
    except SecretError as exc:
        raise ConfigError(f"account {account_id!r}: {exc}") from None

    # An Enterprise-only provider with no key is not a config error: the card
    # still renders, carrying a named unsupported state instead of a guess.

    return Account(
        id=account_id,
        provider=provider,
        label=label,
        vendor=str(raw.get("vendor") or provider).strip(),
        billing=billing,
        plan=str(raw["plan"]).strip() if raw.get("plan") else None,
        price=price,
        currency=str(raw.get("currency", "USD")).strip().upper(),
        price_period=str(raw.get("price_period", "month")).strip(),
        enabled=bool(raw.get("enabled", True)),
        token_ref=token_ref,
        secondary_token_ref=secondary_ref,
        options=dict(raw.get("options") or {}),
        allow_vendor_reset=bool(raw.get("allow_vendor_reset", False)),
    )


def load_config(path: str | os.PathLike[str]) -> Settings:
    config_path = Path(os.path.expanduser(str(path))).resolve()
    if not config_path.exists():
        raise ConfigError(f"config file not found: {config_path}")
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{config_path} is not valid TOML: {exc}") from None

    server = raw.get("server") or {}
    settings = Settings(
        host=str(server.get("host", DEFAULT_HOST)),
        port=int(server.get("port", DEFAULT_PORT)),
        ttl_seconds=int(server.get("ttl_seconds", DEFAULT_TTL_SECONDS)),
        timeout_seconds=float(server.get("timeout_seconds", 20.0)),
        enable_vendor_actions=bool(server.get("enable_vendor_actions", False)),
        allow_remote_bind=bool(server.get("allow_remote_bind", False)),
        config_path=str(config_path),
    )
    if settings.ttl_seconds < 5:
        raise ConfigError("server.ttl_seconds must be >= 5; polling vendors harder than that is not kind")
    if not 1 <= settings.port <= 65535:
        raise ConfigError(f"server.port {settings.port} is out of range")

    accounts_raw = raw.get("account") or []
    if not isinstance(accounts_raw, list):
        raise ConfigError("[[account]] must be an array of tables")

    seen: set[str] = set()
    for index, entry in enumerate(accounts_raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"account #{index + 1} is not a table")
        account = _account_from_table(entry, index)
        if account.id in seen:
            raise ConfigError(f"duplicate account id {account.id!r}; ids address the UI and must be unique")
        seen.add(account.id)
        settings.accounts.append(account)

    if not settings.accounts:
        raise ConfigError(f"{config_path} defines no [[account]] entries, so there is nothing to show")

    settings.accounts.sort(key=lambda a: a.id)
    return settings


def enabled_accounts(settings: Settings) -> list[Account]:
    return [a for a in settings.accounts if a.enabled]
