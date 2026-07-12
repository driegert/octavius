"""Local tools backing the vault as the single note store.

save_note / read_note / edit_note / commit_edit map the agent surface onto the
pure file I/O in vault_files.py. Search is a separate MCP tool (search_vault).
"""
from __future__ import annotations

import vault_files


def save_note(args: dict, session=None, _mcp_manager=None) -> str:
    title = args.get("title", "")
    content = args.get("content", "")
    if not title or not content:
        return "Error: title and content are required."
    tags = args.get("tags") or []
    try:
        res = vault_files.create_note(title, content, tags)
    except vault_files.VaultError as e:
        return f"Error: {e}"
    return f"Saved note to the vault: {res['path']}"


def read_note(args: dict, session=None, _mcp_manager=None) -> str:
    path = args.get("path", "")
    if not path:
        return "Error: path is required."
    try:
        note = vault_files.read_note(path)
    except vault_files.NotFoundError:
        return f"Error: note not found: {path}"
    except vault_files.VaultError as e:
        return f"Error: {e}"
    return (
        f"[{note['title']} — {note['path']} — base_hash={note['base_hash']}]"
        f"\n\n{note['content']}"
    )


def edit_note(args: dict, session=None, _mcp_manager=None) -> str:
    path = args.get("path", "")
    content = args.get("content")
    base_hash = args.get("base_hash", "")
    if not path or content is None:
        return "Error: path and content (full new file text) are required."
    if not base_hash:
        return (
            "Error: base_hash is required. Call read_note first to get the "
            "note's current base_hash, then pass it here."
        )
    try:
        # Fleeting notes write directly, but still hash-guarded against the
        # caller-supplied base_hash so a concurrent edit raises Conflict.
        if vault_files.is_in_fleeting(path):
            res = vault_files.commit_edit(path, content, base_hash)
            return f"Edited fleeting note {res['path']} (new base_hash={res['base_hash']})."
        current = vault_files.read_note(path)
    except vault_files.NotFoundError:
        return f"Error: note not found: {path}"
    except vault_files.ConflictError as e:
        return (
            "Error: the note changed since you read it (base_hash mismatch). "
            f"Current base_hash is {e.current_base_hash}. Re-read it and retry."
        )
    except vault_files.VaultError as e:
        return f"Error: {e}"
    return (
        "[PENDING EDIT — note is outside 001-Fleeting, nothing written yet]\n"
        f"path: {current['path']}\nbase_hash: {current['base_hash']}\n\n"
        "Confirm the change with Dave, then call "
        "commit_edit(path, content, base_hash) to write it.\n\n"
        f"Preview of new content:\n{vault_files._snippet(content, 400)}"
    )


def commit_edit(args: dict, session=None, _mcp_manager=None) -> str:
    path = args.get("path", "")
    content = args.get("content")
    base_hash = args.get("base_hash", "")
    if not path or content is None or not base_hash:
        return "Error: path, content, and base_hash are required."
    try:
        res = vault_files.commit_edit(path, content, base_hash)
    except vault_files.NotFoundError:
        return f"Error: note not found: {path}"
    except vault_files.ConflictError as e:
        return (
            "Error: the note changed since you read it (base_hash mismatch). "
            f"Current base_hash is {e.current_base_hash}. Re-read it and retry."
        )
    except vault_files.VaultError as e:
        return f"Error: {e}"
    return f"Committed edit to {res['path']} (new base_hash={res['base_hash']})."
