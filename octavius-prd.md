# Octavius — Voice Assistant PRD

## What Is This

Octavius is a self-hosted voice assistant running entirely on Dave's homelab.
You speak to it in a browser, it transcribes your speech, reasons about it
(with access to web search and email tools), and speaks the answer back to you
in a natural voice.

Everything is local. No cloud APIs. No data leaves the Tailnet.

## Architecture Overview

```
Browser (octavius.riegert.xyz)
    │
    │  WebSocket (wss://)
    ▼
FastAPI Agent — lilbuddy:8030
    │
    ├──► STT: lilbuddy:8010/api/transcribe     (Whisper large-v3, ROCm)
    ├──► LLM: triplestuffed:8010/v1/chat/completions  (Qwen3.5-35B-A3B, llama.cpp)
    ├──► TTS: triplestuffed:8020/v1/audio/speech       (Voxtral 4B TTS, vLLM-Omni)
    └──► MCP Servers:
           ├─ evangeline-email: http://triplestuffed:8251/mcp  (streamable HTTP)
           └─ searxng: stdio subprocess via uv                  (local on lilbuddy)
```

## Endpoint Reference

| Service | URL | Protocol | Notes |
|---------|-----|----------|-------|
| **STT** | `http://lilbuddy:8010/api/transcribe` | POST, accepts `audio/webm` | Returns `{"text": "..."}` |
| **LLM** | `http://triplestuffed:8010/v1/chat/completions` | OpenAI-compatible | Model: `qwen3.5-35b-a3b`, `--jinja` enabled, 65K context |
| **TTS** | `http://triplestuffed:8020/v1/audio/speech` | OpenAI-compatible | See TTS config below |
| **Email MCP** | `http://triplestuffed:8251/mcp` | MCP streamable HTTP | Evangeline email server |
| **SearXNG MCP** | stdio subprocess | MCP stdio | Spawned via `uv` on lilbuddy |

### TTS Configuration

```python
TTS_URL = "http://triplestuffed:8020/v1/audio/speech"
TTS_MODEL = "/media/extra_stuff/huggingface/mistralai/Voxtral-4B-TTS-2603"
TTS_VOICE = "de_male"
TTS_FORMAT = "wav"
```

Output: 24 kHz WAV. Response time ~4-6s for 2-3 sentences. No authentication.

### MCP Server Configs

```python
MCP_SERVERS = {
    "evangeline-email": {
        "transport": "http",
        "url": "http://triplestuffed:8251/mcp",
    },
    "searxng": {
        "transport": "stdio",
        "command": "/usr/bin/uv",
        "args": [
            "tool", "run",
            "--from", "git+https://github.com/varlabz/searxng-mcp",
            "mcp-server",
        ],
        "env": {
            "SEARX_HOST": "https://searxng.riegert.xyz",
            "SSL_CERT_FILE": "/usr/lib/ssl/cert.pem",
        },
    },
}
```

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Audio format (browser → STT) | webm/opus | Browser default from MediaRecorder, Whisper accepts it, no conversion needed |
| LLM thinking mode | Thinking mode ON | Smarter tool use and reasoning; strip `<think>...</think>` tags before sending to TTS |
| Conversation style | Continuous (maintains history) | More natural voice assistant experience |
| MCP integration | Full MCP client library (`mcp` SDK) | Extensible — can add new MCP servers without changing agent code |
| Concurrent users | Single user only | Simplifies design — no session management needed |
| Browser UI | Minimal — push-to-talk, status, transcript | Not a full chat UI; optimized for voice interaction |
| Framework | FastAPI + WebSocket | Real-time bidirectional communication for status updates |

## Project Structure

```
~/voice-assistant/
├── pyproject.toml
├── .python-version              # 3.11
├── config.py                    # All endpoint URLs, voice settings, model paths
├── main.py                      # FastAPI app, WebSocket endpoint, serves UI
├── agent.py                     # The agentic loop: LLM ↔ tool calls
├── mcp_manager.py               # MCP client lifecycle (stdio + HTTP sessions)
├── stt.py                       # Whisper client wrapper
├── tts.py                       # Voxtral client wrapper
├── conversation.py              # Conversation history management
├── static/
│   └── index.html               # Browser UI (mic, audio player, status)
└── README.md
```

## Dependencies

```toml
[project]
name = "voice-assistant"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.34",
    "httpx>=0.28",
    "websockets>=14",
    "mcp>=1.8",
]
```

Install with `uv sync`. Run with `uv run python main.py`.

## Component Specifications

### 1. `config.py`

All tunables in one place. No hardcoded URLs anywhere else.

