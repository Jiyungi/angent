"""Environment configuration loader for Angent.

Reads all runtime credentials and settings from the process environment
(populated from a local ``.env`` file via ``python-dotenv``) and exposes them
as a single typed :class:`Config` dataclass plus a :func:`load_config` loader.

The variable names here are aligned exactly with the project ``.env`` /
``.env.template`` so the rest of the codebase has a single, typed source of
truth instead of scattered ``os.getenv`` calls. Secrets are never logged.

Grouped sub-configs:
  * :class:`TrueFoundryConfig`  — the OpenAI-compatible LLM gateway.
  * :class:`ClickHouseConfig`   — the analytical blackboard store.
  * :class:`AirbyteConfig`      — GitHub discovery (and Gmail send if unlocked).
  * :class:`GmailConfig`        — the default SMTP send path.
  * :class:`SensoConfig`        — the Publisher (cited.md) integration.
  * :class:`X402Config`         — the Payment Gate (x402 pay-per-fetch).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

try:  # python-dotenv is the preferred loader (matches email_sender.py)
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - fallback when dotenv isn't installed
    load_dotenv = None  # type: ignore[assignment]


# --- Defaults implied by the design ----------------------------------------

DEFAULT_TRUEFOUNDRY_BASE_URL = "https://gateway.truefoundry.ai"
DEFAULT_CLICKHOUSE_PORT = 8443
DEFAULT_CLICKHOUSE_USER = "default"
DEFAULT_CLICKHOUSE_DATABASE = "default"
DEFAULT_SENSO_BASE_URL = "https://apiv2.senso.ai/api/v1"
DEFAULT_X402_FACILITATOR_URL = "https://x402.org/facilitator"
DEFAULT_X402_NETWORK = "eip155:84532"  # Base Sepolia testnet
DEFAULT_X402_PRICE = "$0.001"


def _get(name: str, default: Optional[str] = None) -> Optional[str]:
    """Read an env var, treating empty strings as unset and stripping inline comments.

    ``.env`` files in this project sometimes carry trailing ``# comment`` text on
    unquoted values (e.g. the x402 facilitator URL); python-dotenv usually strips
    these, but we defend against it here so values stay clean regardless of loader.
    """
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    if not value:
        return default
    # Strip a trailing inline comment introduced by " #" on unquoted values.
    if " #" in value:
        value = value.split(" #", 1)[0].strip()
    # Drop surrounding quotes if present.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return value or default


def _get_int(name: str, default: int) -> int:
    raw = _get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class TrueFoundryConfig:
    """TrueFoundry AI Gateway (OpenAI-compatible) — routes all LLM calls."""

    api_key: Optional[str] = None
    base_url: str = DEFAULT_TRUEFOUNDRY_BASE_URL

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True)
class ClickHouseConfig:
    """ClickHouse Cloud HTTPS interface — the shared blackboard + analytics store."""

    host: Optional[str] = None
    port: int = DEFAULT_CLICKHOUSE_PORT
    user: str = DEFAULT_CLICKHOUSE_USER
    password: Optional[str] = None
    database: str = DEFAULT_CLICKHOUSE_DATABASE

    @property
    def is_configured(self) -> bool:
        return bool(self.host)


@dataclass(frozen=True)
class AirbyteConfig:
    """Airbyte Agents API — GitHub discovery (and Gmail send if the tier unlocks)."""

    organization_id: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None

    @property
    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


@dataclass(frozen=True)
class GmailConfig:
    """Gmail SMTP send path (the reliable default sender)."""

    address: Optional[str] = None
    app_password: Optional[str] = None

    @property
    def is_configured(self) -> bool:
        return bool(self.address and self.app_password)


@dataclass(frozen=True)
class PioneerConfig:
    """Pioneer (Fastino) adaptive-inference scorer — optional, pluggable."""

    api_key: Optional[str] = None

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True)
class SensoConfig:
    """Senso / cited.md publishing for the Deal_Memo."""

    api_key: Optional[str] = None
    base_url: str = DEFAULT_SENSO_BASE_URL

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True)
class X402Config:
    """x402 pay-per-fetch Payment Gate configuration (Base Sepolia testnet)."""

    facilitator_url: str = DEFAULT_X402_FACILITATOR_URL
    network: str = DEFAULT_X402_NETWORK
    pay_to_address: Optional[str] = None
    price: str = DEFAULT_X402_PRICE
    evm_private_key: Optional[str] = None

    @property
    def is_seller_configured(self) -> bool:
        return bool(self.pay_to_address)

    @property
    def is_buyer_configured(self) -> bool:
        return bool(self.evm_private_key)


@dataclass(frozen=True)
class Config:
    """Top-level typed configuration object for the whole Angent core."""

    truefoundry: TrueFoundryConfig = field(default_factory=TrueFoundryConfig)
    clickhouse: ClickHouseConfig = field(default_factory=ClickHouseConfig)
    airbyte: AirbyteConfig = field(default_factory=AirbyteConfig)
    gmail: GmailConfig = field(default_factory=GmailConfig)
    pioneer: PioneerConfig = field(default_factory=PioneerConfig)
    senso: SensoConfig = field(default_factory=SensoConfig)
    x402: X402Config = field(default_factory=X402Config)

    # Optional extra signal-source credentials.
    github_token: Optional[str] = None
    huggingface_token: Optional[str] = None

    def summary(self) -> dict[str, bool]:
        """Return a secret-free map of which integrations are configured.

        Safe to log: contains only booleans, never credential values.
        """
        return {
            "truefoundry": self.truefoundry.is_configured,
            "clickhouse": self.clickhouse.is_configured,
            "airbyte": self.airbyte.is_configured,
            "gmail": self.gmail.is_configured,
            "pioneer": self.pioneer.is_configured,
            "senso": self.senso.is_configured,
            "x402_seller": self.x402.is_seller_configured,
            "x402_buyer": self.x402.is_buyer_configured,
            "github_token": bool(self.github_token),
            "huggingface_token": bool(self.huggingface_token),
        }


def load_config(dotenv_path: Optional[str] = None, *, override: bool = False) -> Config:
    """Load configuration from the environment (and a ``.env`` file if available).

    Args:
        dotenv_path: Optional explicit path to a ``.env`` file. When omitted,
            python-dotenv searches upward from the current working directory.
        override: When True, values in the ``.env`` file override existing
            process environment variables.

    Returns:
        A fully populated, typed :class:`Config`.
    """
    if load_dotenv is not None:
        if dotenv_path:
            load_dotenv(dotenv_path=dotenv_path, override=override)
        else:
            load_dotenv(override=override)

    return Config(
        truefoundry=TrueFoundryConfig(
            api_key=_get("TRUEFOUNDRY_API_KEY"),
            base_url=_get("TRUEFOUNDRY_BASE_URL", DEFAULT_TRUEFOUNDRY_BASE_URL) or DEFAULT_TRUEFOUNDRY_BASE_URL,
        ),
        clickhouse=ClickHouseConfig(
            host=_get("CLICKHOUSE_HOST"),
            port=_get_int("CLICKHOUSE_PORT", DEFAULT_CLICKHOUSE_PORT),
            user=_get("CLICKHOUSE_USER", DEFAULT_CLICKHOUSE_USER) or DEFAULT_CLICKHOUSE_USER,
            password=_get("CLICKHOUSE_PASSWORD"),
            database=_get("CLICKHOUSE_DATABASE", DEFAULT_CLICKHOUSE_DATABASE) or DEFAULT_CLICKHOUSE_DATABASE,
        ),
        airbyte=AirbyteConfig(
            organization_id=_get("AIRBYTE_ORGANIZATION_ID"),
            client_id=_get("AIRBYTE_CLIENT_ID"),
            client_secret=_get("AIRBYTE_CLIENT_SECRET"),
        ),
        gmail=GmailConfig(
            address=_get("GMAIL_ADDRESS"),
            app_password=_get("GMAIL_APP_PASSWORD"),
        ),
        pioneer=PioneerConfig(
            api_key=_get("PIONEER_API_KEY"),
        ),
        senso=SensoConfig(
            api_key=_get("SENSO_API_KEY"),
            base_url=_get("SENSO_BASE_URL", DEFAULT_SENSO_BASE_URL) or DEFAULT_SENSO_BASE_URL,
        ),
        x402=X402Config(
            facilitator_url=_get("X402_FACILITATOR_URL", DEFAULT_X402_FACILITATOR_URL) or DEFAULT_X402_FACILITATOR_URL,
            network=_get("X402_NETWORK", DEFAULT_X402_NETWORK) or DEFAULT_X402_NETWORK,
            pay_to_address=_get("X402_PAY_TO_ADDRESS"),
            price=_get("X402_PRICE", DEFAULT_X402_PRICE) or DEFAULT_X402_PRICE,
            evm_private_key=_get("EVM_PRIVATE_KEY"),
        ),
        github_token=_get("GITHUB_TOKEN"),
        huggingface_token=_get("HUGGINGFACE_TOKEN"),
    )


if __name__ == "__main__":  # Quick self-check: prints which integrations are configured.
    cfg = load_config()
    print("Angent configuration loaded. Integration status (secret-free):")
    for name, configured in cfg.summary().items():
        print(f"  {name:16s}: {'configured' if configured else 'not set'}")
