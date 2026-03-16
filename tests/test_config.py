"""Smoke tests for config loading."""

from pathlib import Path

from gateway.config.loader import load_config

EXAMPLE_CONFIG = Path(__file__).resolve().parent.parent / "examples" / "tenants.yaml"


def test_load_example_config() -> None:
    config = load_config(EXAMPLE_CONFIG)
    assert "acme" in config.tenants
    assert config.tenants["acme"].role == "admin"
    assert "filesystem" in config.downstream_servers


def test_tenant_rate_limits() -> None:
    config = load_config(EXAMPLE_CONFIG)
    assert config.tenants["acme"].rate_limit == 120
    assert config.tenants["widgets"].rate_limit == 30
