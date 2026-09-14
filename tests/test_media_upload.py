"""POST /api/media/upload — the client-media upload contract.

Covers `media_uploads.save_upload` (pure logic, no FastAPI) and the route
wiring in `routes/media.py` through `fastapi.testclient.TestClient`. Every
test writes into a `tempfile.TemporaryDirectory`, never into
`settings.media_upload_dir`'s real default.
"""
import asyncio
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_uploads import (
    FILE_MAX_BYTES,
    IMAGE_MAX_BYTES,
    MediaUploadError,
    resolve_mime,
    resolve_spooled_media,
    sanitize_filename,
    save_upload,
)

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routes import media as media_route
except ImportError:  # pragma: no cover
    FastAPI = TestClient = media_route = None


class _FakeUpload:
    """Minimal stand-in for fastapi.UploadFile — just enough for save_upload."""

    def __init__(self, data: bytes, filename: str | None = "photo.jpg",
                 content_type: str | None = "image/jpeg"):
        self._buf = io.BytesIO(data)
        self.filename = filename
        self.content_type = content_type
        self.closed = False

    async def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)

    async def close(self) -> None:
        self.closed = True


def _run(coro):
    return asyncio.run(coro)


class SanitizeFilenameTests(unittest.TestCase):
    def test_path_traversal_reduced_to_basename(self):
        self.assertEqual(sanitize_filename("../../etc/passwd"), "passwd")

    def test_weird_characters_collapse_to_underscore(self):
        self.assertEqual(sanitize_filename("my photo!@#.jpg"), "my_photo___.jpg")

    def test_long_names_capped(self):
        name = ("a" * 200) + ".jpg"
        result = sanitize_filename(name)
        self.assertLessEqual(len(result), 80)

    def test_empty_or_none_falls_back_to_upload(self):
        self.assertEqual(sanitize_filename(""), "upload")
        self.assertEqual(sanitize_filename(None), "upload")

    def test_empty_after_stripping_falls_back_to_upload(self):
        # Sanitizes to nothing but dots/underscores once stripped.
        self.assertEqual(sanitize_filename("..."), "upload")


class ResolveMimeTests(unittest.TestCase):
    def test_content_type_is_honoured(self):
        self.assertEqual(resolve_mime("image/png", "photo.jpg"), "image/png")

    def test_octet_stream_falls_through_to_extension_guess(self):
        self.assertEqual(
            resolve_mime("application/octet-stream", "document.pdf"), "application/pdf"
        )

    def test_unknown_extension_defaults_to_octet_stream(self):
        self.assertEqual(
            resolve_mime("application/octet-stream", "mystery.xyzabc"),
            "application/octet-stream",
        )

    def test_missing_content_type_falls_through_to_extension_guess(self):
        self.assertEqual(resolve_mime(None, "note.txt"), "text/plain")


