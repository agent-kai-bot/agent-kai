"""Command-line entrypoint for the taskboard compatibility gateway."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    """Run the taskboard gateway with uvicorn."""

    host = os.getenv("TASKBOARD_GATEWAY_HOST", "127.0.0.1")
    port = int(os.getenv("TASKBOARD_GATEWAY_PORT", "18789"))
    uvicorn.run(
        "taskboard_gateway.app:create_gateway_app",
        host=host,
        port=port,
        factory=True,
    )


if __name__ == "__main__":
    main()
