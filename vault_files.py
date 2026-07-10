"""Pure file-I/O helpers over the Obsidian vault (single source of truth).

The vault (`VAULT_PATH`, default ~/Documents/Personal) is plain `.md` files on
triplestuffed. Agents and the Android app read/create/edit notes directly here;
search goes through the derived index (the `search_vault` MCP), never this module.

Rules enforced (see the frozen vault API contract):
  - New notes land in `01-Inbox/` only; filename frozen at creation.
  - `03-personal/Journaling/` is off-limits: never listed, read, or written.
  - Every `path` is vault-relative POSIX; traversal / symlink escapes rejected.
  - Writes are atomic (temp file in the same dir, then os.replace).
  - `base_hash` = hex sha256 of the file bytes (optimistic concurrency).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath

VAULT_PATH = Path(
    os.environ.get("VAULT_PATH", str(Path.home() / "Documents/Personal"))
).expanduser()

INBOX_DIR = "01-Inbox"
JOURNALING_PREFIX = "03-personal/Journaling/"
_JOURNALING_RESOLVED = (VAULT_PATH / "03-personal/Journaling").resolve()
SNIPPET_LENGTH = 150

_FILENAME_BAD = re.compile(r'[\\/:*?"<>|\x00\n\r\t]+')
_TITLE_RE = re.compile(r"^title:\s*(.+?)\s*$", re.MULTILINE)
_CREATED_RE = re.compile(r"^created:\s*(.+?)\s*$", re.MULTILINE)


class VaultError(Exception):
    status = 400


class ForbiddenError(VaultError):
    status = 403


class NotFoundError(VaultError):
    status = 404


class ConflictError(VaultError):
    status = 409

    def __init__(self, message: str = "base_hash mismatch", current_base_hash: str = ""):
        super().__init__(message)
        self.current_base_hash = current_base_hash


def _normalize(rel_path: str) -> str:
    """Return a clean vault-relative POSIX path or raise ForbiddenError."""
    if not rel_path or "\x00" in rel_path:
        raise ForbiddenError("empty or invalid path")
    rel = PurePosixPath(rel_path.strip())
    if rel.is_absolute() or ".." in rel.parts:
        raise ForbiddenError("unsafe path")
    return "/".join(p for p in rel.parts if p not in ("", "."))


def is_denylisted(rel_path: str) -> bool:
    try:
        norm = _normalize(rel_path)
    except VaultError:
        return True
    return norm == JOURNALING_PREFIX.rstrip("/") or norm.startswith(JOURNALING_PREFIX)


def is_in_inbox(rel_path: str) -> bool:
    norm = _normalize(rel_path)
    return norm == INBOX_DIR or norm.startswith(INBOX_DIR + "/")


def _resolve(rel_path: str) -> tuple[Path, str]:
    """Map a vault-relative path to an absolute path inside the vault.

    Rejects journaling, traversal, and symlink escapes. Returns (abs, norm).
    """
    norm = _normalize(rel_path)
    if is_denylisted(norm):
        raise ForbiddenError("path is in the journaling denylist")
    target = VAULT_PATH / Path(norm)
    resolved = target.resolve()
    vault_root = VAULT_PATH.resolve()
    if resolved != vault_root and vault_root not in resolved.parents:
        raise ForbiddenError("path escapes the vault")
    # Also check the RESOLVED path: a symlink inside the vault could point into
    # journaling even when the lexical path looks clean.
    if resolved == _JOURNALING_RESOLVED or _JOURNALING_RESOLVED in resolved.parents:
        raise ForbiddenError("path resolves into the journaling denylist")
    return target, norm


def _frontmatter_block(content: str) -> str:
    if not content.startswith("---"):
        return ""
    end = content.find("\n---", 3)
    return content[3:end] if end != -1 else ""


def _strip_frontmatter(content: str) -> str:
    if not content.startswith("---"):
        return content
    end = content.find("\n---", 3)
    if end == -1:
        return content
    rest = content[end + 4:]
    return rest.lstrip("\n")


def _title_of(content: str, target: Path) -> str:
    fm = _frontmatter_block(content)
    if fm:
        m = _TITLE_RE.search(fm)
        if m:
            return _unyaml(m.group(1))
    return target.stem


def _created_of(content: str) -> str | None:
    fm = _frontmatter_block(content)
    if fm:
        m = _CREATED_RE.search(fm)
        if m:
            return _unyaml(m.group(1))
    return None


def _unyaml(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        try:
            return json.loads(value) if value[0] == '"' else value[1:-1]
        except json.JSONDecodeError:
            return value[1:-1]
    return value


def _yaml_str(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _snippet(text: str, length: int = SNIPPET_LENGTH) -> str:
    flat = " ".join(text.split())
    if len(flat) <= length:
        return flat
    return flat[:length].rsplit(" ", 1)[0] + "..."


def _atomic_write(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp forces 0600; match normal file creation so notes aren't more
        # restrictive than the rest of the vault (typically 0664 under umask 002).
        _umask = os.umask(0)
        os.umask(_umask)
        os.chmod(tmp, 0o666 & ~_umask)
        os.replace(tmp, target)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def list_recent_inbox(limit: int = 20) -> list[dict]:
    limit = max(1, min(int(limit), 100))
    inbox = VAULT_PATH / INBOX_DIR
    if not inbox.is_dir():
        return []
    entries = []
    for p in inbox.rglob("*.md"):
        if not p.is_file():
            continue
        entries.append((p.stat().st_mtime, p))
    entries.sort(key=lambda t: t[0], reverse=True)
    out = []
    for mtime, p in entries[:limit]:
        content = p.read_text(encoding="utf-8", errors="replace")
        out.append({
            "path": p.relative_to(VAULT_PATH).as_posix(),
            "title": _title_of(content, p),
            "created": _created_of(content),
            "modified": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
            "snippet": _snippet(_strip_frontmatter(content)),
        })
    return out


def read_note(rel_path: str) -> dict:
    target, norm = _resolve(rel_path)
    if not target.is_file():
        raise NotFoundError(f"note not found: {norm}")
    data = target.read_bytes()
    content = data.decode("utf-8", errors="replace")
    return {
        "path": norm,
        "title": _title_of(content, target),
        "content": content,
        "base_hash": hashlib.sha256(data).hexdigest(),
    }


def create_note(title: str, content: str, tags: list[str] | None = None) -> dict:
    title = (title or "").strip()
    if not title:
        raise VaultError("title is required")
    today = date.today().isoformat()
    stem = _FILENAME_BAD.sub(" ", title).strip()
    stem = re.sub(r"\s+", " ", stem)[:80].rstrip() or "note"
    inbox = VAULT_PATH / INBOX_DIR
    inbox.mkdir(parents=True, exist_ok=True)
    # Guard a symlinked 01-Inbox that escapes the vault.
    inbox_resolved = inbox.resolve()
    vault_root = VAULT_PATH.resolve()
    if inbox_resolved != vault_root and vault_root not in inbox_resolved.parents:
        raise ForbiddenError("inbox path escapes the vault")
    # Reserve the filename atomically (O_EXCL) so concurrent same-title creates
    # can't pick the same name and clobber each other.
    target = inbox / f"{today} {stem}.md"
    n = 2
    while True:
        try:
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            target = inbox / f"{today} {stem} ({n}).md"
            n += 1
            continue
        os.close(fd)
        break
    full = _frontmatter(title, today, tags) + (content or "")
    data = full.encode("utf-8")
    _atomic_write(target, data)
    return {
        "path": target.relative_to(VAULT_PATH).as_posix(),
        "base_hash": hashlib.sha256(data).hexdigest(),
    }


def _frontmatter(title: str, created: str, tags: list[str] | None) -> str:
    tag_list = ["fleeting"]
    for t in tags or []:
        t = str(t).strip().lower()
        if t and t not in tag_list:
            tag_list.append(t)
    lines = ["---", f"title: {_yaml_str(title)}", f"created: {created}", "tags:"]
    lines += [f"  - {t}" for t in tag_list]
    lines += ["---", ""]
    return "\n".join(lines) + "\n"


def commit_edit(rel_path: str, content: str, base_hash: str) -> dict:
    target, norm = _resolve(rel_path)
    if not target.is_file():
        raise NotFoundError(f"note not found: {norm}")
    current = target.read_bytes()
    current_hash = hashlib.sha256(current).hexdigest()
    if current_hash != base_hash:
        raise ConflictError(current_base_hash=current_hash)
    # Known lockless optimistic-concurrency window: another writer could replace
    # the file between this hash check and os.replace. Acceptable for a
    # single-user vault; not guarded.
    data = content.encode("utf-8")
    _atomic_write(target, data)
    return {"path": norm, "base_hash": hashlib.sha256(data).hexdigest()}
