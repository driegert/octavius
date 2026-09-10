"""models.json routing: precedence, validation, and the params passthrough.

The file exists because model routing previously lived in six separate
JSON-inside-a-systemd-EnvironmentFile variables. That made routing awkward to
hand-edit and let a stale alias sit unnoticed on lilripper:8020 until
2026-08-26, by which point every voice turn was silently failing over two hops
to the slowest model in the fleet.
"""

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import settings as settings_mod

CHAIN_VARS = [
    "OCTAVIUS_LLM_CHAIN",
    "OCTAVIUS_SUBAGENT_LLM_CHAIN",
    "OCTAVIUS_VISION_LLM_CHAIN",
    "OCTAVIUS_READER_LLM_URL",
    "OCTAVIUS_READER_LLM_MODEL",
    "OCTAVIUS_READER_LLM_FALLBACK_URL",
    "OCTAVIUS_READER_LLM_FALLBACK_MODEL",
    "OCTAVIUS_SUMMARY_URL",
    "OCTAVIUS_SUMMARY_MODEL",
]


class ModelsFileTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "models.json"
        # Reloading settings re-reads os.environ; clear routing vars so an
        # operator's own exports cannot leak in and look like a logic failure.
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for var in CHAIN_VARS:
            os.environ.pop(var, None)
        self.addCleanup(self._restore_module)

    def _restore_module(self):
        os.environ.pop("OCTAVIUS_MODELS_FILE", None)
        importlib.reload(settings_mod)

    def _load(self, doc=None):
        if doc is not None:
            self.path.write_text(json.dumps(doc) if not isinstance(doc, str) else doc)
        os.environ["OCTAVIUS_MODELS_FILE"] = str(self.path)
        mod = importlib.reload(settings_mod)
        return mod, mod.load_settings()

    def test_absent_file_falls_back_to_code_defaults(self):
        _, st = self._load()
        self.assertEqual(st.llm_chain[0]["url"], "http://lilripper:8010/v1/chat/completions")

    def test_file_overrides_code_defaults(self):
        _, st = self._load({"roles": {"main": [{"url": "http://h:1/v1", "model": "m"}]}})
        self.assertEqual(st.llm_chain, [{"url": "http://h:1/v1", "model": "m"}])

    def test_params_survive_into_the_chain_entry(self):
        _, st = self._load({
            "roles": {"main": [
                {"url": "http://h:1/v1", "model": "m",
                 "params": {"chat_template_kwargs": {"enable_thinking": False}}}
            ]}
        })
        self.assertEqual(
            st.llm_chain[0]["params"], {"chat_template_kwargs": {"enable_thinking": False}}
        )

    def test_environment_still_wins_over_the_file(self):
        """Env keeps precedence so tests and one-off overrides work unchanged."""
        override = json.dumps([{"url": "http://env:9/v1", "model": "from-env"}])
        with patch.dict(os.environ, {"OCTAVIUS_LLM_CHAIN": override}):
            _, st = self._load({"roles": {"main": [{"url": "http://file:1/v1", "model": "from-file"}]}})
        self.assertEqual(st.llm_chain[0]["model"], "from-env")

    def test_unlisted_role_keeps_its_code_default(self):
        _, st = self._load({"roles": {"main": [{"url": "http://h:1/v1", "model": "m"}]}})
        self.assertEqual(st.vision_llm_chain[0]["url"], "http://lilripper:8010/v1/chat/completions")

    def test_single_endpoint_roles_read_url_and_model(self):
        _, st = self._load({
            "roles": {
                "reader": [{"url": "http://r:1/v1", "model": "reader-m"}],
                "summary": [{"url": "http://s:1/v1", "model": "sum-m"}],
            }
        })
        self.assertEqual(st.reader.llm_url, "http://r:1/v1")
        self.assertEqual(st.reader.llm_model, "reader-m")
        self.assertEqual(st.summary_url, "http://s:1/v1")
        self.assertEqual(st.summary_model, "sum-m")

    def test_summary_fallback_takes_the_second_entry_model(self):
        """The two routers stopped sharing an alias, so the summary fallback
        needs its own model or every fallback summary 400s."""
        _, st = self._load({
            "roles": {"summary": [
                {"url": "http://s:1/v1", "model": "primary-m"},
                {"url": "http://s:2/v1", "model": "fallback-m"},
            ]}
        })
        self.assertEqual(st.summary_model, "primary-m")
        self.assertEqual(st.summary_fallback_model, "qwen3.8-27b")

    def test_reader_fallback_reads_the_second_entry(self):
        """The reader is single-endpoint per call, but the role's second entry
        is its failover target (reader_text.py tries it before strip_latex)."""
        _, st = self._load({
            "roles": {"reader": [
                {"url": "http://r:1/v1", "model": "primary-m"},
                {"url": "http://r:2/v1", "model": "fallback-m"},
            ]}
        })
        self.assertEqual(st.reader.llm_url, "http://r:1/v1")
        self.assertEqual(st.reader.llm_model, "primary-m")
        self.assertEqual(st.reader.llm_fallback_url, "http://r:2/v1")
        self.assertEqual(st.reader.llm_fallback_model, "fallback-m")

    def test_reader_single_entry_has_no_fallback(self):
        _, st = self._load({"roles": {"reader": [{"url": "http://r:1/v1", "model": "m"}]}})
        self.assertIsNone(st.reader.llm_fallback_url)
        self.assertIsNone(st.reader.llm_fallback_model)

    def test_reader_fallback_missing_model_is_rejected(self):
        """A half-configured fallback would 400 on every failover — refuse to
        start onto routing nobody asked for, same standard as the chains."""
        with self.assertRaises(ValueError):
            self._load({"roles": {"reader": [
                {"url": "http://r:1/v1", "model": "primary-m"},
                {"url": "http://r:2/v1"},
            ]}})

    def test_malformed_json_raises_rather_than_silently_defaulting(self):
        """Restarting onto routing the operator did not ask for is the exact
        class of bug this file was added to prevent."""
        with self.assertRaises(ValueError):
            self._load("{not json")

    def test_entry_without_url_is_rejected(self):
        with self.assertRaises(ValueError):
            self._load({"roles": {"main": [{"model": "m"}]}})

    def test_empty_role_list_is_rejected(self):
        with self.assertRaises(ValueError):
            self._load({"roles": {"main": []}})


if __name__ == "__main__":
    unittest.main()
