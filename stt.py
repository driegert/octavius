from service_clients import stt_client


async def transcribe(audio_bytes: bytes) -> str:
    """POST webm/opus audio to Whisper, return transcribed text."""
    return await stt_client.transcribe(audio_bytes)


async def transcribe_pcm(pcm_bytes: bytes) -> str:
    """POST raw float32 PCM audio (16kHz) to Whisper, return transcribed text."""
    return await stt_client.transcribe_pcm(pcm_bytes)
