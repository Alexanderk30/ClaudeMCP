"""Smoke test: boot the gateway against a real mock downstream server.

This is the single most important test in the project.  It verifies that
``mcp-gateway --config ...`` actually starts, connects to a downstream
MCP server, aggregates tools, and serves HTTP requests without crashing.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import textwrap
from pathlib import Path

import httpx
import pytest
import yaml

from gateway.config.loader import load_config
from gateway.proxy.aggregator import ToolAggregator
from gateway.routing.pipeline import RequestPipeline
from gateway.server import _build_app, _build_mcp_server


# ── Config pointing at the mock downstream ────────────────────

MOCK_DOWNSTREAM_SCRIPT = str(
    Path(__file__).parent / "mock_downstream.py"
)

_CONFIG = {
    "tenants": {
        "admin": {
            "api_key": "sk-test-admin",
            "role": "admin",
            "allowed_tools": ["*"],
            "rate_limit": 100,
            "downstream": ["mock"],
        },
        "viewer": {
            "api_key": "sk-test-viewer",
            "role": "viewer",
            "allowed_tools": ["mock:echo"],
            "rate_limit": 5,
            "downstream": ["mock"],
        },
    },
    "downstream_servers": {
        "mock": {
            "transport": "stdio",
            "command": sys.executable,
            "args": [MOCK_DOWNSTREAM_SCRIPT],
        },
    },
}


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    """Write the test config to a temp YAML file."""
    p = tmp_path / "test-tenants.yaml"
    p.write_text(yaml.dump(_CONFIG))
    return p


# ── Actual boot tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_gateway_boots_and_aggregates_tools(config_path: Path) -> None:
    """The gateway connects to the mock downstream and discovers its tools."""
    config = load_config(config_path)

    async with ToolAggregator(config.downstream_servers) as aggregator:
        manifest = aggregator.merged_manifest()

        # The mock downstream exposes "echo" and "add"
        assert "mock:echo" in manifest
        assert "mock:add" in manifest
        assert manifest["mock:echo"].description == "Echoes back the input message."
        assert manifest["mock:add"].description == "Adds two numbers."


@pytest.mark.asyncio
async def test_gateway_proxies_tool_call(config_path: Path) -> None:
    """A tool call flows through the full pipeline to the mock downstream."""
    config = load_config(config_path)

    async with ToolAggregator(config.downstream_servers) as aggregator:
        pipeline = RequestPipeline(config, aggregator)

        # Admin calls echo
        result = await pipeline.handle_call_tool(
            api_key="sk-test-admin",
            tool_name="mock:echo",
            arguments={"message": "hello from gateway"},
        )
        assert result.content[0].text == "hello from gateway"  # type: ignore[union-attr]

        # Admin calls add
        result = await pipeline.handle_call_tool(
            api_key="sk-test-admin",
            tool_name="mock:add",
            arguments={"a": 17, "b": 25},
        )
        assert result.content[0].text == "42"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_viewer_cannot_call_add(config_path: Path) -> None:
    """Viewer is only allowed mock:echo — calling mock:add should fail."""
    from gateway.middleware.auth import AuthError

    config = load_config(config_path)

    async with ToolAggregator(config.downstream_servers) as aggregator:
        pipeline = RequestPipeline(config, aggregator)

        # echo is allowed
        result = await pipeline.handle_call_tool(
            api_key="sk-test-viewer",
            tool_name="mock:echo",
            arguments={"message": "ok"},
        )
        assert result.content[0].text == "ok"  # type: ignore[union-attr]

        # add is denied
        with pytest.raises(AuthError):
            await pipeline.handle_call_tool(
                api_key="sk-test-viewer",
                tool_name="mock:add",
                arguments={"a": 1, "b": 2},
            )


@pytest.mark.asyncio
async def test_rate_limit_enforced_e2e(config_path: Path) -> None:
    """Viewer has rate_limit=5; the 6th call should be rejected."""
    from gateway.middleware.rate_limiter import RateLimitExceeded

    config = load_config(config_path)

    async with ToolAggregator(config.downstream_servers) as aggregator:
        pipeline = RequestPipeline(config, aggregator)

        for _ in range(5):
            await pipeline.handle_call_tool(
                api_key="sk-test-viewer",
                tool_name="mock:echo",
                arguments={"message": "ping"},
            )

        with pytest.raises(RateLimitExceeded):
            await pipeline.handle_call_tool(
                api_key="sk-test-viewer",
                tool_name="mock:echo",
                arguments={"message": "one too many"},
            )


@pytest.mark.asyncio
async def test_usage_logged_e2e(config_path: Path) -> None:
    """Every tool call should produce a usage record."""
    config = load_config(config_path)

    async with ToolAggregator(config.downstream_servers) as aggregator:
        pipeline = RequestPipeline(config, aggregator)

        await pipeline.handle_call_tool(
            api_key="sk-test-admin",
            tool_name="mock:echo",
            arguments={"message": "test"},
        )
        await pipeline.handle_call_tool(
            api_key="sk-test-admin",
            tool_name="mock:add",
            arguments={"a": 1, "b": 2},
        )

        records = pipeline.usage_logger.query(tenant_id="admin")
        assert len(records) == 2
        assert all(r.success for r in records)
        assert all(r.latency_ms >= 0 for r in records)

        stats = pipeline.usage_logger.stats_for(tenant_id="admin")
        assert stats.total_calls == 2
        assert stats.successes == 2


@pytest.mark.asyncio
async def test_http_health_and_stats(config_path: Path) -> None:
    """The Starlette HTTP endpoints work against a real downstream."""
    from starlette.testclient import TestClient

    config = load_config(config_path)

    async with ToolAggregator(config.downstream_servers) as aggregator:
        pipeline = RequestPipeline(config, aggregator)
        mcp = _build_mcp_server(pipeline)
        app = _build_app(mcp, pipeline)

        client = TestClient(app)

        # Health
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["tools"] == 2  # echo + add

        # Stats without auth → 403
        resp = client.get("/stats")
        assert resp.status_code == 403

        # Stats with admin key
        resp = client.get("/stats", headers={"x-api-key": "sk-test-admin"})
        assert resp.status_code == 200
        assert resp.json()["total_calls"] == 0  # no calls yet

        # Stats with viewer key → 403 (not admin role)
        resp = client.get("/stats", headers={"x-api-key": "sk-test-viewer"})
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_sse_rejects_bad_auth(config_path: Path) -> None:
    """SSE endpoint rejects missing or bad API keys."""
    from starlette.testclient import TestClient

    config = load_config(config_path)

    async with ToolAggregator(config.downstream_servers) as aggregator:
        pipeline = RequestPipeline(config, aggregator)
        mcp = _build_mcp_server(pipeline)
        app = _build_app(mcp, pipeline)

        client = TestClient(app)

        # No key
        resp = client.get("/sse")
        assert resp.status_code == 401

        # Bad key
        resp = client.get("/sse?api_key=invalid")
        assert resp.status_code == 403
