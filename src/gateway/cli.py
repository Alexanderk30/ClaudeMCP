"""CLI entry point for the MCP Gateway."""

from __future__ import annotations

import click


@click.command()
@click.option(
    "--config",
    "-c",
    default="tenants.yaml",
    type=click.Path(exists=True),
    help="Path to the tenant configuration file.",
)
@click.option("--host", default="0.0.0.0", help="Host to bind the gateway server.")
@click.option("--port", default=8000, type=int, help="Port to bind the gateway server.")
@click.option("--log-level", default="info", type=click.Choice(["debug", "info", "warning", "error"]))
def main(config: str, host: str, port: int, log_level: str) -> None:
    """Start the Multi-Tenant MCP Gateway."""
    import asyncio

    from gateway.server import run_gateway

    asyncio.run(run_gateway(config_path=config, host=host, port=port, log_level=log_level))


if __name__ == "__main__":
    main()
