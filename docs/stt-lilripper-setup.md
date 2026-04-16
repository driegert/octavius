# faster-whisper STT Service on lilripper

Lightweight STT service using faster-whisper + Silero VAD on NVIDIA GPUs.
Drop-in replacement for the voicenotes `/api/transcribe` endpoint — same
API shape, so Octavius needs no client changes.

## Why faster-whisper?

- **~4x faster** than OpenAI Whisper (CTranslate2 backend on NVIDIA CUDA)
- **~1.5 GB VRAM** for large-v3 with int8 quantization (vs ~10 GB for standard Whisper)
- **Built-in Silero VAD** — automatically trims silence, segments speech
- **Same model quality** — uses the same large-v3 weights, just runs them faster

## Setup

### 1. Create project directory

```bash
mkdir -p ~/stt-service && cd ~/stt-service
```

### 2. Create virtual environment with CUDA PyTorch

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip

# Install faster-whisper (pulls in CTranslate2 with CUDA support)
pip install faster-whisper

# Install FastAPI + Uvicorn for the HTTP service
pip install fastapi uvicorn python-multipart
```

### 3. Download the model (first run does this automatically, but you can pre-download)

```bash
python3 -c "from faster_whisper import WhisperModel; WhisperModel('large-v3', device='cuda', compute_type='int8_float16')"
```

This downloads ~3 GB and caches it in `~/.cache/huggingface/`.

### 4. Create the service script

Save this as `~/stt-service/stt_server.py`:

```python
"""Lightweight STT HTTP service using faster-whisper + Silero VAD."""

import io
import os
import tempfile
import logging

import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from faster_whisper import WhisperModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# --- Configuration (env-overridable) ---
MODEL_SIZE = os.environ.get("STT_MODEL", "large-v3")
DEVICE = os.environ.get("STT_DEVICE", "cuda")
COMPUTE_TYPE = os.environ.get("STT_COMPUTE_TYPE", "int8_float16")
LANGUAGE = os.environ.get("STT_LANGUAGE", "en")
PORT = int(os.environ.get("STT_PORT", "8502"))
# Set CUDA_VISIBLE_DEVICES to pin to a specific GPU if needed

# --- Load model ---
log.info("Loading %s on %s (%s)...", MODEL_SIZE, DEVICE, COMPUTE_TYPE)
model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE)
log.info("Model loaded.")

app = FastAPI(title="faster-whisper STT")


def transcribe_audio(audio_path: str) -> str:
    """Transcribe an audio file with VAD filtering."""
    segments, info = model.transcribe(
        audio_path,
        language=LANGUAGE,
        beam_size=3,
        vad_filter=True,
        vad_parameters=dict(
            threshold=0.5,
            min_silence_duration_ms=500,
            speech_pad_ms=300,
        ),
    )
    return " ".join(seg.text.strip() for seg in segments).strip()


def transcribe_pcm(pcm_bytes: bytes) -> str:
    """Transcribe raw float32 PCM at 16kHz."""
    audio = np.frombuffer(pcm_bytes, dtype=np.float32)
    segments, info = model.transcribe(
        audio,
        language=LANGUAGE,
        beam_size=3,
        vad_filter=True,
        vad_parameters=dict(
            threshold=0.5,
            min_silence_duration_ms=500,
            speech_pad_ms=300,
        ),
    )
    return " ".join(seg.text.strip() for seg in segments).strip()


