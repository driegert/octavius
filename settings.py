import json
import os
from dataclasses import dataclass
from urllib.parse import urlsplit


def endpoint_origin(url: str) -> str:
    """scheme://host:port for an endpoint URL, used to key API secrets.

    Keys are held per origin rather than per chain entry because the same
    endpoint is reached from several chains (the reader, for instance, calls
    lilripper:8010 through a client whose own chain doesn't list it).
    """
    parts = urlsplit(url if "://" in url else f"http://{url}")
    return f"{parts.scheme}://{parts.netloc}"


def _env_str(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw is not None else default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw is not None else default


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
    # When False (default), Voxtral is never attempted: all synth calls go
    # straight to Kokoro, with Voxtral-only voices remapped to the fallback
    # voice. Voxtral's voice quality is fine, but its inconsistent sound levels
    # make it unsuitable as the live primary. Set OCTAVIUS_TTS_VOXTRAL_ENABLED=1
    # to restore the primary→fallback path with circuit breaker.
    voxtral_enabled: bool = False


@dataclass(frozen=True)
class ReaderSettings:
    directory: str
    llm_url: str
    llm_model: str


@dataclass(frozen=True)
class Settings:
    stt_url: str
    llm_chain: list[dict]
    subagent_llm_chain: list[dict]
    vision_llm_chain: list[dict]
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
    memory_service_url: str
    memory_read_timeout: int
    memory_write_timeout: int
    # PDF -> markdown conversion, driven through the already-registered
    # "document-processing" MCP server (mcp-tools' documents wrapper: scp to
    # lilripper, convert remotely, download the .md back to local paths).
    # See docproc_client.py. Poll knobs pace docproc_client.poll_job.
    docproc_poll_interval: float
    docproc_poll_timeout: float
    # Char budget for inlining converted markdown directly into an agent turn
    # vs. handing the model a path + head excerpt instead.
    docproc_inline_char_budget: int
    docproc_excerpt_chars: int
    # Bearer tokens for LLM endpoints that sit behind auth, keyed by origin
    # (scheme://host:port). Endpoints absent from the map are called without an
    # Authorization header, as before. Set via OCTAVIUS_LLM_API_KEYS.
    llm_api_keys: dict[str, str]


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
    "web_search": "Web Search",
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
    "save_note": "Saving Note",
    "read_note": "Reading Note",
    "edit_note": "Editing Note",
    "commit_edit": "Saving Edit",
    "search_vault": "Searching Vault",
    "search_papers": "Searching Papers",
    "get_paper": "Reading Paper",
    "read_document": "Preparing Document",
    "list_reader_documents": "Listing Reader Docs",
    "process_pdf": "Processing PDF",
    "consult_specialist": "Consulting Specialist",
    "hybrid_search": "Email Search",
    "read_url": "Reading Web Page",
}


