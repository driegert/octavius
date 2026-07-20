TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "download_file",
            "description": (
                "Download a file from a URL to local storage. "
                "Useful for fetching PDFs, documents, or other files that can "
                "then be processed with other tools (e.g., convert_pdf_to_md). "
                "Returns the local file path on success."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL of the file to download.",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Optional filename to save as. If not provided, inferred from the URL.",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_note",
            "description": (
                "Create a note in Dave's vault (the 001-Fleeting capture area). "
                "Use for saving search summaries, article content, or freeform "
                "notes Dave wants to keep or act on later. Writes a markdown file "
                "with frontmatter and returns its vault path. New notes always go "
                "to the fleeting folder; Dave files them in Obsidian himself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short descriptive title (becomes the filename and frontmatter title).",
                    },
                    "content": {
                        "type": "string",
                        "description": "Note body in markdown (no frontmatter — the server adds it).",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional 1-2 lowercase topic tags (a 'fleeting' tag is always added).",
                    },
                },
                "required": ["title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "description": (
                "Start the document reader for a PDF, markdown file, or article. "
                "Ingests the document, converts math expressions to speech-friendly text, "
                "and prepares it for audio playback in the reader UI at /reader."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path to the document (PDF or markdown).",
                    },
                    "title": {
                        "type": "string",
                        "description": "Title for the document.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_note",
            "description": (
                "Read a note from Dave's vault by its path (as returned by "
                "save_note or search_vault). Returns the note's title, full "
                "content, and a base_hash you pass back when editing it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Vault-relative path of the note, e.g. '00-zettelkasten/001-Fleeting/2026-07-08 my note.md'.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_note",
            "description": (
                "Edit the full content of an existing vault note. ALWAYS call "
                "read_note first to get the note's current base_hash, then pass "
                "it here. For notes in 001-Fleeting this writes immediately (guarded "
                "by base_hash — a mismatch means it changed under you; re-read "
                "and retry). For notes ANYWHERE ELSE it does NOT write — it "
                "returns a preview plus base_hash; confirm with Dave, then call "
                "commit_edit to save it. Never renames or moves a note; pass the "
                "note's exact path."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Vault-relative path of the note to edit.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The complete new file text (frontmatter + body).",
                    },
                    "base_hash": {
                        "type": "string",
                        "description": "The base_hash from read_note (optimistic-concurrency guard).",
                    },
                },
                "required": ["path", "content", "base_hash"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "commit_edit",
            "description": (
                "Commit an edit previewed by edit_note for a note outside "
                "001-Fleeting. Writes only if base_hash still matches the note on "
                "disk (optimistic concurrency); on mismatch, re-read and retry."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Vault-relative path of the note (unchanged from edit_note).",
                    },
                    "content": {
                        "type": "string",
                        "description": "The complete new file text to write.",
                    },
                    "base_hash": {
                        "type": "string",
                        "description": "The base_hash returned by edit_note / read_note.",
                    },
                },
                "required": ["path", "content", "base_hash"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "process_pdf",
            "description": (
                "Convert a PDF to markdown in the background. Returns immediately — "
                "the result will be saved to Dave's stash when processing "
                "completes. Use this instead of convert_pdf_to_md for a non-blocking "
                "experience."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the PDF file to process.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Title for the inbox item.",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_reader_documents",
            "description": (
                "List documents in the reader (PDFs, markdown files, and articles "
                "prepared for audio playback). Use when Dave asks 'what's in the reader', "
                "'is that PDF ready yet', or 'did the conversion finish'. Documents "
                "with status='processing' are still being converted; 'ready' means "
                "playable; 'failed' means conversion hit an error."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["processing", "ready", "failed", "all"],
                        "description": "Filter by status. Defaults to all statuses.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max documents to return (1-50, default 20).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_conversation_history",
            "description": (
                "Search Dave's prior Octavius conversations by semantic meaning, "
                "or list recent ones. Use when he asks things like 'did we talk "
                "about X?', 'when did we last discuss Y?', or 'pull in my voice "
                "conversation from this morning'. Returns past conversations "
                "with their #id, source channel, start time, one-line summary, "
                "and topic tags; feed a #id to read_conversation for the full "
                "transcript. Omit query and pass source and/or since to get a "
                "recency listing instead (that also finds retrieval-only chats "
                "that semantic search skips)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Natural-language search phrase. Optional if source "
                            "or since is given (then lists recent conversations)."
                        ),
                    },
                    "source": {
                        "type": "string",
                        "enum": ["voice", "matrix", "text"],
                        "description": "Only conversations from this channel.",
                    },
                    "since": {
                        "type": "string",
                        "description": (
                            "Only conversations started on/after this local date "
                            "or datetime, e.g. '2026-07-20' for 'today'."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max conversations to return (1-20, default 5).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_conversation",
            "description": (
                "Read the full transcript of a prior Octavius conversation by "
                "its #id (from search_conversation_history). Works across "
                "channels — e.g. pull a past voice conversation into a Matrix "
                "thread to continue it in text. Long transcripts are paged: "
                "page 1 is the most recent stretch, higher pages go further "
                "back in time."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "conversation_id": {
                        "type": "integer",
                        "description": "The conversation #id to read.",
                    },
                    "page": {
                        "type": "integer",
                        "description": (
                            "Transcript page (default 1 = most recent messages)."
                        ),
                    },
                },
                "required": ["conversation_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "consult_specialist",
            "description": (
                "Consult a scoped specialist assistant and get its answer back "
                "immediately, in this same turn. Use for: "
                "email (searching, reading, summarizing Dave's email), "
                "research (finding papers, authors, citations via OpenAlex), "
                "or tasks (searching, creating, updating Vikunja tasks). "
                "This runs synchronously: it returns the specialist's findings as "
                "the tool result, and you then weave them into your spoken reply "
                "to Dave. Do NOT acknowledge-and-stop; wait for the result and "
                "answer. Include all relevant context in the task description — "
                "the specialist only sees what you pass, not the conversation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "enum": ["email", "tasks", "research"],
                        "description": "The specialist domain.",
                    },
                    "task": {
                        "type": "string",
                        "description": (
                            "Clear description of what to do. Include dates, names, "
                            "project names, or other details from the conversation."
                        ),
                    },
                },
                "required": ["domain", "task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_document_status",
            "description": (
                "Check the conversion status of a PDF submitted to the docproc "
                "queue — e.g. one Dave sent as a document over Matrix. Use when "
                "he asks things like 'is that PDF ready?', 'what happened to "
                "that document I sent?', or asks for the converted markdown of "
                "a document you previously mentioned. Pass the job_id that was "
                "given when the PDF was received."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The docproc job id mentioned when the PDF was received.",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "Durably remember a fact Dave states about himself, his work, "
                "preferences, projects, or the people/tools/places in his life. "
                "Use when he says things like 'remember that...', 'note that I...', "
                "or states a stable preference you should keep across conversations. "
                "Do NOT use for one-off task details or transient state."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "statement": {
                        "type": "string",
                        "description": "The fact to remember, as a plain statement.",
                    },
                },
                "required": ["statement"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget",
            "description": (
                "Forget a previously remembered fact. Use when Dave says 'forget "
                "that...', 'that's no longer true', or asks you to drop something "
                "from memory. Soft-deletes; it won't be re-learned automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "fact": {
                        "type": "string",
                        "description": "Description of the fact to forget.",
                    },
                },
                "required": ["fact"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "correct",
            "description": (
                "Replace a remembered fact with an updated one. Use when Dave "
                "corrects something you know ('actually I now use X, not Y', "
                "'I moved to Z'). The old fact is retired and the new one stored."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "old": {
                        "type": "string",
                        "description": "The outdated fact to replace.",
                    },
                    "new": {
                        "type": "string",
                        "description": "The corrected fact, as a plain statement.",
                    },
                },
                "required": ["old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "what_do_you_know",
            "description": (
                "List the durable facts you've stored about Dave. Use when he asks "
                "'what do you know about me?' or 'what do you remember about X?'. "
                "Optionally scope to a topic with 'about'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "about": {
                        "type": "string",
                        "description": "Optional topic/entity to filter by.",
                    },
                },
                "required": [],
            },
        },
    },
]
