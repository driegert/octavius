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
            "name": "save_to_inbox",
            "description": (
                "Save content to Dave's knowledge inbox for later review. "
                "Use for: saving search summaries, article content, freeform notes, "
                "or email drafts that Dave wants to review or act on later."
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
                "Read a chunk of content from a saved inbox item. Use this to access "
                "the full content of an item you're discussing with Dave. Returns the "
                "content from the given offset with the specified character limit."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "integer",
                        "description": "The inbox item ID to read from.",
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
                "the result will be saved to Dave's knowledge inbox when processing "
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
]
