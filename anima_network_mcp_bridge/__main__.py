"""CLI entry: ``anima-network-mcp`` / ``python -m anima_network_mcp_bridge``."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from anima_network_mcp_bridge.rest_client import (
    DEFAULT_BASE_URL,
    RestClient,
    validate_base_url,
)
from anima_network_mcp_bridge.server import create_mcp_server


def _configure_logging() -> None:
    # Stdio MCP: never write to stdout (protocol wire). Logs → stderr only.
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def main(argv: list[str] | None = None) -> None:
    _configure_logging()
    parser = argparse.ArgumentParser(
        prog="anima-network-mcp",
        description=(
            "Anima Network stdio MCP bridge → public REST /v1/agent/* "
            "(does not use public /mcp). Token: env ANIMA_NETWORK_TOKEN only."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("ANIMA_NETWORK_BASE_URL", DEFAULT_BASE_URL),
        help="API base URL (env ANIMA_NETWORK_BASE_URL; https required except loopback)",
    )
    args = parser.parse_args(argv)

    token = (os.environ.get("ANIMA_NETWORK_TOKEN") or "").strip()
    if not token:
        print(
            "ANIMA_NETWORK_TOKEN is required "
            "(mint at /me after GitHub/Apple login).",
            file=sys.stderr,
        )
        raise SystemExit(2)

    try:
        base_url = validate_base_url(args.base_url)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc

    rest = RestClient(token=token, base_url=base_url)
    mcp = create_mcp_server(rest)
    # Process exits when the host closes stdio; httpx client is process-scoped.
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
