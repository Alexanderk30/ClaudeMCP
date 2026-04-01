"""CLI entry point — thin wrapper that boots the async server."""

from __future__ import annotations

import click


@click.command()
@click.option(
    "--config", "-c",
    default="tenants.yaml",
    type=click.Path(exists=True),
    help="Tenant configuration file.",
)
@click.option("--host", default="0.0.0.0", help="Bind host.")
@click.option("--port", default=8000, type=int, help="Bind port.")
@click.option("--log-level", default="info",
              type=click.Choice(["debug", "info", "warning", "error"]))
def main(config: str, host: str, port: int, log_level: str) -> None:
    """Start the MCP Gateway."""
    import asyncio
    from gateway.server import run_gateway

    asyncio.run(run_gateway(config_path=config, host=host, port=port, log_level=log_level))


if __name__ == "__main__":
    main()
