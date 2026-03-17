"""Tests for gateway.utils modules."""

from __future__ import annotations

import os

import pytest
from mcp.types import TextContent

from gateway.utils.env import interpolate_env, interpolate_env_dict
from gateway.utils.errors import (
    GatewayError,
    ToolCallError,
    format_error_result,
)


# ── env interpolation ────────────────────────────────────────


def test_interpolate_env_basic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_TOKEN", "abc123")
    assert interpolate_env("Bearer ${MY_TOKEN}") == "Bearer abc123"


def test_interpolate_env_default() -> None:
    # Use a var that is definitely not set
    assert interpolate_env("${__UNSET_VAR_XYZ:-fallback}") == "fallback"


def test_interpolate_env_missing_no_default() -> None:
    with pytest.raises(KeyError, match="__UNSET_VAR_XYZ"):
        interpolate_env("${__UNSET_VAR_XYZ}")


def test_interpolate_env_multiple(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOST", "localhost")
    monkeypatch.setenv("PORT", "5432")
    result = interpolate_env("postgresql://${HOST}:${PORT}/db")
    assert result == "postgresql://localhost:5432/db"


def test_interpolate_env_no_placeholders() -> None:
    assert interpolate_env("plain string") == "plain string"


def test_interpolate_env_dict_all_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET", "s3cret")
    result = interpolate_env_dict({"key": "${SECRET}", "plain": "hello"})
    assert result == {"key": "s3cret", "plain": "hello"}


# ── error types ───────────────────────────────────────────────


def test_gateway_error_default_status() -> None:
    err = GatewayError("something broke")
    assert err.status_code == 500
    assert str(err) == "something broke"


def test_gateway_error_custom_status() -> None:
    err = GatewayError("not found", status_code=404)
    assert err.status_code == 404


def test_tool_call_error() -> None:
    err = ToolCallError(
        "timeout", server_name="github", tool_name="create_issue"
    )
    assert err.status_code == 502
    assert err.server_name == "github"
    assert err.tool_name == "create_issue"


def test_tool_call_error_is_gateway_error() -> None:
    err = ToolCallError("fail")
    assert isinstance(err, GatewayError)


def test_format_error_result() -> None:
    err = ValueError("bad input")
    result = format_error_result(err)
    assert result.isError is True
    assert len(result.content) == 1
    assert result.content[0].text == "bad input"  # type: ignore[union-attr]
