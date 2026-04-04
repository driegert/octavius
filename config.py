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
READER_DIR = "/home/dave/octavius-reader"
MAX_TOOL_ROUNDS = 7
MAX_CONVERSATION_MESSAGES = 40

# Friendly labels for tool names shown in the UI
TOOL_LABELS = {
    # SearXNG
    "search": "Web Search",
    # Email
    "search_emails": "Email Search",
    "semantic_search": "Email Search",
    "get_email": "Reading Email",
    "get_emails": "Reading Emails",
    "get_conversation": "Reading Email Thread",
    "list_conversations": "Listing Email Threads",
    "email_stats": "Email Stats",
    "find_similar_responses": "Finding Similar Emails",
    "extract_from_emails": "Extracting from Emails",
    # OpenAlex
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
    # Vikunja
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
    # Document processing
    "convert_pdf_to_md": "Converting PDF",
    "get_conversion_result": "Checking PDF Conversion",
    # Local tools
    "download_file": "Downloading File",
    "save_to_inbox": "Saving to Inbox",
    "read_document": "Preparing Document",
    "read_item_content": "Reading Item Content",
    "process_pdf": "Processing PDF",
}

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
- Task management via Vikunja for creating, searching, and updating tasks.
  Vikunja guidelines:
  * Always set done=false when searching tasks unless Dave asks about completed ones.
  * Sort by due_date or created when listing tasks so the most relevant appear first.
  * When creating tasks, ask which project if not obvious from context.
  * Key projects: Inbox (id=1), Teaching and Trent (id=9), math1052 (id=10),
    amod5240 (id=2), math3560 (id=3), Email Tasks (id=14), Personal and
    Professional (id=13), PhD (id=4), Projects (id=5), AI Projects (id=6),
    SSC 2026 Workshop (id=11), Exploration (id=8).
  * Default to Inbox (id=1) if Dave doesn't specify a project.
- PDF processing via process_pdf for converting PDFs to markdown. This runs in the
  background and saves the result to the knowledge inbox — use this instead of
  calling convert_pdf_to_md directly so Dave can keep talking while it processes.
- File download for fetching files from URLs to local storage
- Document reader via read_document for reading papers and documents aloud.
  When Dave says "read this document", "read this paper", or provides a file
  path to read aloud, use read_document. Math expressions are automatically
  converted to natural speech. The document will be available at /reader.

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
