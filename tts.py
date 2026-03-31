import logging

import httpx

from config import (
    TTS_URL, TTS_MODEL, TTS_VOICE, TTS_FORMAT,
    TTS_FALLBACK_URL, TTS_FALLBACK_MODEL, TTS_FALLBACK_VOICE,
)

log = logging.getLogger(__name__)


async def synthesize(text: str, voice: str | None = None) -> bytes:
    """POST text to TTS. Tries Voxtral first, falls back to Kokoro."""
    async with httpx.AsyncClient(timeout=120.0) as client:
        # Try primary (Voxtral)
        try:
            resp = await client.post(
                TTS_URL,
                json={
                    "input": text,
                    "voice": voice or TTS_VOICE,
                    "model": TTS_MODEL,
                    "response_format": TTS_FORMAT,
                },
            )
            resp.raise_for_status()
            return resp.content
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as e:
            log.warning("Primary TTS failed (%s), falling back to Kokoro", e)

        # Fallback (Kokoro)
        resp = await client.post(
            TTS_FALLBACK_URL,
            json={
                "input": text,
                "voice": TTS_FALLBACK_VOICE,
                "model": TTS_FALLBACK_MODEL,
                "response_format": TTS_FORMAT,
            },
        )
        resp.raise_for_status()
        return resp.content
