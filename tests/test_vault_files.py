import hashlib
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import vault_files


class VaultTestCase(unittest.TestCase):
    """Point vault_files at a throwaway vault for the duration of a test."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.vault = Path(tmp.name) / "vault"
        (self.vault / vault_files.INBOX_DIR).mkdir(parents=True)
        (self.vault / "03-personal" / "Journaling").mkdir(parents=True)
        for patcher in (
            patch.object(vault_files, "VAULT_PATH", self.vault),
            patch.object(
                vault_files,
                "_JOURNALING_RESOLVED",
                (self.vault / "03-personal/Journaling").resolve(),
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, rel_path: str, content: str) -> Path:
        target = self.vault / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target


class PathSafetyTests(VaultTestCase):
    def test_absolute_path_rejected(self):
        with self.assertRaises(vault_files.ForbiddenError):
            vault_files.read_note("/etc/passwd")

    def test_traversal_rejected(self):
        with self.assertRaises(vault_files.ForbiddenError):
            vault_files.read_note("../outside.md")
        with self.assertRaises(vault_files.ForbiddenError):
            vault_files.read_note("01-Inbox/../../outside.md")

    def test_empty_and_nul_paths_rejected(self):
        with self.assertRaises(vault_files.ForbiddenError):
            vault_files.read_note("")
        with self.assertRaises(vault_files.ForbiddenError):
            vault_files.read_note("01-Inbox/a\x00b.md")

    def test_journaling_read_rejected(self):
        self.write("03-personal/Journaling/secret.md", "private")
        with self.assertRaises(vault_files.ForbiddenError):
            vault_files.read_note("03-personal/Journaling/secret.md")

    def test_journaling_write_rejected(self):
        self.write("03-personal/Journaling/secret.md", "private")
        with self.assertRaises(vault_files.ForbiddenError):
            vault_files.commit_edit(
                "03-personal/Journaling/secret.md", "overwritten", "any"
            )

    def test_journaling_sibling_prefix_allowed(self):
        # "JournalingX.md" shares the string prefix but is not inside the dir.
        self.write("03-personal/JournalingX.md", "fine")
        note = vault_files.read_note("03-personal/JournalingX.md")
        self.assertEqual(note["content"], "fine")

    def test_symlink_escape_rejected(self):
        outside = self.vault.parent / "outside.md"
        outside.write_text("leaked", encoding="utf-8")
        (self.vault / "link.md").symlink_to(outside)
        with self.assertRaises(vault_files.ForbiddenError):
            vault_files.read_note("link.md")

    def test_symlink_into_journaling_rejected(self):
        secret = self.write("03-personal/Journaling/secret.md", "private")
        (self.vault / "innocent.md").symlink_to(secret)
        with self.assertRaises(vault_files.ForbiddenError):
            vault_files.read_note("innocent.md")

    def test_is_denylisted(self):
        self.assertTrue(vault_files.is_denylisted("03-personal/Journaling/x.md"))
        self.assertTrue(vault_files.is_denylisted("03-personal/Journaling"))
        self.assertTrue(vault_files.is_denylisted("../escape.md"))
        self.assertFalse(vault_files.is_denylisted("01-Inbox/x.md"))
        self.assertFalse(vault_files.is_denylisted("03-personal/JournalingX.md"))

    def test_is_in_inbox(self):
        self.assertTrue(vault_files.is_in_inbox("01-Inbox/x.md"))
        self.assertTrue(vault_files.is_in_inbox("01-Inbox"))
        self.assertFalse(vault_files.is_in_inbox("01-Inboxy/x.md"))
        self.assertFalse(vault_files.is_in_inbox("02-Notes/x.md"))


class CreateNoteTests(VaultTestCase):
    def test_creates_dated_file_in_inbox_with_frontmatter(self):
        res = vault_files.create_note("My Note", "body text", ["Research"])
        today = date.today().isoformat()
        self.assertEqual(res["path"], f"01-Inbox/{today} My Note.md")
        content = (self.vault / res["path"]).read_text(encoding="utf-8")
        self.assertTrue(content.startswith("---\n"))
        self.assertIn('title: "My Note"', content)
        self.assertIn(f"created: {today}", content)
        self.assertIn("  - fleeting", content)
        self.assertIn("  - research", content)  # lowercased
        self.assertTrue(content.endswith("body text"))

    def test_title_required(self):
        with self.assertRaises(vault_files.VaultError):
            vault_files.create_note("   ", "body")

    def test_filename_collision_gets_suffix(self):
        first = vault_files.create_note("Same Title", "one")
        second = vault_files.create_note("Same Title", "two")
        today = date.today().isoformat()
        self.assertEqual(first["path"], f"01-Inbox/{today} Same Title.md")
        self.assertEqual(second["path"], f"01-Inbox/{today} Same Title (2).md")

    def test_bad_filename_chars_sanitized(self):
        res = vault_files.create_note('a/b:c*d?"e', "body")
        name = Path(res["path"]).name
        for ch in '/\\:*?"<>|':
            self.assertNotIn(ch, name.replace(".md", ""))

    def test_long_title_truncated_in_filename(self):
        res = vault_files.create_note("x" * 200, "body")
        self.assertLessEqual(len(Path(res["path"]).stem), 80 + len("2026-01-01 "))

    def test_base_hash_matches_file_bytes(self):
        res = vault_files.create_note("Hash Check", "body")
        data = (self.vault / res["path"]).read_bytes()
        self.assertEqual(res["base_hash"], hashlib.sha256(data).hexdigest())

    def test_write_honors_umask(self):
        old = os.umask(0o027)
        try:
            res = vault_files.create_note("Perm Test", "body")
        finally:
            os.umask(old)
        mode = (self.vault / res["path"]).stat().st_mode & 0o777
        self.assertEqual(mode, 0o640)

    def test_symlinked_inbox_escaping_vault_rejected(self):
        outside = self.vault.parent / "elsewhere"
        outside.mkdir()
        inbox = self.vault / vault_files.INBOX_DIR
        inbox.rmdir()
        inbox.symlink_to(outside)
        with self.assertRaises(vault_files.ForbiddenError):
            vault_files.create_note("Escape", "body")


class ReadNoteTests(VaultTestCase):
    def test_missing_note_raises_not_found(self):
        with self.assertRaises(vault_files.NotFoundError):
            vault_files.read_note("01-Inbox/nope.md")

    def test_title_from_frontmatter(self):
        self.write(
            "01-Inbox/x.md", '---\ntitle: "Real Title"\ncreated: 2026-07-10\n---\n\nbody'
        )
        note = vault_files.read_note("01-Inbox/x.md")
        self.assertEqual(note["title"], "Real Title")
        self.assertEqual(note["path"], "01-Inbox/x.md")
        self.assertIn("body", note["content"])

    def test_title_falls_back_to_stem(self):
        self.write("01-Inbox/plain note.md", "no frontmatter here")
        note = vault_files.read_note("01-Inbox/plain note.md")
        self.assertEqual(note["title"], "plain note")

    def test_base_hash_is_sha256_of_bytes(self):
        target = self.write("01-Inbox/h.md", "content")
        note = vault_files.read_note("01-Inbox/h.md")
        self.assertEqual(
            note["base_hash"], hashlib.sha256(target.read_bytes()).hexdigest()
        )


class CommitEditTests(VaultTestCase):
    def test_missing_note_raises_not_found(self):
        with self.assertRaises(vault_files.NotFoundError):
            vault_files.commit_edit("01-Inbox/nope.md", "new", "hash")

    def test_stale_hash_raises_conflict_with_current_hash(self):
        res = vault_files.create_note("Conflict", "original")
        with self.assertRaises(vault_files.ConflictError) as ctx:
            vault_files.commit_edit(res["path"], "new content", "deadbeef")
        self.assertEqual(ctx.exception.current_base_hash, res["base_hash"])
        # Nothing was written.
        self.assertIn("original", (self.vault / res["path"]).read_text())

    def test_matching_hash_writes_and_returns_new_hash(self):
        res = vault_files.create_note("Edit Me", "original")
        out = vault_files.commit_edit(res["path"], "new content", res["base_hash"])
        self.assertEqual(out["path"], res["path"])
        self.assertEqual((self.vault / res["path"]).read_text(), "new content")
        self.assertEqual(
            out["base_hash"], hashlib.sha256(b"new content").hexdigest()
        )


class ListRecentInboxTests(VaultTestCase):
    def test_missing_inbox_returns_empty(self):
        (self.vault / vault_files.INBOX_DIR).rmdir()
        self.assertEqual(vault_files.list_recent_inbox(), [])

    def test_newest_first_with_limit_and_snippet(self):
        older = self.write(
            "01-Inbox/old.md", '---\ntitle: "Old"\ncreated: 2026-07-01\n---\n\nold body'
        )
        newer = self.write("01-Inbox/new.md", "new body")
        os.utime(older, (1_000_000, 1_000_000))
        os.utime(newer, (2_000_000, 2_000_000))
        items = vault_files.list_recent_inbox(limit=1)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["path"], "01-Inbox/new.md")
        items = vault_files.list_recent_inbox()
        self.assertEqual([i["path"] for i in items], ["01-Inbox/new.md", "01-Inbox/old.md"])
        # Snippet is the body only — frontmatter stripped.
        self.assertEqual(items[1]["snippet"], "old body")
        self.assertEqual(items[1]["title"], "Old")
        self.assertEqual(items[1]["created"], "2026-07-01")


if __name__ == "__main__":
    unittest.main()
