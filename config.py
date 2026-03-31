STT_URL = "http://127.0.0.1:8502/api/transcribe"

LLM_URL = "http://127.0.0.1:8001/v1/chat/completions"
LLM_MODEL = "qwen3.5-35b-a3b"

# Fallback LLM (triplestuffed)
LLM_FALLBACK_URL = "http://triplestuffed:8010/v1/chat/completions"
LLM_FALLBACK_MODEL = "qwen3.5-35b-a3b"

TTS_URL = "http://triplestuffed:8020/v1/audio/speech"
TTS_MODEL = "/media/extra_stuff/huggingface/mistralai/Voxtral-4B-TTS-2603"
TTS_VOICE = "de_male"
TTS_FORMAT = "wav"
TTS_VOICES = [
    "de_male",
    "de_female",
    "neutral_male",
    "neutral_female",
    "casual_male",
    "casual_female",
    "cheerful_female",
    "ar_male",
    "es_female",
    "es_male",
    "fr_female",
    "fr_male",
    "hi_female",
    "hi_male",
    "it_female",
    "it_male",
    "nl_female",
    "nl_male",
    "pt_female",
    "pt_male",
]

# Fallback TTS (Kokoro on lilbuddy)
TTS_FALLBACK_URL = "http://lilbuddy:8880/v1/audio/speech"
TTS_FALLBACK_MODEL = "kokoro"
TTS_FALLBACK_VOICE = "bm_lewis"

AGENT_PORT = 8030
MAX_TOOL_ROUNDS = 10
MAX_CONVERSATION_MESSAGES = 40

MCP_SERVERS = {
    "evangeline-email": {
        "transport": "http",
        "url": "http://triplestuffed:8251/mcp",
    },
    "searxng": {
        "transport": "stdio",
        "command": "/home/dave/.local/bin/uv",
        "args": [
            "tool",
            "run",
            "--from",
            "git+https://github.com/varlabz/searxng-mcp",
            "mcp-server",
        ],
        "env": {
            "SEARX_HOST": "https://searxng.riegert.xyz",
            "SSL_CERT_FILE": "/etc/ssl/cert.pem",
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