```python
STT_URL = "http://lilbuddy:8010/api/transcribe"

LLM_URL = "http://triplestuffed:8010/v1/chat/completions"
LLM_MODEL = "qwen3.5-35b-a3b"

TTS_URL = "http://triplestuffed:8020/v1/audio/speech"
TTS_MODEL = "/media/extra_stuff/huggingface/mistralai/Voxtral-4B-TTS-2603"
TTS_VOICE = "de_male"
TTS_FORMAT = "wav"

AGENT_PORT = 8030
MAX_TOOL_ROUNDS = 10
MAX_CONVERSATION_MESSAGES = 40  # keep ~40 messages + system prompt to stay within 65K context

MCP_SERVERS = {
    "evangeline-email": {
        "transport": "http",
        "url": "http://triplestuffed:8251/mcp",
    },
    "searxng": {
        "transport": "stdio",
        "command": "/usr/bin/uv",
        "args": [
            "tool", "run",
            "--from", "git+https://github.com/varlabz/searxng-mcp",
            "mcp-server",
        ],
        "env": {
            "SEARX_HOST": "https://searxng.riegert.xyz",
            "SSL_CERT_FILE": "/usr/lib/ssl/cert.pem",
        },
    },
}

SYSTEM_PROMPT = """You are Octavius, Dave's personal voice assistant. You run
entirely on Dave's homelab — no cloud, no external APIs, everything local and private.

Your personality: competent, efficient, and dry. You get things done with minimal
fuss and the occasional understated wit. Think Jarvis, but self-hosted. You know
your name is Octavius and you're not shy about it.

You have access to tools:
- Web search via SearXNG for looking things up
- Email via Evangeline for reading and sending email

Important guidelines for your responses:
- Keep responses concise and conversational — they will be spoken aloud via TTS.
- Do NOT use markdown formatting, bullet points, numbered lists, code blocks,
  or any visual formatting. Your output is audio, not text.
- When you use a tool, briefly mention what you're doing so Dave isn't waiting
  in silence (e.g., "Let me look that up." or "Checking your email now.").
- If a search returns results, summarize the key findings conversationally.
  Don't read out URLs.
- Dave is a statistics instructor and researcher at Trent University. He runs
  a homelab with multiple machines. He prefers concise, technically precise
  responses and will correct you if you're wrong. Don't over-explain."""
```

### 2. `mcp_manager.py`

Uses the official `mcp` Python SDK. Manages connections to all MCP servers.

**Responsibilities:**
- Connect to Evangeline via `streamablehttp_client(url)`
- Spawn SearXNG via `stdio_client(StdioServerParameters(...))`
- Call `session.list_tools()` on each to discover available tools
- Convert MCP tool schemas to OpenAI function-calling format for Qwen3.5
- Provide a `call_tool(name, arguments)` method that routes to the correct server
- Maintain a mapping of `tool_name → server_name` for routing
- Use `AsyncExitStack` to manage the lifecycle of all connections
- Connect on app startup, disconnect on shutdown

**Key implementation details:**
- Both `stdio_client` and `streamablehttp_client` are async context managers
  from `mcp.client.stdio` and `mcp.client.streamable_http` respectively
- Each yields `(read_stream, write_stream)` which you pass to `ClientSession`
- `ClientSession.initialize()` must be called before listing tools
- `ClientSession.call_tool(name, arguments=dict)` executes a tool call
- Tool results come back as `CallToolResult` with a `.content` list of blocks;
  extract `.text` from text blocks

**MCP → OpenAI tool format conversion:**
```python
{
    "type": "function",
    "function": {
        "name": mcp_tool.name,
        "description": mcp_tool.description or "",
        "parameters": mcp_tool.inputSchema or {"type": "object", "properties": {}},
    },
}
```

### 3. `stt.py`

Simple async HTTP wrapper around the Whisper endpoint.

```python
async def transcribe(audio_bytes: bytes) -> str:
    """POST webm/opus audio to Whisper, return transcribed text."""
    # POST to STT_URL with Content-Type: audio/webm
    # Parse response JSON for "text" field
    # Return stripped text, or empty string on failure
```

Timeout: 30 seconds. Content-Type: `audio/webm`.

### 4. `tts.py`

Simple async HTTP wrapper around the Voxtral endpoint.

```python
async def synthesize(text: str) -> bytes:
    """POST text to Voxtral TTS, return raw WAV audio bytes."""
    # POST to TTS_URL with JSON payload:
    #   input, voice, model, response_format
    # Return raw response bytes (not JSON — Voxtral returns audio directly)
```

Timeout: 120 seconds (long responses take time to synthesize).

