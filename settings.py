import json
import os
from dataclasses import dataclass


def _env_str(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw is not None else default


def _env_json(name: str, default):
    raw = os.getenv(name)
    return json.loads(raw) if raw else default


@dataclass(frozen=True)
class TTSSettings:
    url: str
    model: str
    voice: str
    format: str
    voices: list[str]
    voxtral_voices: list[str]
    kokoro_voices: list[str]
    fallback_url: str
    fallback_model: str
    fallback_voice: str


@dataclass(frozen=True)
class ReaderSettings:
    directory: str
    llm_url: str
    llm_model: str


@dataclass(frozen=True)
class Settings:
    stt_url: str
    llm_chain: list[dict]
    tts: TTSSettings
    reader: ReaderSettings
    agent_port: int
    downloads_dir: str
    max_tool_rounds: int
    max_conversation_messages: int
    tool_labels: dict[str, str]
    mcp_servers: dict[str, dict]
    system_prompt: str
    summary_url: str
    summary_fallback_url: str
    summary_model: str
    summary_timeout: int
    embedding_chain: list[dict]
    embedding_timeout: int
    result_summary_max_chars: int
    tag_generation_min_messages: int


DEFAULT_VOXTRAL_VOICES = [
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

DEFAULT_KOKORO_VOICES = [
    # American English
    "af_heart", "af_alloy", "af_aoede", "af_bella", "af_jessica",
    "af_kore", "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam",
    "am_michael", "am_onyx", "am_puck", "am_santa",
    # British English
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
]


DEFAULT_TOOL_LABELS = {
    "search": "Web Search",
    "search_emails": "Email Search",
    "semantic_search": "Email Search",
    "get_email": "Reading Email",
    "get_emails": "Reading Emails",
    "get_conversation": "Reading Email Thread",
    "list_conversations": "Listing Email Threads",
    "email_stats": "Email Stats",
    "find_similar_responses": "Finding Similar Emails",
    "extract_from_emails": "Extracting from Emails",
    "search_works": "Academic Search",
    "get_work": "Reading Paper",
    "get_related_works": "Finding Related Papers",
    "search_by_topic": "Topic Search",
    "autocomplete_search": "Academic Search",
    "get_work_citations": "Finding Citations",
    "get_work_references": "Finding References",
    "get_citation_network": "Citation Network",
    "get_top_cited_works": "Top Cited Papers",
    "search_authors": "Author Search",
    "get_author_works": "Author's Papers",
    "get_author_collaborators": "Author Collaborators",
    "search_institutions": "Institution Search",
    "analyze_topic_trends": "Topic Trends",
    "compare_research_areas": "Comparing Research Areas",
    "get_trending_topics": "Trending Topics",
    "analyze_geographic_distribution": "Geographic Analysis",
    "get_entity": "OpenAlex Lookup",
    "search_sources": "Journal Search",
    "list_journal_presets": "Journal Presets",
    "search_in_journal_list": "Journal Search",
    "search_works_in_venue": "Venue Search",
    "get_top_venues_for_field": "Top Venues",
    "check_venue_quality": "Venue Quality Check",
    "get_author_profile": "Author Profile",
    "search_authors_by_expertise": "Expert Search",
    "find_review_articles": "Finding Reviews",
    "find_seminal_papers": "Finding Seminal Papers",
    "batch_resolve_references": "Resolving References",
    "find_open_access_version": "Finding Open Access",
    "health_check": "Health Check",
    "search_tasks": "Searching Tasks",
    "get_task": "Reading Task",
    "create_task": "Creating Task",
    "update_task": "Updating Task",
    "list_projects": "Listing Projects",
    "list_labels": "Listing Labels",
    "add_label_to_task": "Adding Label",
    "remove_label_from_task": "Removing Label",
    "get_task_comments": "Reading Comments",
    "add_task_comment": "Adding Comment",
    "convert_pdf_to_md": "Converting PDF",
    "get_conversion_result": "Checking PDF Conversion",
    "download_file": "Downloading File",
    "save_to_stash": "Saving to Stash",
    "list_stash_items": "Listing Stash",
    "read_document": "Preparing Document",
    "list_reader_documents": "Listing Reader Docs",
    "read_item_content": "Reading Item Content",
    "process_pdf": "Processing PDF",
    "delegate_task": "Delegating...",
}


DEFAULT_MCP_SERVERS = {
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
            # Pinned to commit; update deliberately by bumping this ref.
            "git+https://github.com/varlabz/searxng-mcp@f9797b7db6593082a331e02d852029f8ebbe6a9d",
            "mcp-server",
        ],
        "env": {
            "SEARX_HOST": "https://searxng.riegert.xyz",
            "SSL_CERT_FILE": "/etc/ssl/cert.pem",
        },
    },
    "openalex": {
        "transport": "stdio",
        "command": "/usr/bin/npx",
        # Pinned version; update deliberately by bumping the @x.y.z suffix.
        "args": ["-y", "openalex-research-mcp@0.4.0"],
        "env": {
            "OPENALEX_EMAIL": "davidriegert@trentu.ca",
        },
        "tool_allowlist": [
            # Core search
            "search_works",
            "get_work",
            "find_open_access_version",
            "search_by_topic",
            "find_review_articles",
            "find_seminal_papers",
            "get_related_works",
            "get_top_cited_works",
            # Citations
            "get_citation_network",
            # Authors
            "search_authors",
            "get_author_profile",
            "search_authors_by_expertise",
            # Utility
            "batch_resolve_references",
            "get_entity",
        ],
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


VIKUNJA_PROJECTS: dict[str, int] = {
    "Inbox": 1,
    "Teaching and Trent": 9,
    "math1052": 10,
    "amod5240": 2,
    "math3560": 3,
    "Email Tasks": 14,
    "Personal and Professional": 13,
    "PhD": 4,
    "Projects": 5,
    "AI Projects": 6,
    "SSC 2026 Workshop": 11,
    "Exploration": 8,
}
VIKUNJA_DEFAULT_PROJECT = "Inbox"


def format_vikunja_projects() -> str:
    return ", ".join(f"{name} (id={pid})" for name, pid in VIKUNJA_PROJECTS.items())


def format_vikunja_default() -> str:
    pid = VIKUNJA_PROJECTS[VIKUNJA_DEFAULT_PROJECT]
    return f"{VIKUNJA_DEFAULT_PROJECT} (id={pid})"


_RAW_SYSTEM_PROMPT = """You are Octavius, Dave's personal voice assistant. You run
entirely on Dave's homelab — no cloud, no external APIs, everything local and private.

Your personality: competent, efficient, and dry. You get things done with minimal
fuss and the occasional understated wit. Think Jarvis, but self-hosted. You know
your name is Octavius and you're not shy about it.

You have access to tools:
- Web search via SearXNG for general lookups
- delegate_task for email, research, and task management. This hands off to a
  specialist assistant with its own tools. Use it when Dave asks about:
  * Email: "check my email", "find emails from X", "any emails about Y" →
    delegate_task(domain="email", task="..."). Include dates, senders, or
    topics Dave mentioned.
  * Research: "find papers about X", "who publishes on Y", "citations for Z" →
    delegate_task(domain="research", task="..."). Include topic details.
  * Tasks: "add a task", "what's on my list", "mark X as done" →
    delegate_task(domain="tasks", task="..."). Include project names if Dave
    specified one. Key projects: {vikunja_projects}.
    Default to {vikunja_default} if Dave doesn't specify a project.
  Write a clear, complete task description — the specialist only sees what you
  pass in the task field, not the full conversation.
  The specialist's response may contain a "===TOOL DATA===" block after its
  spoken summary. That block is the authoritative source for IDs, field
  values, and exact names — prefer it over the summary when quoting or
  reusing specifics (e.g. task IDs for follow-up actions). Do not read the
  TOOL DATA block aloud; use the spoken summary for your reply.
- PDF processing via process_pdf for converting PDFs to markdown. This runs in the
  background and saves the result to Dave's stash — use this instead of
  calling convert_pdf_to_md directly so Dave can keep talking while it processes.
- File download for fetching files from URLs to local storage
- Document reader via read_document for reading papers and documents aloud.
  When Dave says "read this document", "read this paper", or provides a file
  path to read aloud, use read_document. Math expressions are automatically
  converted to natural speech. The document will be available at /reader.
- list_reader_documents to check what's in the reader and whether in-flight
  PDF conversions have finished. Use when Dave asks "what's in the reader",
  "is that PDF ready yet", or "did the conversion finish".

Important guidelines for your responses:
- Keep responses concise and conversational — they will be spoken aloud via TTS.
- Do NOT use markdown formatting, bullet points, numbered lists, code blocks,
  or any visual formatting. Your output is audio, not text.
- When you use a tool, briefly mention what you're doing so Dave isn't waiting
  in silence (e.g., "Let me look that up." or "Checking your email now.").
- If a search returns results, summarize the key findings conversationally.
  Don't read out URLs.
- Stash via save_to_stash for saving content Dave wants to review later. The
  stash is Dave's personal capture area — it is NOT his email inbox. When Dave
  says "save this", "remember that", "draft a reply", "add this to my stash",
  or similar, use save_to_stash. For search results, save your summary (not raw
  results). For notes, save his words verbatim. For email drafts, set item_type
  to "email_draft" and include recipient and subject in metadata. Always give a
  clear, descriptive title.
- list_stash_items to browse the stash (defaults to pending items). Use when
  Dave asks "what's in my stash", "what did I save", or "what's still pending
  to review". Do NOT use this for email — email lives in Evangeline.
- Dave is a statistics instructor and researcher at Trent University. He runs
  a homelab with multiple machines. He prefers concise, technically precise
  responses and will correct you if you're wrong. Don't over-explain."""


DEFAULT_SYSTEM_PROMPT = _RAW_SYSTEM_PROMPT.format(
    vikunja_projects=format_vikunja_projects(),
    vikunja_default=format_vikunja_default(),
)


def load_settings() -> Settings:
    llm_chain = _env_json(
        "OCTAVIUS_LLM_CHAIN",
        [
            {"url": "http://lilripper:8020/v1/chat/completions", "model": "qwen3.5-35b-a3b"},
            {"url": "http://127.0.0.1:8001/v1/chat/completions", "model": "qwen3.5-35b-a3b"},
            {"url": "http://triplestuffed:8010/v1/chat/completions", "model": "qwen3.5-35b-a3b"},
        ],
    )
    voxtral_voices = _env_json("OCTAVIUS_TTS_VOXTRAL_VOICES", DEFAULT_VOXTRAL_VOICES)
    kokoro_voices = _env_json("OCTAVIUS_TTS_KOKORO_VOICES", DEFAULT_KOKORO_VOICES)
    tts = TTSSettings(
        url=_env_str("OCTAVIUS_TTS_URL", "http://triplestuffed:8020/v1/audio/speech"),
        model=_env_str("OCTAVIUS_TTS_MODEL", "/media/extra_stuff/huggingface/mistralai/Voxtral-4B-TTS-2603"),
        voice=_env_str("OCTAVIUS_TTS_VOICE", "bm_lewis"),
        format=_env_str("OCTAVIUS_TTS_FORMAT", "wav"),
        voices=voxtral_voices + kokoro_voices,
        voxtral_voices=voxtral_voices,
        kokoro_voices=kokoro_voices,
        fallback_url=_env_str("OCTAVIUS_TTS_FALLBACK_URL", "http://lilbuddy:8880/v1/audio/speech"),
        fallback_model=_env_str("OCTAVIUS_TTS_FALLBACK_MODEL", "kokoro"),
        fallback_voice=_env_str("OCTAVIUS_TTS_FALLBACK_VOICE", "bm_lewis"),
    )
    reader = ReaderSettings(
        directory=_env_str("OCTAVIUS_READER_DIR", "/home/dave/octavius-reader"),
        llm_url=_env_str("OCTAVIUS_READER_LLM_URL", "http://lilripper:8010/v1/chat/completions"),
        llm_model=_env_str("OCTAVIUS_READER_LLM_MODEL", "qwen3.5-9b"),
    )
    return Settings(
        stt_url=_env_str("OCTAVIUS_STT_URL", "http://lilripper:8552/api/transcribe"),
        llm_chain=llm_chain,
        tts=tts,
        reader=reader,
        agent_port=_env_int("OCTAVIUS_AGENT_PORT", 8030),
        downloads_dir=_env_str("OCTAVIUS_DOWNLOADS_DIR", "/home/dave/octavius-downloads"),
        max_tool_rounds=_env_int("OCTAVIUS_MAX_TOOL_ROUNDS", 7),
        max_conversation_messages=_env_int("OCTAVIUS_MAX_CONVERSATION_MESSAGES", 40),
        tool_labels=_env_json("OCTAVIUS_TOOL_LABELS", DEFAULT_TOOL_LABELS),
        mcp_servers=_env_json("OCTAVIUS_MCP_SERVERS", DEFAULT_MCP_SERVERS),
        system_prompt=_env_str("OCTAVIUS_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
        summary_url=_env_str("OCTAVIUS_SUMMARY_URL", "http://127.0.0.1:8001/v1/chat/completions"),
        summary_fallback_url=_env_str("OCTAVIUS_SUMMARY_FALLBACK_URL", "http://triplestuffed:8010/v1/chat/completions"),
        summary_model=_env_str("OCTAVIUS_SUMMARY_MODEL", "qwen3.5-35b-a3b"),
        summary_timeout=_env_int("OCTAVIUS_SUMMARY_TIMEOUT", 60),
        embedding_chain=_env_json(
            "OCTAVIUS_EMBEDDING_CHAIN",
            [
                {
                    "url": "http://lilbuddy:8010/v1/embeddings",
                    "model": "bge-m3",
                    "schema": "openai",
                },
                {
                    "url": "http://workhorse:11434/api/embeddings",
                    "model": "bge-m3",
                    "schema": "ollama",
                },
            ],
        ),
        embedding_timeout=_env_int("OCTAVIUS_EMBEDDING_TIMEOUT", 5),
        result_summary_max_chars=_env_int("OCTAVIUS_RESULT_SUMMARY_MAX_CHARS", 500),
        tag_generation_min_messages=_env_int("OCTAVIUS_TAG_GENERATION_MIN_MESSAGES", 4),
    )


settings = load_settings()