class SaveUploadTests(unittest.TestCase):
    def test_happy_path_image(self):
        data = b"\xff\xd8\xff" + b"x" * 100
        upload = _FakeUpload(data, filename="cat.jpg", content_type="image/jpeg")
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "media"
            result = _run(save_upload(upload, dest))

            self.assertEqual(result["mime"], "image/jpeg")
            self.assertEqual(result["filename"], "cat.jpg")
            self.assertEqual(result["size_bytes"], len(data))

            path = Path(result["path"])
            self.assertTrue(path.is_file())
            self.assertTrue(str(path).startswith(str(dest.resolve())))
            self.assertRegex(path.name, r"^[0-9a-f]{12}-cat\.jpg$")
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            self.assertTrue(upload.closed)

    def test_happy_path_pdf(self):
        data = b"%PDF-1.4\n" + b"y" * 500
        upload = _FakeUpload(data, filename="paper.pdf", content_type="application/pdf")
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "media"
            result = _run(save_upload(upload, dest))

            self.assertEqual(result["mime"], "application/pdf")
            self.assertEqual(result["filename"], "paper.pdf")
            path = Path(result["path"])
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_directory_created_on_demand(self):
        upload = _FakeUpload(b"abc", filename="a.txt", content_type="text/plain")
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "nested" / "media"
            self.assertFalse(dest.exists())
            _run(save_upload(upload, dest))
            self.assertTrue(dest.is_dir())

    def test_empty_body_raises_400(self):
        upload = _FakeUpload(b"", filename="empty.jpg", content_type="image/jpeg")
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "media"
            with self.assertRaises(MediaUploadError) as ctx:
                _run(save_upload(upload, dest))
            self.assertEqual(ctx.exception.status, 400)
            # No temp/partial file left behind.
            self.assertEqual(list(dest.glob("*")) if dest.exists() else [], [])

    def test_image_over_cap_returns_413_and_leaves_no_partial_file(self):
        upload = _FakeUpload(b"x" * 30, filename="big.jpg", content_type="image/jpeg")
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "media"
            with patch("media_uploads._cap_for",
                       side_effect=lambda mime: (10, "max 10 B for images")
                       if mime.startswith("image/") else (FILE_MAX_BYTES, "max 50 MB")):
                with self.assertRaises(MediaUploadError) as ctx:
                    _run(save_upload(upload, dest))
            self.assertEqual(ctx.exception.status, 413)
            self.assertIn("10 B", ctx.exception.message)
            remaining = list(dest.glob("*")) if dest.exists() else []
            self.assertEqual(remaining, [])

    def test_file_over_cap_returns_413_and_leaves_no_partial_file(self):
        upload = _FakeUpload(b"y" * 30, filename="big.bin", content_type="application/octet-stream")
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "media"
            with patch("media_uploads._cap_for",
                       side_effect=lambda mime: (10, "max 10 B for images")
                       if mime.startswith("image/") else (10, "max 10 B")):
                with self.assertRaises(MediaUploadError) as ctx:
                    _run(save_upload(upload, dest))
            self.assertEqual(ctx.exception.status, 413)
            self.assertIn("10 B", ctx.exception.message)
            remaining = list(dest.glob("*")) if dest.exists() else []
            self.assertEqual(remaining, [])


class ResolveSpooledMediaTests(unittest.TestCase):
    def test_empty_path_rejected(self):
        self.assertIsNone(resolve_spooled_media("", ["/tmp"]))

    def test_file_within_root_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "a.jpg"
            f.write_bytes(b"x")
            self.assertEqual(resolve_spooled_media(str(f), [tmp]), f.resolve())

    def test_file_within_nested_root_dir_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "sub" / "dir"
            nested.mkdir(parents=True)
            f = nested / "a.jpg"
            f.write_bytes(b"x")
            self.assertEqual(resolve_spooled_media(str(f), [tmp]), f.resolve())

    def test_file_outside_all_roots_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            f = Path(other) / "a.jpg"
            f.write_bytes(b"x")
            self.assertIsNone(resolve_spooled_media(str(f), [tmp]))

    def test_dangling_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(resolve_spooled_media(str(Path(tmp) / "nope.jpg"), [tmp]))

    def test_directory_rejected_not_a_regular_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(resolve_spooled_media(tmp, [tmp]))

    def test_symlink_inside_root_pointing_inside_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "real.jpg"
            target.write_bytes(b"data")
            link = Path(tmp) / "link.jpg"
            link.symlink_to(target)
            self.assertEqual(resolve_spooled_media(str(link), [tmp]), target.resolve())

    def test_symlink_escaping_root_is_rejected(self):
        """Symlinks are resolved BEFORE the containment check, so a link that
        lives inside the root but points outside it is caught rather than
        passing containment on its own (in-root) location."""
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            secret = Path(outside) / "secret.jpg"
            secret.write_bytes(b"data")
            link = Path(tmp) / "link.jpg"
            link.symlink_to(secret)
            self.assertIsNone(resolve_spooled_media(str(link), [tmp]))

    def test_nonexistent_root_is_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "a.jpg"
            f.write_bytes(b"x")
            result = resolve_spooled_media(str(f), ["/definitely/does/not/exist", tmp])
            self.assertEqual(result, f.resolve())

    def test_matches_second_of_multiple_roots(self):
        with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
            f = Path(tmp2) / "a.jpg"
            f.write_bytes(b"x")
            self.assertEqual(resolve_spooled_media(str(f), [tmp1, tmp2]), f.resolve())


