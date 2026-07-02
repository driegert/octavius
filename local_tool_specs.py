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
            "name": "search_conversation_history",
            "description": (
                "Search Dave's prior Octavius conversations by semantic meaning. "
                "Use when he asks things like 'did we talk about X?', 'when did "
                "we last discuss Y?', or 'remind me what we decided about Z'. "
                "Returns past conversations with their one-line summary, age, "
                "and topic tags. Note: purely retrieval-only conversations "
                "(e.g. just listing emails or tasks) are not indexed, so the "
                "absence of a match may mean nothing substantive was said."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language search phrase.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max conversations to return (1-20, default 5).",
                    },
                },
                "required": ["query"],
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