**Important:** If the response text is very long (> ~500 characters), consider
truncating or splitting. Voxtral handles up to ~2 minutes of audio natively,
but longer texts increase latency significantly.

### 5. `conversation.py`

Manages chat history for the single user session.

**Responsibilities:**
- Initialize with system prompt
- `add_user(text)` — append user message
- `add_assistant(text)` — append assistant response
- `add_tool_call(tool_call_id, name, arguments_str)` — append tool call message
- `add_tool_result(tool_call_id, content)` — append tool result
- `get_messages()` — return full message list
- `trim()` — keep system prompt + last N messages to stay within 65K context
- `reset()` — clear history back to just the system prompt

**Context management:** With 65K context and Qwen3.5, keep max ~40 non-system
messages. This gives room for multi-turn conversations with tool calls. Each
tool call round uses multiple messages (assistant tool_call + tool result), so
actual conversational turns will be fewer.

### 6. `agent.py`

The core agentic loop. Processes one user turn through the LLM, handling
tool calls iteratively until a final text response is produced.

**Flow:**
1. Add user text to conversation history
2. POST conversation messages + tool definitions to LLM
3. Check response:
   - If `tool_calls` present → execute each via `mcp_manager.call_tool()`,
     add tool call + result to history, loop back to step 2
   - If plain text content → strip `<think>...</think>` tags, return text
4. Safety limit: max 10 tool call rounds, then return a fallback message

**`<think>` tag stripping:**
Qwen3.5 in thinking mode wraps reasoning in `<think>...</think>`. This MUST
be stripped before returning text to TTS — otherwise Octavius will literally
read out the reasoning traces. Use a regex: `re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()`

**Tool call ID handling:**
llama.cpp may or may not generate `tool_call.id` fields. If the id is missing
or empty, generate a UUID (e.g., `f"call_{uuid.uuid4().hex[:8]}"`). The id is
needed to match tool results back to tool calls in the conversation history.

