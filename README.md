# MCP Gateway — Multi-Tenant MCP Proxy & Aggregation Server

A gateway that sits between MCP clients (Claude, Cowork, custom agents) and
multiple downstream MCP servers, adding **authentication**, **per-tenant
permissions**, **rate limiting**, **usage logging**, and **tool routing** in a
single process.

```
┌──────────────┐
│  Tenant A    │──┐
│ (Claude)     │  │
└──────────────┘  │    ┌─────────────────────┐    ┌──────────────────┐
                  ├───▶│                     │───▶│ Filesystem MCP   │
┌──────────────┐  │    │    MCP  Gateway     │    └──────────────────┘
│  Tenant B    │──┤    │                     │    ┌──────────────────┐
│ (Cowork)     │  │    │  • Auth / Perms     │───▶│ GitHub MCP       │
└──────────────┘  │    │  • Rate Limiting    │    └──────────────────┘
                  ├───▶│  • Usage Logging    │    ┌──────────────────┐
┌──────────────┐  │    │  • Tool Routing     │───▶│ Google Drive MCP │
│  Tenant C    │──┘    │  • Proxy / Agg      │    └──────────────────┘
│ (Custom)     │       │                     │    ┌──────────────────┐
└──────────────┘       └─────────────────────┘───▶│ Custom MCP       │
                                                  └──────────────────┘
```

## Quick Start

```bash
# Install in dev mode
pip install -e ".[dev]"

# Copy and edit the example config
cp examples/tenants.yaml tenants.yaml
# → set real API keys and downstream server commands/URLs

# Start the gateway
mcp-gateway --config tenants.yaml --port 8000

# Run the tests (80 tests, ~2 seconds)
pytest
```

Once running, tenants connect to the SSE endpoint with their API key:

```
GET http://localhost:8000/sse?api_key=sk-acme-XXXXXXXXXXXXXXXXXXXX
```

The gateway also serves two HTTP endpoints: `GET /health` returns tool count
and uptime info, and `GET /stats` (admin-only, requires `x-api-key` header)
returns per-tenant usage statistics.


## Architecture

Four layers, each independently testable, composed into a single request
pipeline by `gateway.routing.pipeline.RequestPipeline`.

### Layer 1 — Auth & Permissions (`gateway.middleware.auth`)

Every inbound request carries an API key, passed as an `api_key` query
parameter on the SSE endpoint or as an `x-api-key` header. The auth middleware
validates the key using timing-safe `hmac.compare_digest`, resolves the tenant
identity and role, then runs two authorization checks: first, whether the
requested tool matches the tenant's `allowed_tools` glob patterns (e.g.
`filesystem:*`); second, whether the tool's server prefix is in the tenant's
`downstream` list. Both checks must pass.

Supports hot-reload via `reload()` so tenant configs can be updated without
restarting the gateway.

### Layer 2 — Rate Limiting & Usage Logging

**Rate limiter** (`gateway.middleware.rate_limiter`) — A per-tenant
sliding-window counter that caps requests per minute. Returns a `RateStatus`
with `remaining` and `retry_after` fields so the server layer can surface
rate-limit headers. `peek()` checks capacity without consuming a slot.
`reload()` preserves bucket history when rate limits haven't changed, and
resets cleanly when they have.

**Usage logger** (`gateway.middleware.usage_logger`) — An async context manager
that wraps every tool call, recording tenant ID, tool name, latency, and
success/failure into a bounded ring buffer (50k records by default).
`query(tenant_id=..., tool_name=..., since=..., limit=...)` retrieves filtered
records, and `stats_for()` computes aggregates including total calls,
success/failure counts, average latency, and p99 latency.

### Layer 3 — Tool Routing & Request Pipeline

**Router** (`gateway.routing.router`) — Holds the merged tool manifest. Tool
names are qualified as `server:tool` (e.g. `github:create_issue`). The router
resolves qualified names back to the originating downstream server and
dispatches via the aggregator. `list_tools()` accepts glob patterns for
per-tenant filtering.

**Pipeline** (`gateway.routing.pipeline`) — The single entry point that
composes all layers: authenticate → authorize → rate-limit → usage-log →
route → proxy. Wildcard tenants (`allowed_tools: ["*"]`) are automatically
scoped to their allowed downstream servers. Exposes `reload_config()` and
`refresh_tools()` for live reconfiguration.

### Layer 4 — Proxy & Aggregation (`gateway.proxy.aggregator`)

