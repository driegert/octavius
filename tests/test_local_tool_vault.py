import unittest

import local_tool_vault
import vault_files
try:
    from test_vault_files import VaultTestCase  # unittest discover -s tests
except ImportError:
    from tests.test_vault_files import VaultTestCase


class SaveNoteTests(VaultTestCase):
    def test_requires_title_and_content(self):
        self.assertEqual(
            local_tool_vault.save_note({"title": "", "content": ""}),
            "Error: title and content are required.",
        )

    def test_success_returns_vault_path(self):
        result = local_tool_vault.save_note({"title": "A Note", "content": "body"})
        self.assertTrue(result.startswith("Saved note to the vault: 01-Inbox/"))
        path = result.split(": ", 1)[1]
        self.assertTrue((self.vault / path).is_file())

    def test_vault_error_surfaced_as_message(self):
        # Whitespace-only title passes the handler check but VaultError inside.
        result = local_tool_vault.save_note({"title": "   ", "content": "body"})
        self.assertTrue(result.startswith("Error:"))


class ReadNoteTests(VaultTestCase):
    def test_requires_path(self):
        self.assertEqual(local_tool_vault.read_note({}), "Error: path is required.")

    def test_missing_note(self):
        self.assertEqual(
            local_tool_vault.read_note({"path": "01-Inbox/nope.md"}),
            "Error: note not found: 01-Inbox/nope.md",
        )

    def test_forbidden_path_surfaced_as_error(self):
        result = local_tool_vault.read_note({"path": "03-personal/Journaling/x.md"})
        self.assertTrue(result.startswith("Error:"))

    def test_success_includes_header_and_content(self):
        res = vault_files.create_note("Read Me", "the body")
        result = local_tool_vault.read_note({"path": res["path"]})
        self.assertIn(f"[Read Me — {res['path']} — base_hash={res['base_hash']}]", result)
        self.assertIn("the body", result)


class EditNoteTests(VaultTestCase):
    def test_requires_path_and_content(self):
        result = local_tool_vault.edit_note({"path": "", "content": None})
        self.assertTrue(result.startswith("Error: path and content"))

    def test_requires_base_hash(self):
        result = local_tool_vault.edit_note(
            {"path": "01-Inbox/x.md", "content": "new"}
        )
        self.assertIn("base_hash is required", result)
        self.assertIn("read_note", result)

    def test_inbox_note_writes_directly(self):
        res = vault_files.create_note("Inbox Edit", "original")
        result = local_tool_vault.edit_note(
            {"path": res["path"], "content": "updated", "base_hash": res["base_hash"]}
        )
        self.assertIn("Edited inbox note", result)
        self.assertEqual((self.vault / res["path"]).read_text(), "updated")

    def test_inbox_note_stale_hash_conflict(self):
        res = vault_files.create_note("Inbox Conflict", "original")
        result = local_tool_vault.edit_note(
            {"path": res["path"], "content": "updated", "base_hash": "deadbeef"}
        )
        self.assertIn("base_hash mismatch", result)
        self.assertIn(res["base_hash"], result)
        self.assertIn("original", (self.vault / res["path"]).read_text())

    def test_outside_inbox_returns_pending_preview_without_writing(self):
        target = self.write("02-Notes/filed.md", "filed content")
        note = vault_files.read_note("02-Notes/filed.md")
        result = local_tool_vault.edit_note(
            {
                "path": "02-Notes/filed.md",
                "content": "proposed new content",
                "base_hash": note["base_hash"],
            }
        )
        self.assertIn("PENDING EDIT", result)
        self.assertIn(note["base_hash"], result)
        self.assertIn("proposed new content", result)
        # Nothing written until commit_edit.
        self.assertEqual(target.read_text(), "filed content")

    def test_missing_note_outside_inbox(self):
        result = local_tool_vault.edit_note(
            {"path": "02-Notes/nope.md", "content": "new", "base_hash": "h"}
        )
        self.assertEqual(result, "Error: note not found: 02-Notes/nope.md")


class CommitEditTests(VaultTestCase):
    def test_requires_all_args(self):
        result = local_tool_vault.commit_edit({"path": "x.md", "content": "new"})
        self.assertEqual(result, "Error: path, content, and base_hash are required.")

    def test_success_writes_and_reports_new_hash(self):
        self.write("02-Notes/filed.md", "filed content")
        note = vault_files.read_note("02-Notes/filed.md")
        result = local_tool_vault.commit_edit(
            {
                "path": "02-Notes/filed.md",
                "content": "committed content",
                "base_hash": note["base_hash"],
            }
        )
        self.assertIn("Committed edit to 02-Notes/filed.md", result)
        self.assertEqual(
            (self.vault / "02-Notes/filed.md").read_text(), "committed content"
        )

    def test_stale_hash_conflict_message(self):
        self.write("02-Notes/filed.md", "filed content")
        note = vault_files.read_note("02-Notes/filed.md")
        result = local_tool_vault.commit_edit(
            {"path": "02-Notes/filed.md", "content": "new", "base_hash": "deadbeef"}
        )
        self.assertIn("base_hash mismatch", result)
        self.assertIn(note["base_hash"], result)

    def test_missing_note(self):
        result = local_tool_vault.commit_edit(
            {"path": "02-Notes/nope.md", "content": "new", "base_hash": "h"}
        )
        self.assertEqual(result, "Error: note not found: 02-Notes/nope.md")


if __name__ == "__main__":
    unittest.main()
