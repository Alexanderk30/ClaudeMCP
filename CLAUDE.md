# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install in dev mode (editable + dev extras: pytest, ruff, mypy)
pip install -e ".[dev]"

# Run the gateway against a config file
mcp-gateway --config tenants.yaml --port 8000
# or: python -m gateway.cli --config tenants.yaml --port 8000

# Full test suite (pytest configured with asyncio_mode = "auto")
pytest

# Single test file / single test
pytest tests/test_pipeline.py
pytest tests/test_pipeline.py::test_handle_call_tool_full_chain

# Lint and type check
ruff check src tests
mypy src
```

There is no `npm run build` / `npm test` here — this is a Python project (hatchling build backend, `requires-python = ">=3.10"`, but ruff/mypy target `py311`). Ignore the generic Node commands in any inherited global `CLAUDE.md`.

## Architecture

The gateway is a **single async process** that proxies MCP traffic from multiple authenticated tenants to multiple downstream MCP servers. The whole thing composes in `gateway.routing.pipeline.RequestPipeline` — that class is the single entry point for understanding how a request flows, and is the right starting point when changing behavior.

### Request flow (one tool call)

`server.py` (SSE handler) → `RequestPipeline.handle_call_tool` → `AuthMiddleware.authenticate` → `AuthMiddleware.authorize` → `RateLimiter.check` → `UsageLogger.track` (async context manager wraps the next step) → `ToolRouter.call_tool` → `ToolAggregator.call_downstream` → real `mcp.ClientSession` to a downstream server.

`handle_list_tools` skips the rate limiter and usage logger but goes through the same auth + scoping logic.

### Key cross-cutting details

- **Tool namespacing**: every downstream tool is stored under a qualified name `server:tool` (e.g. `github:create_issue`). The aggregator merges all downstream manifests into one flat dict; the router resolves qualified names back to the originating server. Don't introduce non-qualified tool names anywhere outside `ToolEntry.original_name`.
- **API key plumbing via contextvars**: `server.py` defines `_current_api_key: ContextVar` and sets it in the SSE connect handler before running `mcp.run(...)`. The `@mcp.list_tools()` / `@mcp.call_tool()` decorators read it back to identify the tenant. This is the SDK-friendly substitute for per-session state — preserve this pattern; do not patch the MCP SDK.
- **Wildcard scoping** (`pipeline._effective_patterns`): a tenant with `allowed_tools: ["*"]` is automatically restricted to `{ds}:*` for each downstream in their `downstream` list. Authorization happens twice: tool-glob match + downstream-prefix match. Both must pass.
- **Auth uses `hmac.compare_digest`** for timing-safe comparison. Keep it that way for any future credential check.
- **Hot reload**: `pipeline.reload_config(new_config)` updates auth + rate limits in-place; `pipeline.refresh_tools()` re-fetches downstream manifests and rebuilds the router. `RateLimiter.reload` preserves bucket history when limits are unchanged. Don't replace the pipeline instance — mutate it.
- **Aggregator startup is sequential**, not parallel: anyio's task-group scoping requires it. Failed downstream connections are logged and skipped; the gateway keeps serving the rest. See `ToolAggregator.__aenter__`.
- **Env interpolation** (`gateway.utils.env`) handles `${VAR}` and `${VAR:-default}` syntax. It runs during config loading on: tenant `api_key`, SSE `url`, and stdio `env` values. If you add a new config field that should accept secrets, wire it through `interpolate_env` in `config/loader.py`.

### Layer boundaries

Each middleware is independently constructable and independently tested — keep it that way. New cross-cutting concerns should be added as a new middleware composed in `RequestPipeline.__init__`, not bolted onto an existing one.

- `middleware/auth.py` — authenticate + authorize (raises `AuthError`)
- `middleware/rate_limiter.py` — sliding-window per tenant; returns `RateStatus` so the HTTP layer can surface headers; raises `RateLimitExceeded` when over
- `middleware/usage_logger.py` — bounded `deque` (50k default); `track()` is the async context manager used by the pipeline; `query()` / `stats_for()` power `/stats`
- `routing/router.py` — qualified-name resolution + glob filtering for `list_tools`
- `routing/pipeline.py` — composition (see above)
- `proxy/aggregator.py` — `ClientSession` management, manifest merge, `refresh_manifest`, `call_downstream` with per-call timeout

### Tests

`tests/mock_downstream.py` is a minimal MCP server (echo + add) used by `test_boot_smoke.py` for end-to-end coverage (boot → SSE connect → proxy through to the mock → HTTP endpoints). Use it (or extend it) when you need integration-level behavior; for pure middleware logic, the layer-specific test files (`test_auth.py`, `test_rate_limiter.py`, etc.) construct each component directly with fake config.

## Project conventions

- Files live under `src/gateway/` (hatch packages from there). Tests under `tests/`. Do not add code to the repo root.
- Ruff lint set: `E, F, I, N, W, UP, B, SIM`; mypy is `strict = true`. New code should pass both without ignores.
- `structlog` is the logger — bind context (`logger.bind(server=...)`) rather than string-formatting; events are keyword-arg style (`logger.info("event.name", key=value)`).
- Custom errors derive from `gateway.utils.errors.GatewayError`; auth/rate-limit have their own subclasses surfaced through the MCP response in `server.py`.
