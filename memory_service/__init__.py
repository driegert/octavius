"""Octavius memory service — FastAPI over the shared `memory/` module."""

from .app import create_app

__all__ = ["create_app"]