**Error handling:**
- If a tool call fails, return the error as the tool result (don't crash)
- If the LLM returns an unparseable response, return a generic error message
- If the LLM endpoint is down, inform the user via WebSocket status message

### 7. `main.py`

FastAPI application with WebSocket endpoint and static file serving.

**Lifecycle:**
- On startup: instantiate `MCPManager`, call `connect_all()`, instantiate
  single `Conversation`
- On shutdown: call `disconnect_all()`

**Endpoints:**
- `GET /` → serve `static/index.html`
- `GET /health` → return 200 (for monitoring)
- `WS /ws` → the main voice interaction loop

**WebSocket protocol:**
The WebSocket carries two types of messages:

Browser → Server:
- Binary frames: raw webm/opus audio blobs from MediaRecorder

Server → Browser:
- JSON frames: `{"type": "status", "text": "Transcribing..."}` — status updates
- JSON frames: `{"type": "transcript", "text": "what you said"}` — user's transcribed text
- JSON frames: `{"type": "response", "text": "Octavius's reply"}` — assistant response text
- Binary frames: raw WAV audio bytes for playback

**WebSocket flow per turn:**
1. Receive binary (audio) from browser
2. Send status: "Transcribing..."
3. POST audio to Whisper → get text
4. Send transcript: user's words
5. Send status: "Thinking..."
6. Run agent loop (may send interim statuses for tool calls)
7. Send response: Octavius's text reply
8. Send status: "Speaking..."
9. POST text to Voxtral → get WAV bytes
10. Send binary (WAV) to browser
11. Browser auto-plays audio

**Conversation management:**
- Single `Conversation` instance (single user)
- Trim history on every turn to stay within context limits
- Provide a way to reset (e.g., browser sends a JSON `{"type": "reset"}` message)

### 8. `static/index.html`

Minimal, functional browser UI. Not a chat interface — this is a voice interface.

**UI Elements:**
- Large push-to-talk button (hold or toggle — implementer's choice)
- Status indicator showing current state (Ready / Recording / Transcribing / Thinking / Speaking)
- Text area showing the last transcript (what Dave said) and response (what Octavius said)
- A "Reset conversation" button to clear history
- Simple, dark theme, minimal design

**Technical requirements:**
- **MediaRecorder API** to capture mic audio as webm/opus
- **WebSocket** connection to `wss://octavius.riegert.xyz/ws`
- When recording stops, send the audio blob as a binary WebSocket message
- Listen for JSON messages (status/transcript/response) and binary messages (audio)
- On receiving binary audio, create a Blob URL with type `audio/wav` and play
  it via an `<audio>` element or `new Audio(url).play()`
- **Auto-reconnect** WebSocket on disconnect (simple retry with backoff)
- Handle the HTTPS/mic permission requirement (Caddy provides the cert)

**Audio playback considerations:**
- The WAV from Voxtral is 24 kHz. Modern browsers handle this fine.
- Auto-play may be blocked by browser policy on first interaction — the push-to-talk
  button serves as user gesture, which should satisfy autoplay requirements.

## Caddy Configuration

Dave will handle this himself. The setup is:
- `octavius.riegert.xyz` → reverse proxy to `lilbuddy:8030`
- This provides HTTPS (required for mic access in the browser)
- Caddy with local CA certs on lilbuddy, same pattern as other `.riegert.xyz` subdomains

## Build Order

Follow this order. Test each step before moving to the next.

### Step 1: Scaffold
```bash
cd ~
mkdir voice-assistant && cd voice-assistant
uv init
uv add fastapi "uvicorn[standard]" httpx websockets mcp
mkdir static
```

### Step 2: config.py
Create with all endpoints, model names, and system prompt as specified above.

### Step 3: stt.py + tts.py
Simple HTTP wrappers. Test independently:
```bash
# Record a test clip, send to Whisper
# Send test text to Voxtral, play the output
```

### Step 4: mcp_manager.py
Connect to both MCP servers, list tools, print their names.
Test with a standalone script before integrating.

### Step 5: conversation.py
Straightforward data structure. No external dependencies.

### Step 6: agent.py
The agentic loop. Test with hardcoded text input first (no audio):
```python
# Quick test:
result = await run_agent_turn(conversation, mcp_manager, "Search for the weather in Peterborough Ontario")
print(result)
```
Verify tool calls work, `<think>` tags are stripped, conversation history accumulates.

### Step 7: main.py
Wire up FastAPI + WebSocket. Test with `wscat` or a simple Python WebSocket client
before building the browser UI.

### Step 8: static/index.html
Browser UI. Open `https://octavius.riegert.xyz`, press the button, speak.

### Step 9: Integration test
Full end-to-end: speak → transcribe → think → tool calls → respond → hear audio.

## Testing Commands

```bash
# Test STT independently
curl -X POST http://lilbuddy:8010/api/transcribe \
  -H "Content-Type: audio/webm" \
  --data-binary @test_recording.webm

# Test LLM independently
curl http://triplestuffed:8010/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.5-35b-a3b",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "What is 2+2?"}
    ]
  }'

# Test TTS independently
curl -X POST http://triplestuffed:8020/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{
    "input": "Good evening, Dave. Octavius here. All systems operational.",
    "voice": "de_male",
    "model": "/media/extra_stuff/huggingface/mistralai/Voxtral-4B-TTS-2603",
    "response_format": "wav"
  }' --output test_octavius.wav

# Test MCP connections
uv run python -c "
import asyncio
from mcp_manager import MCPManager
from config import MCP_SERVERS

async def main():
    m = MCPManager(MCP_SERVERS)
    await m.connect_all()
    for t in m.tools:
        print(f\"  {t['function']['name']}: {t['function']['description'][:80]}\")
    await m.disconnect_all()

asyncio.run(main())
"
```

## Known Gotchas

1. **`<think>` tags in TTS**: If these aren't stripped, Octavius will literally
   say "think... the user is asking about..." which is terrible. The regex strip
   in agent.py is critical.

2. **Tool call IDs**: llama.cpp may return empty or missing tool call IDs.
   Generate a fallback UUID if this happens.

3. **Conversation context overflow**: 65K context fills up fast with tool call
   results (search results can be verbose). Trim aggressively. Consider
   summarizing tool results before adding to history if they're very long
   (e.g., truncate search results to first 2000 characters).

4. **MediaRecorder MIME type**: Some browsers report `audio/webm;codecs=opus`
   as the MIME type. Make sure the Content-Type header sent to Whisper is
   just `audio/webm` (or whatever your Whisper endpoint expects).

5. **WebSocket binary vs text**: FastAPI WebSockets distinguish between
   `receive_bytes()` and `receive_text()`. Browser sends audio as binary
   and control messages (like reset) as text/JSON. Handle both.

6. **TTS latency for long responses**: If Octavius gets chatty (>500 chars),
   TTS synthesis will take 10+ seconds. Consider either instructing the model
   to be brief (system prompt does this) or chunking long responses.

7. **Voxtral apostrophe bug**: Avoid sending text with shell-problematic
   characters to Voxtral via curl. The Python `httpx` path handles this fine,
   but be aware during manual testing.

8. **SearXNG MCP cold start**: The first search may be slow because `uv tool run`
   needs to install/resolve the package. Subsequent calls should be fast since
   the stdio process stays alive for the session.
