from service_clients import tts_client


async def synthesize(text: str, voice: str | None = None) -> bytes:
    """POST text to TTS. Tries Voxtral first, falls back to Kokoro."""
    return await tts_client.synthesize(text, voice=voice)
