"""Load and validate tenant + downstream server configuration from YAML."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


# ── Tenant schema ────────────────────────────────────────────


class TenantConfig(BaseModel):
    """Configuration for a single tenant."""

    api_key: str
    role: str = Field(pattern=r"^(admin|editor|viewer)$")
    allowed_tools: list[str] = Field(default_factory=lambda: ["*"])
    rate_limit: int = 60  # requests per minute
    downstream: list[str] = Field(default_factory=list)


# ── Downstream server schema ────────────────────────────────


class StdioTransport(BaseModel):
    transport: str = "stdio"
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


class SSETransport(BaseModel):
    transport: str = "sse"
    url: str


DownstreamServerConfig = StdioTransport | SSETransport


# ── Top-level gateway config ────────────────────────────────


class GatewayConfig(BaseModel):
    """Entire gateway configuration file."""

    tenants: dict[str, TenantConfig]
    downstream_servers: dict[str, DownstreamServerConfig]


# ── Loader ───────────────────────────────────────────────────


def _parse_downstream(raw: dict[str, Any]) -> DownstreamServerConfig:
    if raw.get("transport") == "sse":
        return SSETransport(**raw)

    # Interpolate ${VAR} references in the env block
    from gateway.utils.env import interpolate_env_dict

    if "env" in raw:
        raw = {**raw, "env": interpolate_env_dict(raw["env"])}

    return StdioTransport(**raw)


def load_config(path: str | Path) -> GatewayConfig:
    """Read a YAML config file and return a validated GatewayConfig."""
    raw = yaml.safe_load(Path(path).read_text())
    tenants = {name: TenantConfig(**t) for name, t in raw["tenants"].items()}
    downstreams = {name: _parse_downstream(d) for name, d in raw["downstream_servers"].items()}
    return GatewayConfig(tenants=tenants, downstream_servers=downstreams)
