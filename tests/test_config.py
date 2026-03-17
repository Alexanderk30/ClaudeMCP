"""Tests for config loading and env-var interpolation."""

from pathlib import Path

import pytest
import yaml

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


def test_env_interpolation_in_stdio_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """${VAR} in a stdio transport's env block is resolved at load time."""
    monkeypatch.setenv("MY_SECRET", "s3cret-token")
    cfg = {
        "tenants": {
            "t": {
                "api_key": "sk-test",
                "role": "viewer",
                "rate_limit": 10,
                "downstream": ["srv"],
            }
        },
        "downstream_servers": {
            "srv": {
                "transport": "stdio",
                "command": "echo",
                "env": {"TOKEN": "${MY_SECRET}"},
            }
        },
    }
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.dump(cfg))
    config = load_config(p)
    assert config.downstream_servers["srv"].env["TOKEN"] == "s3cret-token"  # type: ignore[union-attr]


def test_env_interpolation_in_sse_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """${VAR} in an SSE transport's url field is resolved at load time."""
    monkeypatch.setenv("SSE_HOST", "prod.example.com")
    cfg = {
        "tenants": {
            "t": {
                "api_key": "sk-test",
                "role": "viewer",
                "rate_limit": 10,
                "downstream": ["srv"],
            }
        },
        "downstream_servers": {
            "srv": {
                "transport": "sse",
                "url": "https://${SSE_HOST}/sse",
            }
        },
    }
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.dump(cfg))
    config = load_config(p)
    assert config.downstream_servers["srv"].url == "https://prod.example.com/sse"  # type: ignore[union-attr]


def test_env_interpolation_in_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """${VAR} in a tenant's api_key is resolved at load time."""
    monkeypatch.setenv("ACME_KEY", "sk-from-env")
    cfg = {
        "tenants": {
            "acme": {
                "api_key": "${ACME_KEY}",
                "role": "admin",
                "rate_limit": 60,
                "downstream": [],
            }
        },
        "downstream_servers": {},
    }
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.dump(cfg))
    config = load_config(p)
    assert config.tenants["acme"].api_key == "sk-from-env"


def test_env_interpolation_default_value(tmp_path: Path) -> None:
    """${VAR:-default} falls back when the env var is not set."""
    cfg = {
        "tenants": {
            "t": {
                "api_key": "sk-test",
                "role": "viewer",
                "rate_limit": 10,
                "downstream": ["srv"],
            }
        },
        "downstream_servers": {
            "srv": {
                "transport": "sse",
                "url": "${UNSET_VAR:-http://localhost:9999/sse}",
            }
        },
    }
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.dump(cfg))
    config = load_config(p)
    assert config.downstream_servers["srv"].url == "http://localhost:9999/sse"  # type: ignore[union-attr]


def test_env_interpolation_missing_raises(tmp_path: Path) -> None:
    """${VAR} with no default and no env var raises KeyError."""
    cfg = {
        "tenants": {
            "t": {
                "api_key": "sk-test",
                "role": "viewer",
                "rate_limit": 10,
                "downstream": ["srv"],
            }
        },
        "downstream_servers": {
            "srv": {
                "transport": "stdio",
                "command": "echo",
                "env": {"TOKEN": "${DEFINITELY_NOT_SET}"},
            }
        },
    }
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.dump(cfg))
    with pytest.raises(KeyError, match="DEFINITELY_NOT_SET"):
        load_config(p)
