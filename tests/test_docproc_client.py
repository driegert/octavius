import unittest
from unittest.mock import patch

import docproc_client


class _FakeResponse:
    def __init__(self, payload, status_ok=True):
        self._payload = payload
        self._status_ok = status_ok

    def raise_for_status(self):
        if not self._status_ok:
            raise RuntimeError("HTTP error")

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Mirrors the _FakeAsyncClient idiom in tests/test_service_clients.py."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.get_calls: list[tuple] = []
        self.post_calls: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None):
        self.post_calls.append((url, json))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def get(self, url, params=None):
        self.get_calls.append((url, params))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class SubmitJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_submit_job_posts_to_api_jobs(self):
        fake = _FakeAsyncClient([_FakeResponse({"id": "job-1", "status": "queued"})])
        with patch("docproc_client.httpx.AsyncClient", return_value=fake):
            job = await docproc_client.submit_job("/tmp/paper.pdf", mode="full", caller="octavius")

        self.assertEqual(job, {"id": "job-1", "status": "queued"})
        url, payload = fake.post_calls[0]
        self.assertTrue(url.endswith("/api/jobs"))
        self.assertEqual(payload, {"source_path": "/tmp/paper.pdf", "mode": "full", "caller": "octavius"})


class GetJobStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_job_status_returns_matching_row(self):
        fake = _FakeAsyncClient([_FakeResponse([{"id": "job-1", "status": "done", "result_md_path": "/x.md"}])])
        with patch("docproc_client.httpx.AsyncClient", return_value=fake):
            row = await docproc_client.get_job_status("job-1")

        self.assertEqual(row["status"], "done")
        url, params = fake.get_calls[0]
        self.assertTrue(url.endswith("/api/jobs/lookup"))
        self.assertEqual(params, {"ids": "job-1"})

    async def test_get_job_status_unknown_when_empty(self):
        fake = _FakeAsyncClient([_FakeResponse([])])
        with patch("docproc_client.httpx.AsyncClient", return_value=fake):
            row = await docproc_client.get_job_status("missing")

        self.assertEqual(row, {"id": "missing", "status": "unknown"})


class PollJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_poll_job_returns_on_done(self):
        fake = _FakeAsyncClient([
            _FakeResponse([{"id": "job-1", "status": "queued"}]),
            _FakeResponse([{"id": "job-1", "status": "running"}]),
            _FakeResponse([{"id": "job-1", "status": "done", "result_md_path": "/x.md"}]),
        ])
        with patch("docproc_client.httpx.AsyncClient", return_value=fake), \
             patch("docproc_client.asyncio.sleep", new=_instant_sleep):
            row = await docproc_client.poll_job("job-1", interval=0.001, timeout=5)

        self.assertEqual(row["status"], "done")
        self.assertEqual(row["result_md_path"], "/x.md")

    async def test_poll_job_raises_on_error_status(self):
        fake = _FakeAsyncClient([
            _FakeResponse([{"id": "job-1", "status": "error", "error_msg": "boom"}]),
        ])
        with patch("docproc_client.httpx.AsyncClient", return_value=fake):
            with self.assertRaises(docproc_client.DocprocError) as ctx:
                await docproc_client.poll_job("job-1", interval=0.001, timeout=5)
        self.assertIn("boom", str(ctx.exception))

    async def test_poll_job_raises_on_unknown(self):
        fake = _FakeAsyncClient([_FakeResponse([])])
        with patch("docproc_client.httpx.AsyncClient", return_value=fake):
            with self.assertRaises(docproc_client.DocprocError):
                await docproc_client.poll_job("job-1", interval=0.001, timeout=5)

    async def test_poll_job_times_out(self):
        # Every poll comes back 'running' — should hit the timeout path.
        responses = [_FakeResponse([{"id": "job-1", "status": "running"}]) for _ in range(50)]
        fake = _FakeAsyncClient(responses)
        with patch("docproc_client.httpx.AsyncClient", return_value=fake), \
             patch("docproc_client.asyncio.sleep", new=_instant_sleep), \
             patch("docproc_client.time.monotonic", side_effect=_fake_clock()):
            with self.assertRaises(docproc_client.DocprocError) as ctx:
                await docproc_client.poll_job("job-1", interval=1, timeout=5)
        self.assertIn("timed out", str(ctx.exception))


async def _instant_sleep(_seconds):
    return None


def _fake_clock():
    """Monotonic clock stub that jumps forward faster than the real timeout
    so the poll loop's deadline check fires on the first couple of iterations
    without the test actually sleeping."""
    state = {"t": 0.0}

    def _tick():
        state["t"] += 10.0
        return state["t"]

    return _tick


if __name__ == "__main__":
    unittest.main()
