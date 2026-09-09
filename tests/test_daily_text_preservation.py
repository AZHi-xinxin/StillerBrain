"""Synthetic-only original-text preservation; no service, credentials or live DB."""
from __future__ import annotations

from contextlib import closing
import hashlib
import inspect
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from runtime.emotional_memory import EmotionalMemoryError, EmotionalMemoryStore
from runtime.learning_memory import LearningLimits, LearningMemoryError, LearningMemoryStore


class DailyTextPreservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="daily-text-synthetic-")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name).resolve()
        original_connect = sqlite3.connect

        def connect(database, *args, **kwargs):
            # Every SQL access in this suite must stay in its new synthetic root.
            if kwargs.get("uri") or not Path(database).resolve().is_relative_to(self.root):
                raise AssertionError("only owned synthetic databases are allowed")
            return original_connect(database, *args, **kwargs)

        for guard in (
            patch("sqlite3.connect", side_effect=connect),
            patch("socket.socket.connect", side_effect=AssertionError("network prohibited")),
            patch("socket.create_connection", side_effect=AssertionError("network prohibited")),
            patch("subprocess.Popen", side_effect=AssertionError("subprocess prohibited")),
        ):
            guard.start()
            self.addCleanup(guard.stop)
        self.learning_db = self.root / "learning.sqlite"
        self.emotional_db = self.root / "emotional.sqlite"
        self.learning = LearningMemoryStore(
            self.learning_db, idea_database=self.root / "ideas.sqlite",
        )
        self.emotional = EmotionalMemoryStore(self.emotional_db)
        self.owner = "owner:daily-synthetic"
        self.model = "model:daily-synthetic"

    def learning_fields(self, text):
        return {
            "kind": "lesson", "title": "  Synthetic title  ",
            "summary": "  Synthetic summary  ", "current_understanding": text,
            "epistemic_status": "reported", "confidence": 50,
        }

    def remember_learning(self, text, **options):
        return self.learning.remember(
            owner_id=self.owner, model_id=self.model, wake_id="wake:synthetic",
            wake_seq=1, expected_row_version=0,
            correctness_assessment="Synthetic source assessment",
            reason="Synthetic preservation test", **options, **self.learning_fields(text),
        )

    def remember_emotional(self, text, **options):
        return self.emotional.remember(
            owner_id=self.owner, model_id=self.model, wake_id="wake:synthetic",
            expected_row_version=0, memory_type="shared_event", original_text=text,
            summary="  Synthetic summary  ", primary_emotion="joy",
            reason="Synthetic preservation test", **options,
        )

    def rows(self, database, statement):
        with closing(sqlite3.connect(database)) as connection:
            return connection.execute(statement).fetchall()

    def assert_no_memories(self):
        for database, table in (
            (self.learning_db, "learning_items"),
            (self.learning_db, "learning_versions"),
            (self.emotional_db, "emotion_memories"),
            (self.emotional_db, "emotion_memory_versions"),
        ):
            self.assertEqual([(0,)], self.rows(database, f"SELECT COUNT(*) FROM {table}"))

    def test_learning_preserves_unicode_whitespace_and_final_content_hash(self):
        text = " \t\r\n合成原文 e\u0301 / é / 🧪\u3000\n "
        self.remember_learning(text, preserve_original_text=True)
        [(raw, digest)] = self.rows(self.learning_db, "SELECT current_json, current_hash FROM learning_items")
        content = json.loads(raw)
        self.assertEqual(text, content["current_understanding"])
        self.assertEqual(text.encode("utf-8"), content["current_understanding"].encode("utf-8"))
        self.assertEqual("Synthetic title", content["title"])
        self.assertEqual("Synthetic summary", content["summary"])
        self.assertEqual(hashlib.sha256(raw.encode("utf-8")).hexdigest(), digest)
        self.assertEqual([(raw, digest)], self.rows(self.learning_db, "SELECT mutable_json, mutable_hash FROM learning_versions"))
        trimmed = {**content, "current_understanding": text.strip()}
        trimmed_raw = json.dumps(trimmed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.assertNotEqual(hashlib.sha256(trimmed_raw.encode("utf-8")).hexdigest(), digest)

    def test_emotional_preserves_unicode_whitespace_and_original_hash(self):
        text = "\u3000\tSynthetic e\u0301 / é / 合成🧪\r\n "
        result = self.remember_emotional(text, preserve_original_text=True)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self.assertEqual([(text, digest, "Synthetic summary")], self.rows(
            self.emotional_db, "SELECT original_text, original_hash, summary FROM emotion_memories",
        ))
        self.assertEqual(text, result["memory"]["original_text"])
        self.assertEqual(digest, result["memory"]["original_hash"])
        [(audit,)] = self.rows(self.emotional_db, "SELECT details_json FROM emotion_audit_events WHERE action='remember'")
        self.assertEqual(digest, json.loads(audit)["original_hash"])

    def test_learning_original_length_limit_counts_outer_whitespace(self):
        text = " " + "合" * self.learning.limits.understanding + " "
        with self.assertRaisesRegex(LearningMemoryError, "^current_understanding_too_long$"):
            self.remember_learning(text, preserve_original_text=True)
        self.assert_no_memories()

    def test_emotional_original_length_limit_counts_outer_whitespace(self):
        text = " " + "合" * self.emotional.limits.original_chars + " "
        with self.assertRaisesRegex(EmotionalMemoryError, "^original_text_too_long$"):
            self.remember_emotional(text, preserve_original_text=True)
        self.assert_no_memories()

    def test_exact_raw_length_boundary_is_accepted_without_trimming(self):
        learning_text = " " + "合" * (self.learning.limits.understanding - 2) + "\n"
        emotional_text = "\t" + "合" * (self.emotional.limits.original_chars - 2) + " "
        self.remember_learning(learning_text, preserve_original_text=True)
        self.remember_emotional(emotional_text, preserve_original_text=True)
        [(raw,)] = self.rows(self.learning_db, "SELECT current_json FROM learning_items")
        self.assertEqual(learning_text, json.loads(raw)["current_understanding"])
        self.assertEqual([(emotional_text,)], self.rows(self.emotional_db, "SELECT original_text FROM emotion_memories"))

    def test_learning_total_json_limit_uses_preserved_not_trimmed_content(self):
        text = " \tSynthetic original\r\n "
        normalized = self.learning._validate_content(self.learning_fields(text))
        limit = len(json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        self.learning.limits = LearningLimits(total_json=limit)
        with self.assertRaisesRegex(LearningMemoryError, "^learning_content_too_large$"):
            self.remember_learning(text, preserve_original_text=True)
        self.assert_no_memories()

    def test_whitespace_only_empty_and_nonstring_remain_rejected(self):
        for text in ("", " \t\r\n\u3000", " " * 2001, None, False, 0, []):
            with self.subTest(text_type=type(text).__name__), self.assertRaisesRegex(
                LearningMemoryError, "^current_understanding_required$",
            ):
                self.remember_learning(text, preserve_original_text=True)
            with self.subTest(text_type=type(text).__name__), self.assertRaisesRegex(
                EmotionalMemoryError, "^original_text_required$",
            ):
                self.remember_emotional(text, preserve_original_text=True)
        self.assert_no_memories()

    def test_legacy_default_still_trims_before_length_validation(self):
        learning_text = " " + "合" * self.learning.limits.understanding + " "
        emotional_text = " " + "合" * self.emotional.limits.original_chars + " "
        self.remember_learning(learning_text)
        self.remember_emotional(emotional_text)
        [(raw,)] = self.rows(self.learning_db, "SELECT current_json FROM learning_items")
        self.assertEqual(learning_text.strip(), json.loads(raw)["current_understanding"])
        self.assertEqual([(emotional_text.strip(),)], self.rows(self.emotional_db, "SELECT original_text FROM emotion_memories"))

    def test_explicit_false_is_legacy_and_flag_is_keyword_only(self):
        text = " \tSynthetic original\n "
        self.remember_learning(text, preserve_original_text=False)
        result = self.remember_emotional(text, preserve_original_text=False)
        [(raw,)] = self.rows(self.learning_db, "SELECT current_json FROM learning_items")
        self.assertEqual(text.strip(), json.loads(raw)["current_understanding"])
        self.assertEqual(text.strip(), result["memory"]["original_text"])
        for method in (LearningMemoryStore.remember, EmotionalMemoryStore.remember):
            parameter = inspect.signature(method).parameters["preserve_original_text"]
            self.assertEqual(inspect.Parameter.KEYWORD_ONLY, parameter.kind)
            self.assertIs(parameter.default, False)

    def test_nonboolean_opt_in_is_rejected(self):
        for flag in (None, 0, 1, "true", [], {}):
            for remember, error in ((self.remember_learning, LearningMemoryError), (self.remember_emotional, EmotionalMemoryError)):
                with self.subTest(flag_type=type(flag).__name__), self.assertRaisesRegex(error, "^preserve_original_text_invalid$"):
                    remember("Synthetic original", preserve_original_text=flag)
        self.assert_no_memories()

    def test_preservation_does_not_bypass_secret_validation(self):
        text = " \tapi_key=synthetic-noncredential-fixture\n "
        for remember, error in ((self.remember_learning, LearningMemoryError), (self.remember_emotional, EmotionalMemoryError)):
            with self.assertRaisesRegex(error, "^credential_or_secret_detected$"):
                remember(text, preserve_original_text=True)
        self.assert_no_memories()

    def test_preservation_retains_transactional_row_version_conflicts(self):
        self.remember_learning(" First synthetic original ", preserve_original_text=True)
        self.remember_emotional(" First synthetic original ", preserve_original_text=True)
        with self.assertRaisesRegex(LearningMemoryError, "^learning_row_version_conflict$"):
            self.remember_learning(" Second synthetic original ", preserve_original_text=True)
        with self.assertRaisesRegex(EmotionalMemoryError, "^emotion_row_version_conflict$"):
            self.remember_emotional(" Second synthetic original ", preserve_original_text=True)
        for database, table in (
            (self.learning_db, "learning_items"), (self.learning_db, "learning_versions"),
            (self.emotional_db, "emotion_memories"), (self.emotional_db, "emotion_memory_versions"),
        ):
            self.assertEqual([(1,)], self.rows(database, f"SELECT COUNT(*) FROM {table}"))


if __name__ == "__main__":
    unittest.main()
