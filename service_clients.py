import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from settings import settings

log = logging.getLogger(__name__)


class STTClient:
    def __init__(self, url: str):
        self.url = url

    async def transcribe(self, audio_bytes: bytes) -> str:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                self.url,
                content=audio_bytes,
                headers={"Content-Type": "audio/webm"},
            )
            resp.raise_for_status()
            return resp.json().get("text", "").strip()


class TTSClient:
    def __init__(self, primary: dict, fallback: dict, response_format: str):
        self.primary = primary
        self.fallback = fallback
        self.response_format = response_format

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        async with httpx.AsyncClient(timeout=120.0) as client:
            try:
                resp = await client.post(
                    self.primary["url"],
                    json={
                        "input": text,
                        "voice": voice or self.primary["voice"],
                        "model": self.primary["model"],
                        "response_format": self.response_format,
                    },
                )
                resp.raise_for_status()
                return resp.content
            except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                log.warning("Primary TTS failed (%s), falling back", exc)

            resp = await client.post(
                self.fallback["url"],
                json={
                    "input": text,
                    "voice": self.fallback["voice"],
                    "model": self.fallback["model"],
                    "response_format": self.response_format,
                },
            )
            resp.raise_for_status()
            return resp.content


class LLMChainClient:
    def __init__(self, chain: list[dict]):
        self.chain = chain

    @asynccontextmanager
    async def stream_chat(self, payload: dict) -> AsyncIterator[httpx.Response]:
        async with httpx.AsyncClient(timeout=120.0) as client:
            for i, entry in enumerate(self.chain):
                try:
                    request_payload = dict(payload)
                    request_payload["model"] = entry["model"]
                    async with client.stream("POST", entry["url"], json=request_payload) as resp:
                        resp.raise_for_status()
                        yield resp
                        return
                except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                    if i < len(self.chain) - 1:
                        log.warning("LLM %s failed (%s), trying next", entry["url"], exc)
                    else:
                        raise

    async def complete(self, payload: dict, *, urls: list[str] | None = None) -> str | None:
        target_urls = urls or [entry["url"] for entry in self.chain]
        model = payload.get("model") or self.chain[0]["model"]
        request_payload = dict(payload)
        request_payload["model"] = model
        async with httpx.AsyncClient(timeout=120.0) as client:
            for url in target_urls:
                try:
                    resp = await client.post(url, json=request_payload)
                    resp.raise_for_status()
                    return resp.json()["choices"][0]["message"]["content"].strip()
                except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError, KeyError, IndexError, json.JSONDecodeError):
                    log.debug("Completion failed via %s", url, exc_info=True)
                    continue
        return None


stt_client = STTClient(settings.stt_url)
tts_client = TTSClient(
    primary={
        "url": settings.tts.url,
        "model": settings.tts.model,
        "voice": settings.tts.voice,
    },
    fallback={
        "url": settings.tts.fallback_url,
        "model": settings.tts.fallback_model,
        "voice": settings.tts.fallback_voice,
    },
    response_format=settings.tts.format,
)
llm_client = LLMChainClient(settings.llm_chain)
