import json
import os
from dataclasses import dataclass
from pathlib import Path
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


def _env_str_list(name: str, default: list[str]) -> list[str]:
    """Colon-separated list env var (PATH-style), e.g. `OCTAVIUS_MEDIA_SPOOL_DIRS`."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return [p for p in raw.split(":") if p]


# Optional single-file model routing. Everything else in this module reads
# os.environ directly and that stays true — this is the ONE file the app loads,
# and it holds no secrets, only which endpoint serves which alias with which
# generation params. It exists because routing lived in six separate JSON-inside-
# a-systemd-EnvironmentFile variables, which is unpleasant to hand-edit and was
# how a stale alias survived unnoticed on :8020 until 2026-08-26.
#
# Precedence, lowest to highest: code defaults < models.json < environment.
# Env still wins so tests and one-off overrides keep working unchanged, and so
# nothing in this file can override a secret.
MODELS_FILE = Path(
    os.getenv("OCTAVIUS_MODELS_FILE", "~/.config/octavius/models.json")
).expanduser()


def _load_models_file() -> dict:
    """Parse MODELS_FILE, or return {} if absent.

    A malformed file raises: silently falling back to defaults would restart the
    service onto routing the operator did not ask for, which is exactly the
    class of bug this file was added to prevent.
    """
    if not MODELS_FILE.is_file():
        return {}
    try:
        data = json.loads(MODELS_FILE.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"{MODELS_FILE} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{MODELS_FILE} must contain a JSON object")
    roles = data.get("roles", {})
    if not isinstance(roles, dict):
        raise ValueError(f"{MODELS_FILE}: 'roles' must be a JSON object")
    return roles


_MODEL_ROLES = _load_models_file()


def _role_chain(role: str, env_var: str, default):
    """Chain for `role`: env var, else models.json, else the code default."""
    raw = os.getenv(env_var)
    if raw:
        return json.loads(raw)
    entries = _MODEL_ROLES.get(role)
    if entries is None:
        return default
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{MODELS_FILE}: roles.{role} must be a non-empty list")
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("url"):
            raise ValueError(f"{MODELS_FILE}: every roles.{role} entry needs a 'url'")
    return entries


def _role_single(role: str, key: str, env_var: str, default, index: int = 0):
    """One field of a single-endpoint role (reader, summary), same precedence.

    index=1 addresses the optional second entry of a role — the reader's
    failover target. A missing entry yields `default`.
    """
    raw = os.getenv(env_var)
    if raw:
        return raw
    entries = _MODEL_ROLES.get(role)
    if isinstance(entries, list) and len(entries) > index and isinstance(entries[index], dict):
        value = entries[index].get(key)
        if value:
            return value
    return default


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
    # straight to the fallback (Phonon), with Voxtral-only voices remapped to the
    # fallback voice. Voxtral's voice quality is fine, but its inconsistent sound
    # levels make it unsuitable as the live primary. Set OCTAVIUS_TTS_VOXTRAL_ENABLED=1
    # to restore the primary→fallback path with circuit breaker.
    voxtral_enabled: bool = False


@dataclass(frozen=True)
class PiBrainSettings:
    """pi as the turn brain (docs/pi-backend-design.md, rollout step 2).

    ``brain`` is "native" (agent.py's loop, the default) or "pi" (a long-lived
    ``pi --mode rpc`` process per conversation, see pi_brain.py).
    """
    brain: str
    launcher: str
    pi_bin: str
    model: str
    idle_reap_s: float
    turn_timeout_s: float
    confirm_timeout_s: float

    @property
    def enabled(self) -> bool:
        return self.brain == "pi"


@dataclass(frozen=True)
class ReaderSettings:
    directory: str
    llm_url: str
    llm_model: str
    llm_fallback_url: str | None = None
    llm_fallback_model: str | None = None


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
    # Where POST /api/media/upload lands client-uploaded files (Android app
    # today) before the client sends the existing image_input/file_input WS
    # frame. See docs/ws-media-contract.md's "Client uploads" section.
    media_upload_dir: str
    # Allowlist of directories a WS media frame's `path` may resolve under.
    # media_uploads.resolve_spooled_media checks this before the image_input/
    # file_input handlers touch a file named in a frame. Always includes
    # media_upload_dir (built into the default even when
    # OCTAVIUS_MEDIA_UPLOAD_DIR overrides it) so a relocated upload dir can't
    # silently fall outside its own allowlist.
    media_spool_dirs: list[str]
    max_tool_rounds: int
    max_conversation_messages: int
    tool_labels: dict[str, str]
    mcp_servers: dict[str, dict]
    system_prompt: str
    summary_url: str
    summary_fallback_url: str
    summary_model: str
    summary_fallback_model: str
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
    pi_brain: PiBrainSettings


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

# Despite the name (kept to match OCTAVIUS_TTS_KOKORO_VOICES), these are the
# voices passed through unchanged to the fallback endpoint; any other voice is
# remapped to fallback_voice. Defaults are Phonon 31m's voices (the live TTS
# since 2026-10-28). Phonon 400s on unknown names, so list only what
# GET /v1/audio/voices returns.
DEFAULT_KOKORO_VOICES = [
    "Imogen", "Delia", "Hugh", "Edwin", "Spencer", "Teddy", "Sir Bramwell",
]


DEFAULT_TOOL_LABELS = {
    "web_search": "Web Search",
    # Email tools carry the `email_` tool_prefix (see DEFAULT_MCP_SERVERS);
    # upstream names were shortened 2026-09-23 (mcp-tools RENAMES-2026-09.md).
    "email_keyword_search": "Email Search",
    "email_semantic_search": "Email Search",
    "email_hybrid_search": "Email Search",
    "email_get": "Reading Email",
    "email_get_many": "Reading Emails",
    "email_get_conversation": "Reading Email Thread",
    "email_list_conversations": "Listing Email Threads",
    "email_stats": "Email Stats",
    "email_find_people": "Finding People",
    "email_find_similar_responses": "Finding Similar Emails",
    "email_extract": "Extracting from Emails",
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
    "vault_search": "Searching Vault",
    "search_papers": "Searching Papers",
    "get_paper": "Reading Paper",
    "read_document": "Preparing Document",
    "list_reader_documents": "Listing Reader Docs",
    "append_to_reader_document": "Extending Document",
    "process_pdf": "Processing PDF",
    "consult_specialist": "Consulting Specialist",
    "read_url": "Reading Web Page",
}


DEFAULT_MCP_SERVERS = {
    "evangeline-email": {
        "transport": "http",
        "url": "http://triplestuffed:8251/mcp",
        # Upstream tool names were shortened on 2026-09-23 (keyword_search,
        # get, get_many, stats, extract, ... — mcp-tools RENAMES-2026-09.md)
        # because clients prefix them; this prefix makes the model see
        # email_keyword_search, email_get, email_stats, ... exactly as pi does.
        "tool_prefix": "email",
    },
    "calendar": {
        "transport": "http",
        # mcp-tools' server_calendar.py (calendar-mcp.service), split out of the
        # email server on 2026-09-23. Loopback like web-search; Octavius must not
        # start before this unit is up (an unreachable server aborts connect_all).
        "url": "http://127.0.0.1:8208/mcp",
        "tool_prefix": "calendar",
    },
    "web-search": {
        "transport": "http",
        # mcp-tools' server_serper.py, served as a shared streamable-HTTP
        # service on triplestuffed since 2026-08-26 (web-search.service,
        # :8205 behind Caddy :8255) so every consumer shares one process, one
        # set of API keys and one 15-minute result cache. Parallel.ai is the
        # PRIMARY arm (neural index, excerpts, 5k free/month); Google via
        # Serper answers on a Parallel error or zero hits and owns the
        # kind="news"/"scholar" verticals. SearXNG only when named. Keys live
        # in mcp-tools/.env on triplestuffed — nothing is needed here.
        # Octavius runs ON triplestuffed, so talk to the unit directly over
        # loopback; the Caddy :8255 route exists for other hosts (pi on
        # tinkertoy etc.). An unreachable MCP server aborts connect_all() and
        # the whole lifespan — Octavius crash-looped on 2026-08-26 for the few
        # minutes this pointed at a Caddy port that wasn't configured yet.
        "url": "http://127.0.0.1:8205/mcp",
        "tool_description_suffix": (
            " | SCOPE: general web lookups only (news, recipes, product info, "
            "how-to, definitions, current events). Do NOT use for academic "
            "papers, journal articles, citations, authors, or scholarly "
            "research — those ALWAYS go to consult_specialist(domain=\"research\")."
        ),
    },
    "web-reader": {
        "transport": "http",
        # Web reader (mcp-tools' server_reader.py) — the "read" half of the
        # search -> read -> reason pipeline. Since 2026-08-26 a fetch LADDER on
        # triplestuffed (web-reader.service, :8207 behind Caddy :8257): site
        # recipes (Reddit threads/listings, X, arXiv, Substack) -> plain fetch
        # + trafilatura -> Crawl4AI on lilripper in stealth mode -> archive.
        # Same deployed instance the pi agents use; loopback for the same
        # reason as web-search above.
        "url": "http://127.0.0.1:8207/mcp",
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
        # vault on triplestuffed. Exposes a single upstream tool `search`
        # (was `search_vault` until 2026-09-23), shown to the model as
        # `vault_search` via tool_prefix; routes/vault.py calls it by
        # (server key, "search"). The 03-personal/Journaling/ subtree is
        # excluded server-side.
        "url": "http://triplestuffed:8254/mcp",
        "tool_prefix": "vault",
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
- Web search (Google) for GENERAL web lookups ONLY — news, recipes,
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
  It also takes raw text instead of a path: pass text="..." when Dave pastes
  or dictates something to read, or wants something from this conversation
  sent to the reader. Pass the full text verbatim, never a summary. A title
  is derived from the first line if you don't give one.
- list_reader_documents to check what's in the reader and whether in-flight
  PDF conversions have finished. Use when Dave asks "what's in the reader",
  "is that PDF ready yet", or "did the conversion finish".
- append_to_reader_document to add content to a document already in the reader
  when the original pull was partial (sign-in pop-up, paywall teaser, truncated
  page) and Dave supplies the rest, pasted or as a saved file. Pass it verbatim.
  Use replace=true if what the reader has is junk and should be rebuilt from
  the new content instead.

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
- vault_search to find Dave's existing notes. Use when he asks "what did I note
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


# Origins whose bearer token may be supplied by the dedicated
# OCTAVIUS_LR_API_KEY env var ("LR" = lilripper). The scope is the *host*, not
# the port: `lilbuddy:8010` (main-chain fallback) and `triplestuffed:8010` also
# listen on 8010 and are OPEN — they must never receive this header.
#
# Both lilripper router ports went behind the same token: `:8020` began
# 401ing bare requests some time before 2026-08-26, and because the key was
# pinned to `:8010` alone, Octavius sent `:8020` no header at all and read the
# result as an ordinary failover. One token authenticates both ports (verified
# 2026-08-26), so widening the scope to lilripper keeps a single rotating value
# covering every authed hop.
KEYED_LR_ORIGINS = (
    "http://lilripper:8010",
    "http://lilripper:8020",
)


def _llm_api_keys() -> dict[str, str]:
    """Resolve bearer tokens for LLM endpoints, keyed by origin.

    Two sources, in increasing precedence:

    1. ``OCTAVIUS_LLM_API_KEYS`` — a JSON object of endpoint URL -> token,
       normalized to origins. Still the general mechanism; use it if another
       endpoint ever goes behind auth.
    2. ``OCTAVIUS_LR_API_KEY`` — a bare token applied to every origin in
       ``KEYED_LR_ORIGINS``.
       This is the one that rotates, so it wins on conflict. A dedicated var
       exists because rotating a value nested inside single-quoted JSON in a
       systemd EnvironmentFile is easy to get subtly wrong (and a mangled key
       fails as a 401, which the chain client reports as an ordinary failover,
       not as an auth error).
    """
    raw = _env_json("OCTAVIUS_LLM_API_KEYS", {})
    if not isinstance(raw, dict):
        raise ValueError("OCTAVIUS_LLM_API_KEYS must be a JSON object of origin -> key")
    keys = {endpoint_origin(url): key for url, key in raw.items() if key}
    direct = _env_str("OCTAVIUS_LR_API_KEY", "").strip()
    if direct:
        for origin in KEYED_LR_ORIGINS:
            keys[origin] = direct
    return keys


def load_settings() -> Settings:
    # Code defaults mirror the live ~/.config/octavius/models.json as of
    # 2026-10-28 (models.json and env still override these). The model id is
    # load-bearing on every endpoint: an alias absent from that endpoint's
    # catalog hard-400s and silently burns a failover hop, so re-curl /v1/models
    # (scripts/octavius-models check) after any router rebuild.
    #
    # Main chain:
    #  1. lilbuddy:8010 gemma4-26b-a4b — primary (open; --parallel 2, shared with
    #     the vision primary). A thinking model that returns reasoning in a
    #     separate `reasoning_content` field, which agent.py ignores; anything
    #     that sends max_tokens to it must budget for reasoning tokens. Size any
    #     new lilbuddy alias against the whole box's residency budget: an OOM
    #     there once took every port on the box down.
    #  2. lilripper:8020 qwen3.8-27b — vLLM, behind auth, effectively always
    #     warm. Fallback only: Dave loads other models there by hand, and as a
    #     primary every turn would evict them. lilripper:8010 now serves only
    #     glm-5.3-flash and is not used by Octavius.
    #  3. triplestuffed:8010 gemma4-12b — third host, so voice survives lilbuddy
    #     and lilripper both being down. Text-only (image turns use the vision
    #     chain), --parallel 4.
    llm_chain = _role_chain(
        "main",
        "OCTAVIUS_LLM_CHAIN",
        [
            {"url": "http://lilbuddy:8010/v1/chat/completions", "model": "gemma4-26b-a4b"},
            {"url": "http://lilripper:8020/v1/chat/completions", "model": "qwen3.8-27b"},
            {"url": "http://triplestuffed:8010/v1/chat/completions", "model": "gemma4-12b"},
        ],
    )
    # Subagents (inline consult_specialist) run on triplestuffed:8010 gemma4-12b.
    # `capacity` 3 of its --parallel 4 leaves one slot for main hop 3, the reader
    # and summaries, which share the model. :8020 is the HTTP-level fallback
    # (capacity is a guess: vLLM's limit is not readable from /v1/models); the
    # dispatcher keeps only the first role=fallback entry. The model is resolved
    # per ENDPOINT URL (subagent.py::_model_for_url), not per domain.
    subagent_llm_chain = _role_chain(
        "subagent",
        "OCTAVIUS_SUBAGENT_LLM_CHAIN",
        [
            {"url": "http://triplestuffed:8010/v1/chat/completions", "model": "gemma4-12b", "role": "primary", "capacity": 3},
            {"url": "http://lilripper:8020/v1/chat/completions", "model": "qwen3.8-27b", "role": "fallback", "capacity": 2},
        ],
    )
    # Vision-capable chain for turns carrying image content (image_input WS
    # frames). Kept separate from llm_chain because that chain's third hop is
    # chosen for availability, not modality: triplestuffed's gemma4-12b is
    # text-only and must never be added here. Check architecture.input_modalities
    # AND --ubatch-size (>= ~1152 for a gemma4v image) before adding a hop.
    vision_llm_chain = _role_chain(
        "vision",
        "OCTAVIUS_VISION_LLM_CHAIN",
        [
            {"url": "http://lilbuddy:8010/v1/chat/completions", "model": "gemma4-26b-a4b"},
            {"url": "http://lilripper:8020/v1/chat/completions", "model": "qwen3.8-27b"},
        ],
    )
    voxtral_voices = _env_json("OCTAVIUS_TTS_VOXTRAL_VOICES", DEFAULT_VOXTRAL_VOICES)
    kokoro_voices = _env_json("OCTAVIUS_TTS_KOKORO_VOICES", DEFAULT_KOKORO_VOICES)
    voxtral_enabled = _env_str("OCTAVIUS_TTS_VOXTRAL_ENABLED", "").lower() in {"1", "true", "yes", "on"}
    tts = TTSSettings(
        url=_env_str("OCTAVIUS_TTS_URL", "http://lilripper:8030/v1/audio/speech"),
        model=_env_str("OCTAVIUS_TTS_MODEL", "voxtral-4b-tts"),
        voice=_env_str("OCTAVIUS_TTS_VOICE", "Imogen"),
        format=_env_str("OCTAVIUS_TTS_FORMAT", "wav"),
        voices=voxtral_voices + kokoro_voices,
        voxtral_voices=voxtral_voices,
        kokoro_voices=kokoro_voices,
        fallback_url=_env_str("OCTAVIUS_TTS_FALLBACK_URL", "http://lilripper:8881/v1/audio/speech"),
        fallback_model=_env_str("OCTAVIUS_TTS_FALLBACK_MODEL", "phonon-31m-en"),
        fallback_voice=_env_str("OCTAVIUS_TTS_FALLBACK_VOICE", "Imogen"),
        voxtral_enabled=voxtral_enabled,
    )
    # The code-default failover applies only when models.json says nothing about
    # the reader. A file that lists a single reader entry means "no fallback".
    _reader_in_file = "reader" in _MODEL_ROLES
    _reader_fb_url = None if _reader_in_file else "http://lilripper:8020/v1/chat/completions"
    _reader_fb_model = None if _reader_in_file else "qwen3.8-27b"
    reader = ReaderSettings(
        directory=_env_str("OCTAVIUS_READER_DIR", "/home/dave/octavius-reader"),
        llm_url=_role_single("reader", "url", "OCTAVIUS_READER_LLM_URL", "http://triplestuffed:8010/v1/chat/completions"),
        # gemma4-12b on triplestuffed, the same alias as that host's other
        # consumers (subagent, summary, main hop 3) so the router does not thrash
        # between resident models. Verify the alias is live after router rebuilds.
        llm_model=_role_single("reader", "model", "OCTAVIUS_READER_LLM_MODEL", "gemma4-12b"),
        # The reader is single-endpoint per call, but the role's second entry is
        # its failover target (reader_text.py tries it before giving up to
        # strip_latex). Generation params are NOT read from here: LLMChainClient.
        # complete() merges per-url params from the MAIN chain, and this url is
        # also a main chain hop (:8020 is hop 2) — so a fallback attempt inherits whatever params
        # that entry carries (none today).
        llm_fallback_url=_role_single("reader", "url", "OCTAVIUS_READER_LLM_FALLBACK_URL", _reader_fb_url, index=1),
        llm_fallback_model=_role_single("reader", "model", "OCTAVIUS_READER_LLM_FALLBACK_MODEL", _reader_fb_model, index=1),
    )
    if (reader.llm_fallback_url is None) != (reader.llm_fallback_model is None):
        raise ValueError(
            f"{MODELS_FILE}: roles.reader's fallback entry needs both 'url' and 'model' (or neither)"
        )
    media_upload_dir = _env_str(
        "OCTAVIUS_MEDIA_UPLOAD_DIR", "/media/extra_stuff/octavius/client_media/"
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
        media_upload_dir=media_upload_dir,
        media_spool_dirs=_env_str_list(
            "OCTAVIUS_MEDIA_SPOOL_DIRS",
            ["/media/extra_stuff/octavius/matrix_media", media_upload_dir],
        ),
        max_tool_rounds=_env_int("OCTAVIUS_MAX_TOOL_ROUNDS", 7),
        max_conversation_messages=_env_int("OCTAVIUS_MAX_CONVERSATION_MESSAGES", 40),
        tool_labels=_env_json("OCTAVIUS_TOOL_LABELS", DEFAULT_TOOL_LABELS),
        mcp_servers=_env_json("OCTAVIUS_MCP_SERVERS", DEFAULT_MCP_SERVERS),
        system_prompt=_env_str("OCTAVIUS_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
        # triplestuffed gemma4-12b primary, lilripper:8020 qwen3.8-27b fallback.
        # Summaries fire at conversation END, long after Dave has moved on, so
        # :8020 stays the fallback: a background job must not evict a model he
        # loaded there by hand. SummaryClient carries a model PER URL and sends
        # auth_headers, so the authed fallback is fine.
        summary_url=_role_single("summary", "url", "OCTAVIUS_SUMMARY_URL", "http://triplestuffed:8010/v1/chat/completions"),
        summary_fallback_url=_env_str("OCTAVIUS_SUMMARY_FALLBACK_URL", "http://lilripper:8020/v1/chat/completions"),
        summary_model=_role_single("summary", "model", "OCTAVIUS_SUMMARY_MODEL", "gemma4-12b"),
        # The two endpoints do not share an alias; the fallback needs its own
        # model or it 400s.
        summary_fallback_model=_env_str("OCTAVIUS_SUMMARY_FALLBACK_MODEL", "qwen3.8-27b"),
        summary_timeout=_env_int("OCTAVIUS_SUMMARY_TIMEOUT", 60),
        # lilbuddy is primary as of 2026-08-12. It was demoted on 2026-08-10 for
        # being unreachable (a tailscale fault, since fixed) — and because a dead
        # host drops packets rather than refusing them, every embed burned the
        # full connect timeout on it before failing over. That is no longer a
        # reason to keep it second: the connect-timeout fix plus the per-endpoint
        # breaker in EmbeddingClient cap a dead primary at two failures and then
        # 300 s of nothing. So order is now purely about speed, and measured on a
        # realistic ~1800-char payload lilbuddy wins ~3x (0.11 s vs 0.30 s, plus
        # workhorse pays a ~2.7 s cold start after an idle period).
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
        pi_brain=PiBrainSettings(
            brain=_env_str("OCTAVIUS_BRAIN", "native"),
            launcher=_env_str(
                "OCTAVIUS_PI_LAUNCHER",
                os.path.expanduser("~/git_repos/pi_harness/scripts/pi-octavius"),
            ),
            # The user service's PATH has no ~/.local/bin, so name pi explicitly.
            pi_bin=_env_str("OCTAVIUS_PI_BIN", os.path.expanduser("~/.local/bin/pi")),
            # Passed to the launcher as PI_OCTAVIUS_MODEL; also what history records.
            model=_env_str("OCTAVIUS_PI_MODEL", "lb-unified/gemma4-26b-a4b"),
            # Hours, not minutes: a reaped process loses its warm session and the
            # next turn reseeds from history (see "What /warm can and cannot warm").
            idle_reap_s=_env_float("OCTAVIUS_PI_IDLE_REAP_S", 4 * 3600),
            # scholar alone took 228 s in the step-1 run.
            turn_timeout_s=_env_float("OCTAVIUS_PI_TURN_TIMEOUT_S", 600),
            # Below octavius-guard's 60 s, so Octavius answers before pi gives up.
            confirm_timeout_s=_env_float("OCTAVIUS_PI_CONFIRM_TIMEOUT_S", 55),
        ),
    )


settings = load_settings()