@unittest.skipIf(TestClient is None, "fastapi dependency not installed")
class MediaUploadRouteTests(unittest.TestCase):
    def _client(self, tmp_dir: str) -> TestClient:
        app = FastAPI()
        app.include_router(media_route.router)
        self._patcher = patch.object(media_route, "MEDIA_UPLOAD_DIR", Path(tmp_dir))
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        return TestClient(app)

    def test_upload_image_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp)
            response = client.post(
                "/api/media/upload",
                files={"file": ("photo.jpg", b"binarydata", "image/jpeg")},
            )
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["mime"], "image/jpeg")
            self.assertEqual(body["filename"], "photo.jpg")
            self.assertEqual(body["size_bytes"], len(b"binarydata"))
            self.assertTrue(Path(body["path"]).is_file())
            self.assertTrue(body["path"].startswith(str(Path(tmp).resolve())))

    def test_missing_file_part_returns_400(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp)
            response = client.post("/api/media/upload", data={"not_file": "x"})
            self.assertEqual(response.status_code, 400)
            self.assertIn("error", response.json())

    def test_empty_file_returns_400(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp)
            response = client.post(
                "/api/media/upload",
                files={"file": ("empty.txt", b"", "text/plain")},
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json(), {"error": "Empty file"})

    def test_oversized_image_returns_413(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp)
            with patch.object(media_route, "save_upload") as fake_save:
                async def _raise(*_args, **_kwargs):
                    raise MediaUploadError(413, "File too large (max 20 MB for images)")
                fake_save.side_effect = _raise
                response = client.post(
                    "/api/media/upload",
                    files={"file": ("big.jpg", b"x", "image/jpeg")},
                )
            self.assertEqual(response.status_code, 413)
            self.assertEqual(
                response.json(), {"error": "File too large (max 20 MB for images)"}
            )

    def test_two_file_parts_both_accepted_named_field_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp)
            response = client.post(
                "/api/media/upload",
                files=[
                    ("file", ("cat.jpg", b"binarydata", "image/jpeg")),
                    ("extra", ("other.txt", b"hello", "text/plain")),
                ],
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["filename"], "cat.jpg")

    def test_too_many_file_parts_returns_400_not_500(self):
        # router allows max_files=2; a third file part must trigger Starlette's
        # MultiPartException, and the route must translate that to a 400 JSON
        # error rather than letting it surface as an unhandled 500.
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp)
            response = client.post(
                "/api/media/upload",
                files=[
                    ("file", ("a.jpg", b"1", "image/jpeg")),
                    ("b", ("b.jpg", b"1", "image/jpeg")),
                    ("c", ("c.jpg", b"1", "image/jpeg")),
                ],
            )
            self.assertEqual(response.status_code, 400)
            self.assertIn("error", response.json())


@unittest.skipIf(TestClient is None, "fastapi dependency not installed")
class ContentLengthFastRejectTests(unittest.TestCase):
    """routes.media.media_upload called directly against a bodyless Request:
    there's no `receive` callable wired up at all, so if the handler tried to
    read the body it would raise rather than silently succeeding -- proving
    the oversize case is rejected purely from the header."""

    def _request(self, content_length: str | None):
        from starlette.requests import Request as StarletteRequest

        headers = []
        if content_length is not None:
            headers.append((b"content-length", content_length.encode()))
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/media/upload",
            "headers": headers,
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("testclient", 1234),
            "scheme": "http",
        }
        return StarletteRequest(scope)

    def test_oversized_content_length_rejected_before_reading_body(self):
        oversize = media_route.FILE_MAX_BYTES + 2 * 1024 * 1024
        response = _run(media_route.media_upload(self._request(str(oversize))))
        self.assertEqual(response.status_code, 413)
        self.assertEqual(
            json.loads(response.body), {"error": "File too large (max 50 MB)"}
        )

    def test_content_length_within_cap_falls_through_to_normal_parsing(self):
        # Well under the fast-reject threshold, no body/content-type at all:
        # proves the gate doesn't fire here and control reaches the normal
        # "no file part" 400, not a fast reject.
        response = _run(media_route.media_upload(self._request(str(1000))))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body), {"error": "Missing file"})

    def test_missing_content_length_header_is_not_rejected(self):
        response = _run(media_route.media_upload(self._request(None)))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body), {"error": "Missing file"})


if __name__ == "__main__":
    unittest.main()
