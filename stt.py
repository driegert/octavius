import httpx

from config import STT_URL


async def transcribe(audio_bytes: bytes) -> str:
    """POST webm/opus audio to Whisper, return transcribed text."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            STT_URL,
            content=audio_bytes,
            headers={"Content-Type": "audio/webm"},
        )
        resp.raise_for_status()
        return resp.json().get("text", "").strip()
