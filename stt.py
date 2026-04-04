from service_clients import stt_client


async def transcribe(audio_bytes: bytes) -> str:
    """POST webm/opus audio to Whisper, return transcribed text."""
    return await stt_client.transcribe(audio_bytes)