@app.post("/api/transcribe")
async def api_transcribe(request: Request):
    """Transcribe audio. Accepts audio files, raw PCM, or base64 JSON.

    Compatible with the voicenotes /api/transcribe endpoint that Octavius
    already uses — same content-type handling, same response shape.
    """
    import base64

    content_type = request.headers.get("content-type", "")

    try:
        if "application/json" in content_type:
            body = await request.json()
            b64 = body.get("audio", "")
            raw = base64.b64decode(b64)
            audio = np.frombuffer(raw, dtype=np.float32)
            text = transcribe_pcm(raw)

        elif "application/octet-stream" in content_type:
            raw = await request.body()
            text = transcribe_pcm(raw)

        else:
            # Audio file (webm, wav, mp3, ogg, etc.)
            raw = await request.body()
            ext = ".webm"
            if "wav" in content_type:
                ext = ".wav"
            elif "mp3" in content_type or "mpeg" in content_type:
                ext = ".mp3"
            elif "ogg" in content_type:
                ext = ".ogg"

            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
                f.write(raw)
                tmp_path = f.name
            try:
                text = transcribe_audio(tmp_path)
            finally:
                os.unlink(tmp_path)

        return JSONResponse({"text": text})

    except Exception as e:
        log.exception("Transcription failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/health")
async def health():
    return {"status": "ok", "model": MODEL_SIZE, "device": DEVICE, "compute_type": COMPUTE_TYPE}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
```

### 5. Test it

```bash
cd ~/stt-service
source .venv/bin/activate

# Start the server
python stt_server.py

# In another terminal, test with a WAV file:
curl -X POST -H "Content-Type: audio/wav" --data-binary @test.wav http://localhost:8502/api/transcribe

# Test health:
curl http://localhost:8502/health
```

### 6. Create a systemd service

```bash
cat > ~/.config/systemd/user/stt-faster-whisper.service << 'EOF'
[Unit]
Description=faster-whisper STT Service
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/dave/stt-service
ExecStart=/home/dave/stt-service/.venv/bin/python stt_server.py
Restart=always
RestartSec=5
Environment=STT_MODEL=large-v3
Environment=STT_DEVICE=cuda
Environment=STT_COMPUTE_TYPE=int8_float16
Environment=STT_LANGUAGE=en
Environment=STT_PORT=8502

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable stt-faster-whisper
systemctl --user start stt-faster-whisper
```

### 7. Point Octavius at it

On lilbuddy, set the environment variable (or update `.env`):

```bash
OCTAVIUS_STT_URL=http://lilripper:8502/api/transcribe
```

Or for primary/fallback (once we add STT failover):
- Primary: `http://lilripper:8502/api/transcribe` (faster-whisper, fast)
- Fallback: `http://127.0.0.1:8502/api/transcribe` (voicenotes Whisper on lilbuddy, slower but local)

## Expected performance

| Metric | Current (lilbuddy ROCm) | faster-whisper (lilripper CUDA) |
|--------|------------------------|-------------------------------|
| Model | Whisper large-v3 | Whisper large-v3 (same quality) |
| 5s audio | ~2s | ~0.5s |
| 15s audio | ~5-7s | ~1-2s |
| 30s audio | ~10-15s | ~3-4s |
| VRAM | ~10 GB (PyTorch) | ~1.5 GB (int8 CTranslate2) |
| VAD | None (manual silence detect) | Silero VAD built-in |

## Configuration options

All configurable via environment variables:

- `STT_MODEL`: `large-v3` (best quality), `distil-large-v3` (faster, slightly lower quality), `medium` (faster still)
- `STT_DEVICE`: `cuda` (default), `cuda:0` through `cuda:4` to pin to a specific 3090
- `STT_COMPUTE_TYPE`: `int8_float16` (recommended), `float16`, `int8`
- `STT_LANGUAGE`: `en` (skip language detection for speed), or omit for auto-detect
- `STT_PORT`: default `8502`

## Notes

- First request after startup takes ~1-2s extra (CUDA warmup). Subsequent requests are fast.
- With `vad_filter=True`, Silero VAD automatically skips silence segments, so trailing silence in recordings won't produce phantom text.
- The `beam_size=3` matches what voicenotes uses. Increase to 5 for slightly better accuracy at the cost of speed.
- If you want to restrict to one GPU: `CUDA_VISIBLE_DEVICES=0 python stt_server.py`
