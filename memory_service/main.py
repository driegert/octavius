"""Run the memory service.

    uv run python -m memory_service.main

Host defaults to loopback (Octavius, co-located on triplestuffed, is the only
client today). To let the Pi-agent harness on other machines reach it over the
tailnet, set OCTAVIUS_MEMORY_HOST to the tailscale IP (or 0.0.0.0 behind tailnet
ACLs). Port defaults to 8031 (Octavius itself is 8030).
"""

import os

import uvicorn

from .app import create_app

app = create_app()


def main():
    host = os.environ.get("OCTAVIUS_MEMORY_HOST", "127.0.0.1")
    port = int(os.environ.get("OCTAVIUS_MEMORY_PORT", "8031"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