DEFAULT_MCP_SERVERS = {
    "evangeline-email": {
        "transport": "http",
        "url": "http://triplestuffed:8251/mcp",
    },
    "web-search": {
        "transport": "stdio",
        # mcp-tools' server_serper.py exposes a single `web_search` tool that
        # tries self-hosted SearXNG first (free/private) and falls back to the
        # Serper.dev Google API when SearXNG is unreachable, rate-limited, or
        # returns nothing. It reads SERPER_API_KEY from mcp-tools/.env
        # (load_dotenv) and trusts the system CA bundle for SearXNG's Caddy
        # cert on its own (honors SSL_CERT_FILE, else /etc/ssl/certs/
        # ca-certificates.crt), so no env is needed here. SEARX_HOST defaults
        # to https://searxng.riegert.xyz. Reading pages stays on web-reader
        # (Crawl4AI). Replaced the old varlabz searxng-mcp (`search` tool,
        # SearXNG-only, no fallback).
        "command": "/home/dave/git_repos/mcp-tools/.venv/bin/python",
        "args": [
            "/home/dave/git_repos/mcp-tools/server_serper.py",
        ],
        "tool_description_suffix": (
            " | SCOPE: general web lookups only (news, recipes, product info, "
            "how-to, definitions, current events). Do NOT use for academic "
            "papers, journal articles, citations, authors, or scholarly "
            "research — those ALWAYS go to consult_specialist(domain=\"research\")."
        ),
    },
    "web-reader": {
        "transport": "http",
        # Crawl4AI markdown reader (mcp-tools' server_reader.py) — the "read"
        # half of the search -> read -> reason pipeline. Same deployed instance
        # the pi agents use (Caddy on lilripper -> localhost Crawl4AI).
        "url": "http://lilripper:8254/mcp",
        "tool_description_suffix": (
            " | Use AFTER a web search to read the full content of a specific "
            "result URL, or whenever a search snippet isn't enough. Read one "
            "page at a time; don't bulk-read results."
        ),
    },
    "vault-search": {
        "transport": "http",
        # Vault search (mcp-tools' server_vault.py) — sqlite-vec + FTS5 BM25
        # over the Obsidian vault, RRF-fused. Local client, co-located with the
        # vault on triplestuffed. Exposes a single `search_vault` tool; the
        # 03-personal/Journaling/ subtree is excluded server-side.
        "url": "http://triplestuffed:8254/mcp",
        "tool_description_suffix": (
            " | Search Dave's Obsidian vault (his personal notes and captured "
            "thoughts). Use for 'what did I note about X', 'find my note on Y', "
            "or recalling past ideas. Returns note paths — read one with "
            "read_note. NOT for web or academic search."
        ),
    },
    "paper-search": {
        "transport": "http",
        # Paper library search (mcp-tools' server_papers.py) — sqlite-vec +
        # FTS5 BM25 over Dave's converted Paperpile PDFs, RRF-fused. Runs
        # locally on triplestuffed (papers-mcp.service); no Caddy hop needed.
        # Tools: search_papers, get_paper.
        "url": "http://127.0.0.1:8206/mcp",
        "tool_description_suffix": (
            " | Dave's academic paper library (Paperpile, ~1000 papers). Use "
            "for 'what papers discuss X' or reading a specific paper he owns. "
            "NOT for general web search; use openalex via the research "
            "specialist for papers he does NOT have."
        ),
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
- Web search via SearXNG for GENERAL web lookups ONLY — news, recipes,
  product info, how-to, definitions, current events. NEVER use web search
  for academic papers, journal articles, citations, authors, or scholarly
  research; those ALWAYS go through consult_specialist(domain="research").
- Web page reading via read_url — fetch a specific URL and get its clean
  content. Use this AFTER a web search to read a promising result, or when
  Dave gives you a URL, whenever a search snippet isn't enough to answer.
  Read one page at a time; don't bulk-read every result.
- consult_specialist for email, research, and task management. This hands off
  to a scoped specialist assistant and returns its answer to you in the SAME
  turn. Use it when Dave asks about:
  * Email: "check my email", "find emails from X", "any emails about Y" →
    consult_specialist(domain="email", task="..."). Include dates, senders, or
    topics Dave mentioned.
  * Research: "find papers about X", "who publishes on Y", "citations for Z",
    "journal articles on W", "recent publications about V" →
    consult_specialist(domain="research", task="..."). This is the ONLY correct
    tool for academic/scholarly queries — do not fall back to web search.
    Include topic details.
  * Tasks: "add a task", "what's on my list", "mark X as done" →
    consult_specialist(domain="tasks", task="..."). Include project names if
    Dave specified one. Key projects: {vikunja_projects}.
    Default to {vikunja_default} if Dave doesn't specify a project.
  consult_specialist is SYNCHRONOUS. It returns the specialist's findings as
  the tool result; wait for them and then weave them into a natural, spoken
  reply to Dave. Do NOT just acknowledge and stop — the answer comes back to
  you in this turn, so deliver it. It can take a few seconds; that's expected.
  Write a clear, complete task description — the specialist only sees what you
  pass in the task field, not the full conversation.
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
- Response length and formatting depend on the channel Dave is using; a per-turn
  note tells you whether this is a spoken voice turn or a typed text turn. Follow it.
- When you use a tool, briefly mention what you're doing so Dave isn't waiting
  in silence (e.g., "Let me look that up." or "Checking your email now.").
- If a search returns results, summarize the key findings conversationally.
  Don't read out URLs.
- Vault notes via save_note for saving content Dave wants to keep or review
  later. The vault is Dave's personal note store (his Obsidian notes) — it is
  NOT his email inbox. When Dave says "save this", "make a note", "jot that
  down", or similar, use save_note. For search results, save your summary (not
  raw results). For notes, save his words verbatim. Always give a clear,
  descriptive title.
- search_vault to find Dave's existing notes. Use when he asks "what did I note
  about X", "find my note on Y", or "did I write anything about Z". It returns
  note paths — read one with read_note. Do NOT use this for email (that lives
  in Evangeline) or web/academic lookups.
- read_note / edit_note to read or revise a note. edit_note writes directly for
  inbox notes; for notes elsewhere it returns a preview to confirm with Dave,
  then commit_edit saves it. Never rename or move notes — Dave files them in
  Obsidian himself.
- Dave is a statistics instructor and researcher at Trent University. He runs
  a homelab with multiple machines. He prefers concise, technically precise
  responses and will correct you if you're wrong. Don't over-explain."""


DEFAULT_SYSTEM_PROMPT = _RAW_SYSTEM_PROMPT.format(
    vikunja_projects=format_vikunja_projects(),
    vikunja_default=format_vikunja_default(),
)


def _llm_api_keys() -> dict[str, str]:
    raw = _env_json("OCTAVIUS_LLM_API_KEYS", {})
    if not isinstance(raw, dict):
        raise ValueError("OCTAVIUS_LLM_API_KEYS must be a JSON object of origin -> key")
    return {endpoint_origin(url): key for url, key in raw.items() if key}


def load_settings() -> Settings:
    llm_chain = _env_json(
        "OCTAVIUS_LLM_CHAIN",
        [
            {"url": "http://lilripper:8020/v1/chat/completions", "model": "qwen3.6-35b-a3b"},
            {"url": "http://lilbuddy:8010/v1/chat/completions", "model": "qwen3.6-35b-a3b"},
            {"url": "http://triplestuffed:8010/v1/chat/completions", "model": "qwen3.6-35b-a3b"},
        ],
    )
    # Subagents (inline consult_specialist + backgrounded delegations) run on
    # lilripper:8010, which is served with --parallel 3, so they no longer queue
    # behind the main agent's own turn on the single-slot :8020. `capacity`
    # matches that --parallel, letting SubagentDispatcher run three consults at
    # once instead of serialising them. :8020 stays as the HTTP-level fallback.
    subagent_llm_chain = _env_json(
        "OCTAVIUS_SUBAGENT_LLM_CHAIN",
        [
            {"url": "http://lilripper:8010/v1/chat/completions", "model": "qwen3.6-35b-a3b-general", "role": "primary", "capacity": 3},
            {"url": "http://lilripper:8020/v1/chat/completions", "model": "qwen3.6-35b-a3b", "role": "fallback"},
        ],
    )
    # Vision-capable chain for turns carrying image content (image_input WS
    # frames from the Matrix sidecar). Separate from llm_chain because most
    # of the default chain's endpoints don't have image input enabled — only
    # lilripper:8010 (qwen3.6-35b-a3b-general) currently does.
    vision_llm_chain = _env_json(
        "OCTAVIUS_VISION_LLM_CHAIN",
        [
            {"url": "http://lilripper:8010/v1/chat/completions", "model": "qwen3.6-35b-a3b-general"},
        ],
    )
    voxtral_voices = _env_json("OCTAVIUS_TTS_VOXTRAL_VOICES", DEFAULT_VOXTRAL_VOICES)
    kokoro_voices = _env_json("OCTAVIUS_TTS_KOKORO_VOICES", DEFAULT_KOKORO_VOICES)
    voxtral_enabled = _env_str("OCTAVIUS_TTS_VOXTRAL_ENABLED", "").lower() in {"1", "true", "yes", "on"}
    tts = TTSSettings(
        url=_env_str("OCTAVIUS_TTS_URL", "http://lilripper:8030/v1/audio/speech"),
        model=_env_str("OCTAVIUS_TTS_MODEL", "voxtral-4b-tts"),
        voice=_env_str("OCTAVIUS_TTS_VOICE", "bm_lewis"),
        format=_env_str("OCTAVIUS_TTS_FORMAT", "wav"),
        voices=voxtral_voices + kokoro_voices,
        voxtral_voices=voxtral_voices,
        kokoro_voices=kokoro_voices,
        fallback_url=_env_str("OCTAVIUS_TTS_FALLBACK_URL", "http://lilbuddy:8880/v1/audio/speech"),
        fallback_model=_env_str("OCTAVIUS_TTS_FALLBACK_MODEL", "kokoro"),
        fallback_voice=_env_str("OCTAVIUS_TTS_FALLBACK_VOICE", "bm_lewis"),
        voxtral_enabled=voxtral_enabled,
    )
    reader = ReaderSettings(
        directory=_env_str("OCTAVIUS_READER_DIR", "/home/dave/octavius-reader"),
        llm_url=_env_str("OCTAVIUS_READER_LLM_URL", "http://lilripper:8010/v1/chat/completions"),
        # qwen3.5-9b went stale on the lilripper router (still listed in /v1/models,
        # but completions hang) — every reader math chunk silently fell back to
        # dollar-stripping. Keep this pointed at a model verified LIVE on :8010.
        llm_model=_env_str("OCTAVIUS_READER_LLM_MODEL", "qwen3.6-35b-a3b-general"),
    )
    return Settings(
        stt_url=_env_str("OCTAVIUS_STT_URL", "http://lilripper:8552/api/transcribe"),
        llm_chain=llm_chain,
        subagent_llm_chain=subagent_llm_chain,
        vision_llm_chain=vision_llm_chain,
        tts=tts,
        reader=reader,
        agent_port=_env_int("OCTAVIUS_AGENT_PORT", 8030),
        downloads_dir=_env_str("OCTAVIUS_DOWNLOADS_DIR", "/home/dave/octavius-downloads"),
        max_tool_rounds=_env_int("OCTAVIUS_MAX_TOOL_ROUNDS", 7),
        max_conversation_messages=_env_int("OCTAVIUS_MAX_CONVERSATION_MESSAGES", 40),
        tool_labels=_env_json("OCTAVIUS_TOOL_LABELS", DEFAULT_TOOL_LABELS),
        mcp_servers=_env_json("OCTAVIUS_MCP_SERVERS", DEFAULT_MCP_SERVERS),
        system_prompt=_env_str("OCTAVIUS_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
        summary_url=_env_str("OCTAVIUS_SUMMARY_URL", "http://lilbuddy:8010/v1/chat/completions"),
        summary_fallback_url=_env_str("OCTAVIUS_SUMMARY_FALLBACK_URL", "http://triplestuffed:8010/v1/chat/completions"),
        summary_model=_env_str("OCTAVIUS_SUMMARY_MODEL", "qwen3.6-35b-a3b"),
        summary_timeout=_env_int("OCTAVIUS_SUMMARY_TIMEOUT", 60),
        embedding_chain=_env_json(
            "OCTAVIUS_EMBEDDING_CHAIN",
            [
                {
                    "url": "http://lilbuddy:8020/v1/embeddings",
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
        # Shared memory service (v2): Octavius is a loopback HTTP client of the
        # memory brain. Empty url => memory disabled (degrades to memory-less).
        memory_service_url=_env_str("OCTAVIUS_MEMORY_URL", "http://127.0.0.1:8031"),
        memory_read_timeout=_env_int("OCTAVIUS_MEMORY_READ_TIMEOUT", 10),
        # Writes run the extractor LLM + synthesis on the service synchronously.
        memory_write_timeout=_env_int("OCTAVIUS_MEMORY_WRITE_TIMEOUT", 120),
        docproc_poll_interval=_env_float("OCTAVIUS_DOCPROC_POLL_INTERVAL", 3.0),
        docproc_poll_timeout=_env_float("OCTAVIUS_DOCPROC_POLL_TIMEOUT", 300.0),
        docproc_inline_char_budget=_env_int("OCTAVIUS_DOCPROC_INLINE_CHAR_BUDGET", 20000),
        docproc_excerpt_chars=_env_int("OCTAVIUS_DOCPROC_EXCERPT_CHARS", 3000),
        llm_api_keys=_llm_api_keys(),
    )


settings = load_settings()
