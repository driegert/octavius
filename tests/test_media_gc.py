import importlib.machinery
import importlib.util
import os
import tempfile
import time
import unittest
from pathlib import Path

_path = Path(__file__).resolve().parents[1] / "scripts" / "octavius-media-gc"
_loader = importlib.machinery.SourceFileLoader("octavius_media_gc", str(_path))
_spec = importlib.util.spec_from_loader("octavius_media_gc", _loader)
gc = importlib.util.module_from_spec(_spec)
_loader.exec_module(gc)

DAY = 86400


def make(path: Path, age_days: float, size: int = 10) -> Path:
    path.write_bytes(b"x" * size)
    t = time.time() - age_days * DAY
    os.utime(path, (t, t))
    return path


class MediaGcTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.spool = self.root / "spool"
        self.spool.mkdir()

    def test_age_cutoff(self):
        old = make(self.spool / "old.png", 40, 100)
        new = make(self.spool / "new.png", 5)
        (rep,) = gc.run([str(self.spool)], 30, apply=True)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        self.assertEqual((rep.scanned, rep.selected, rep.deleted, rep.deleted_bytes), (2, 1, 1, 100))
        self.assertGreater(rep.oldest_selected_age_days, 39)

    def test_dry_run_deletes_nothing(self):
        old = make(self.spool / "old.png", 90, 50)
        (rep,) = gc.run([str(self.spool)], 30, apply=False)
        self.assertTrue(old.exists())
        self.assertEqual((rep.selected, rep.selected_bytes, rep.deleted), (1, 50, 0))

    def test_ignores_subdirs_and_their_contents(self):
        sub = self.spool / "sub"
        sub.mkdir()
        inner = make(sub / "inner.pdf", 100)
        os.utime(sub, (0, 0))
        gc.run([str(self.spool)], 30, apply=True)
        self.assertTrue(sub.is_dir())
        self.assertTrue(inner.exists())

    def test_ignores_symlinks_and_never_follows(self):
        outside = make(self.root / "outside.txt", 100)
        link = self.spool / "link.png"
        link.symlink_to(outside)
        os.utime(link, (0, 0), follow_symlinks=False)
        (rep,) = gc.run([str(self.spool)], 30, apply=True)
        self.assertTrue(link.is_symlink())
        self.assertTrue(outside.exists())
        self.assertEqual(rep.scanned, 0)

    def test_missing_dir_reports_error_not_crash(self):
        (rep,) = gc.run([str(self.root / "nope")], 30, apply=True)
        self.assertEqual(len(rep.errors), 1)

    def test_rejects_short_retention(self):
        with self.assertRaises(SystemExit):
            gc.main(["--days", "0", "--dir", str(self.spool)])

    def test_cli_dry_run_default(self):
        old = make(self.spool / "old.png", 90)
        self.assertEqual(gc.main(["--dir", str(self.spool)]), 0)
        self.assertTrue(old.exists())
        self.assertEqual(gc.main(["--dir", str(self.spool), "--apply"]), 0)
        self.assertFalse(old.exists())


if __name__ == "__main__":
    unittest.main()
