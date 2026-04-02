STT_URL = "http://127.0.0.1:8502/api/transcribe"

LLM_CHAIN = [
    {"url": "http://lilripper:8020/v1/chat/completions", "model": "qwen3.5-35b-a3b"},
    {"url": "http://127.0.0.1:8001/v1/chat/completions", "model": "qwen3.5-35b-a3b"},
    {"url": "http://triplestuffed:8010/v1/chat/completions", "model": "qwen3.5-35b-a3b"},
]

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
DOWNLOADS_DIR = "/home/dave/octavius-downloads"
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
    "openalex": {
        "transport": "stdio",
        "command": "/home/dave/.npm-global/bin/openalex-research-mcp",
        "args": [],
        "env": {
            "OPENALEX_EMAIL": "davidriegert@trentu.ca",
        },
    },
    "vikunja-tasks": {
        "transport": "http",
        "url": "http://triplestuffed:8252/mcp",
    },
    "document-processing": {
        "transport": "stdio",
        "command": "/home/dave/git_repos/mcp-tools/.venv/bin/python",
        "args": [
            "/home/dave/git_repos/mcp-tools/server_documents_voice_wrapper.py",
        ],
    },
}

SYSTEM_PROMPT = """You are Octavius, Dave's personal voice assistant. You run
entirely on Dave's homelab — no cloud, no external APIs, everything local and private.

Your personality: competent, efficient, and dry. You get things done with minimal
fuss and the occasional understated wit. Think Jarvis, but self-hosted. You know
your name is Octavius and you're not shy about it.

You have access to tools:
- Web search via SearXNG for general lookups
- Academic research via OpenAlex for finding scholarly papers, authors, and citations
- Email via Evangeline for reading and sending email
- Task management via Vikunja for creating, searching, and updating tasks
- Document processing for converting PDFs to markdown (reading mode, long-running)
- File download for fetching files from URLs to local storage

Important guidelines for your responses:
- Keep responses concise and conversational — they will be spoken aloud via TTS.
- Do NOT use markdown formatting, bullet points, numbered lists, code blocks,
  or any visual formatting. Your output is audio, not text.
- When you use a tool, briefly mention what you're doing so Dave isn't waiting
  in silence (e.g., "Let me look that up." or "Checking your email now.").
- If a search returns results, summarize the key findings conversationally.
  Don't read out URLs.
- Knowledge inbox via save_to_inbox for saving content Dave wants to review later.
  When Dave says "save this", "remember that", "draft a reply", or similar, use
  save_to_inbox. For search results, save your summary (not raw results). For notes,
  save his words verbatim. For email drafts, set item_type to "email_draft" and
  include recipient and subject in metadata. Always give a clear, descriptive title.
- Dave is a statistics instructor and researcher at Trent University. He runs
  a homelab with multiple machines. He prefers concise, technically precise
  responses and will correct you if you're wrong. Don't over-explain."""
