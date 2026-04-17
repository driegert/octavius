from service_clients import tts_client


async def synthesize(text: str, voice: str | None = None) -> bytes:
    """POST text to TTS. Kokoro voices go direct to Kokoro; all others go
    through Voxtral-with-Kokoro-fallback via the circuit breaker."""
    return await tts_client.synthesize(text, voice=voice)
