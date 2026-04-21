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
            "name": "save_to_stash",
            "description": (
                "Save content to Dave's stash for later review. "
                "Use for: saving search summaries, article content, freeform notes, "
                "or email drafts that Dave wants to review or act on later. "
                "(The stash is Dave's personal capture area — distinct from his email inbox.)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short descriptive title for the saved item.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full content to save.",
                    },
                    "item_type": {
                        "type": "string",
                        "enum": ["note", "search_summary", "article", "email_draft"],
                        "description": "Type of content being saved.",
                    },
                    "source_url": {
                        "type": "string",
                        "description": "Source URL if applicable.",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "Type-specific data. For email_draft: {to, subject, in_reply_to}.",
                    },
                },
                "required": ["title", "content", "item_type"],
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
            "name": "read_item_content",
            "description": (
                "Read a chunk of content from a saved stash item. Use this to access "
                "the full content of an item you're discussing with Dave. Returns the "
                "content from the given offset with the specified character limit."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "integer",
                        "description": "The stash item ID to read from.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Character offset to start reading from. Default 0.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum characters to return. Default 4000.",
                    },
                },
                "required": ["item_id"],
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
            "name": "list_stash_items",
            "description": (
                "List items in Dave's stash (the personal capture area for notes, "
                "search summaries, articles, and email drafts). Defaults to pending "
                "items only. Use when Dave asks things like 'what's in my stash', "
                "'what did I save', or 'what's still pending to review'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["pending", "done", "dismissed", "all"],
                        "description": "Filter by status. Defaults to 'pending'. Use 'all' for no filter.",
                    },
                    "item_type": {
                        "type": "string",
                        "enum": ["note", "search_summary", "article", "email_draft"],
                        "description": "Optional filter by item type.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max items to return (1-50, default 20).",
                    },
                },
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
            "name": "delegate_task",
            "description": (
                "Start a backgrounded specialist task. Use for: "
                "email (searching, reading, summarizing emails), "
                "research (finding papers, authors, citations via OpenAlex), "
                "or tasks (searching, creating, updating Vikunja tasks). "
                "This tool returns IMMEDIATELY with a handle — the specialist "
                "runs in the background on a separate machine and the result "
                "will be spoken to Dave when it finishes. "
                "After calling this, briefly acknowledge to Dave (e.g. 'on it', "
                "'checking now') and END YOUR TURN — do not stall or loop. "
                "Include all relevant context in the task description; the "
                "specialist only sees what you pass, not the conversation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "enum": ["email", "research", "tasks"],
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
            "name": "cancel_delegation",
            "description": (
                "Cancel a previously-started delegation by its handle. Use when "
                "Dave changes his mind mid-task (e.g. 'never mind', 'forget that', "
                "'actually don't') while a delegation is still running. The handle "
                "was returned by delegate_task. Safe to call even if the task has "
                "already finished — returns cancelled=false in that case."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "handle": {
                        "type": "string",
                        "description": "The delegation handle returned by delegate_task.",
                    },
                },
                "required": ["handle"],
            },
        },
    },
]
