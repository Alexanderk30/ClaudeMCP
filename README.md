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

## Architecture

The gateway is organized into four layers, each implemented as an independent
module so they can be tested and evolved separately.

### Layer 1 — Auth & Permissions (`gateway.middleware.auth`)

Every inbound request carries an API key (passed as a header or in the MCP
session metadata). The auth middleware validates the key against the tenant
registry, resolves the tenant identity and role, and checks whether the
requested tool matches the tenant's `allowed_tools` glob patterns.

Roles are coarse-grained labels (`admin`, `editor`, `viewer`) that downstream
policy can inspect; the gateway itself enforces tool-level allow-lists.

### Layer 2 — Rate Limiting & Usage Logging (`gateway.middleware.rate_limiter`, `gateway.middleware.usage_logger`)

A per-tenant sliding-window rate limiter caps requests per minute according to
each tenant's configured `rate_limit`. The usage logger wraps every tool call
in a context manager that records tenant ID, tool name, latency, and
success/failure. Records are held in memory for now but the interface is ready
to swap in a database or metrics backend.

### Layer 3 — Tool Routing (`gateway.routing.router`)

The router holds the merged tool manifest produced by the aggregator. Tool
names are qualified as `server:tool` (e.g. `github:create_issue`). When a
tenant calls a tool, the router resolves the qualified name back to the
originating downstream server and dispatches via the aggregator.

### Layer 4 — Proxy & Aggregation (`gateway.proxy.aggregator`)

The aggregator manages MCP client connections to every downstream server
(stdio or SSE transport). On startup it connects to each server, fetches its
tool manifest, and merges them into a single namespace. Tool calls are proxied
transparently to the correct downstream session.

## Project Structure

```
mcp-gateway/
├── pyproject.toml              # Dependencies and project metadata
├── README.md
├── examples/
│   └── tenants.yaml            # Sample tenant + downstream config
├── src/
│   └── gateway/
│       ├── __init__.py
│       ├── cli.py              # Click CLI entry point
│       ├── server.py           # Boot sequence — wires all layers
│       ├── config/
│       │   └── loader.py       # YAML config → Pydantic models
│       ├── middleware/
│       │   ├── auth.py         # Layer 1: API key validation & tool ACLs
│       │   ├── rate_limiter.py # Layer 2: sliding-window rate limiter
│       │   └── usage_logger.py # Layer 2b: per-call logging
│       ├── routing/
│       │   └── router.py       # Layer 3: qualified-name → downstream dispatch
│       ├── proxy/
│       │   └── aggregator.py   # Layer 4: downstream connections & manifest merge
│       └── utils/
└── tests/
    ├── test_config.py
    ├── test_auth.py
    └── test_rate_limiter.py
```

## Quick Start

```bash
# 1. Install in dev mode
pip install -e ".[dev]"

# 2. Copy and edit the example config
cp examples/tenants.yaml tenants.yaml
# → fill in real API keys and downstream server paths

# 3. Run the gateway
mcp-gateway --config tenants.yaml --port 8000

# 4. Run the tests
pytest
```

## Configuration

All tenant and downstream server definitions live in a single YAML file. See
`examples/tenants.yaml` for the full schema with comments. The key sections
are:

**tenants** — each entry defines an `api_key`, `role`, `allowed_tools` (glob
patterns), `rate_limit` (requests/min), and which `downstream` servers the
tenant may reach.

**downstream_servers** — each entry specifies a `transport` (`stdio` or `sse`)
and the connection details (command + args for stdio, URL for SSE). Environment
variable interpolation (`${VAR}`) is supported in the `env` map.

## Roadmap

The scaffold is in place. Here is the build order for bringing each layer to
production readiness:

1. **Config loader** — already functional; add env-var interpolation.
2. **Auth middleware** — already functional; add header extraction from MCP session.
3. **Rate limiter** — already functional; add Redis backend option.
4. **Usage logger** — already functional; add SQLite / Prometheus export.
5. **Aggregator** — wire up real `mcp.ClientSession` connections (stdio + SSE).
6. **Router** — integrate with the live aggregator.
7. **Server** — expose the gateway as an MCP server (SSE transport via Starlette).
8. **Integration tests** — spin up mock downstream servers and test end-to-end.
