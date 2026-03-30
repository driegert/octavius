import httpx

from config import TTS_URL, TTS_MODEL, TTS_VOICE, TTS_FORMAT


async def synthesize(text: str, voice: str | None = None) -> bytes:
    """POST text to Voxtral TTS, return raw WAV audio bytes."""
    async with httpx.AsyncClient(timeout=120.0) as client:
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
