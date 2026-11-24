"""Shared pieces for the WebSocket session modules (``websocket_session`` and ``ws_*``)."""
from __future__ import annotations

from typing import Any

try:
    from fastapi import WebSocket, WebSocketDisconnect
except ModuleNotFoundError:
    WebSocket = Any

    class WebSocketDisconnect(RuntimeError):
        pass