The aggregator manages MCP client connections to every downstream server via
real `mcp.ClientSession` instances over stdio or SSE transports. On startup it
connects to each server sequentially (required by anyio's task-group scoping),
fetches paginated tool manifests, and merges them into a single `server:tool`
namespace with duplicate detection. Failed connections are logged but don't
block the gateway — the remaining servers still serve traffic.

`refresh_manifest()` re-fetches tools from all downstreams for hot-reload.
`call_downstream()` proxies tool calls with configurable per-call timeouts.

### Server Layer (`gateway.server`)

Wires everything into a real MCP server exposed over SSE transport via
Starlette and uvicorn. Tenants authenticate on the `GET /sse` endpoint; a
`contextvars`-based approach threads the API key from the HTTP layer into the
MCP protocol handlers so each SSE session sees only the tools it's authorized
for.


## Project Structure

```
mcp-gateway/
├── pyproject.toml
├── README.md
├── examples/
│   └── tenants.yaml              # Sample config (3 tenants, 4 downstreams)
├── src/gateway/
│   ├── __init__.py
│   ├── cli.py                    # Click CLI: mcp-gateway --config ... --port ...
│   ├── server.py                 # Starlette app, SSE transport, /health, /stats
│   ├── config/
│   │   └── loader.py             # YAML → Pydantic models, ${VAR} interpolation
│   ├── middleware/
│   │   ├── auth.py               # Timing-safe auth, glob ACLs, downstream scoping
│   │   ├── rate_limiter.py       # Sliding-window rate limiter with status/peek
│   │   └── usage_logger.py       # Ring-buffer logger with query/stats
│   ├── routing/
│   │   ├── router.py             # Qualified-name resolution + glob filtering
│   │   └── pipeline.py           # Full middleware chain composition
│   ├── proxy/
│   │   └── aggregator.py         # MCP ClientSession management + manifest merge
│   └── utils/
│       ├── env.py                # ${VAR} and ${VAR:-default} interpolation
│       └── errors.py             # GatewayError hierarchy + MCP error formatting
└── tests/
    ├── mock_downstream.py        # Minimal MCP server (echo + add) for integration tests
    ├── test_boot_smoke.py        # End-to-end: boot → connect → proxy → HTTP
    ├── test_aggregator.py        # Layer 4 unit tests (12 tests)
    ├── test_pipeline.py          # Layer 3 pipeline tests (10 tests)
    ├── test_auth.py              # Layer 1 auth tests (14 tests)
    ├── test_rate_limiter.py      # Layer 2a rate limiter tests (8 tests)
    ├── test_usage_logger.py      # Layer 2b usage logger tests (9 tests)
    ├── test_server.py            # HTTP endpoint tests (6 tests)
    ├── test_config.py            # Config loading + env interpolation (7 tests)
    └── test_utils.py             # Env/error utility tests (11 tests)
```


## Configuration

All tenant and downstream server definitions live in a single YAML file. See
`examples/tenants.yaml` for the full schema with comments.

**tenants** — each entry defines an `api_key`, `role` (`admin`/`editor`/`viewer`),
`allowed_tools` (glob patterns like `filesystem:*` or `github:create_issue`),
`rate_limit` (requests/min), and which `downstream` servers the tenant may reach.

**downstream_servers** — each entry specifies a `transport` (`stdio` or `sse`)
and the connection details (command + args for stdio, URL for SSE).

**Environment variable interpolation** — `${VAR}` and `${VAR:-default}` syntax
is supported in `api_key`, SSE `url`, and stdio `env` fields. This keeps
secrets out of the config file:

```yaml
tenants:
  acme:
    api_key: "${ACME_API_KEY}"
    # ...

downstream_servers:
  github:
    transport: stdio
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_TOKEN}"

  gdrive:
    transport: sse
    url: "${GDRIVE_URL:-http://localhost:3002/sse}"
```


## Extending

**Add a new downstream** — add an entry to `downstream_servers` in your YAML,
then reference it in each tenant's `downstream` list. The gateway discovers
tools automatically on next boot.

**Custom roles** — the `role` field is validated as `admin|editor|viewer` but
the gateway only uses it for the `/stats` endpoint (admin-only). Tool-level
access is controlled entirely by `allowed_tools` globs, so roles are a
convention for your own policy layer.

**Swap the storage backend** — `UsageLogger` holds records in an in-memory
`deque`. Subclass it and override the recording to write to SQLite, Postgres,
or a metrics backend. The `query()` and `stats_for()` interface stays the same.

**Hot-reload** — call `pipeline.reload_config(new_config)` to update tenant
auth and rate limits without restart, and `pipeline.refresh_tools()` to
re-fetch downstream tool manifests.
