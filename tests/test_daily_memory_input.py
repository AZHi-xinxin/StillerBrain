"""Runtime-discovered daily input contracts; no DB, sockets or model calls."""
from __future__ import annotations

import copy
import unittest

from runtime.daily_memory_input import (
    DailyMemoryInputError, normalize_daily_memory_input, resolve_daily_memory_module,
)


class DailyMemoryInputTests(unittest.TestCase):
    def test_content_only_has_neutral_default_not_semantic_inference(self):
        for content in ("I am joyful", "Tomorrow I must do this", "I learned a fact", "  原文\n  "):
            result = normalize_daily_memory_input({"content": content})
            self.assertEqual(result.fields["module"], "emotional_memory")
            self.assertEqual(result.fields["kind"], "unclassified")
            self.assertEqual(result.fields["emotion"], "other")
            self.assertEqual(result.fields["importance"], 50)
            self.assertEqual(result.fields["content"], content)
            self.assertEqual(result.warnings, ())

    def test_auto_uses_metadata_and_matches_scope_resolver(self):
        for fields, module in (({}, "emotional_memory"), ({"kind": "moment"}, "emotional_memory"),
                               ({"kind": "fact"}, "learning_memory"), ({"kind": "TASK"}, "planning_memory"),
                               ({"due_at": "2030-01-01"}, "planning_memory"),
                               ({"parent_ref": "plan://synthetic@1"}, "planning_memory")):
            for automatic in (None, "", "auto", " AUTO "):
                arguments = {"module": automatic, "content": "exact body", **fields}
                self.assertEqual(resolve_daily_memory_module(arguments), module)
                self.assertEqual(normalize_daily_memory_input(arguments).fields["module"], module)

    def test_only_explicit_ordinary_module_aliases(self):
        for alias, module in ((" EMOTION ", "emotional_memory"), ("emotional", "emotional_memory"),
                              ("learning", "learning_memory"), ("learn", "learning_memory"),
                              ("plan", "planning_memory"), ("planning", "planning_memory")):
            self.assertEqual(resolve_daily_memory_module({"module": alias}), module)
        for name in ("self", "self_memory", "tool_guidance", "work_memory", "core", "learning_memroy",
                     "emotional_memory_module_two", "*", [], {}, True, 1):
            with self.subTest(module_type=type(name).__name__):
                with self.assertRaises(DailyMemoryInputError) as error:
                    resolve_daily_memory_module({"module": name, "kind": "moment"})
                self.assertEqual(error.exception.code, "invalid_module")

    def test_moment_alias_unknown_classification_and_nullable_fields(self):
        result = normalize_daily_memory_input({"kind": "moment", "emotion": None,
                                               "importance": None, "keywords": " "})
        self.assertEqual(result.fields["kind"], "shared_event")
        self.assertEqual(result.fields["emotion"], "other")
        self.assertIsNone(result.fields["keywords"])
        unknown = normalize_daily_memory_input({"kind": "synthetic-unknown-label", "emotion": "synthetic-emotion",
                                                "keywords": ["original"], "content": " original "})
        self.assertEqual(unknown.fields["kind"], "unclassified")
        self.assertEqual(unknown.fields["emotion"], "other")
        self.assertEqual(unknown.fields["keywords"], ["original"])
        self.assertEqual(unknown.fields["content"], " original ")
        self.assertEqual([item["code"] for item in unknown.warnings],
                         ["kind_unrecognized_defaulted", "emotion_unrecognized_defaulted"])
        self.assertNotIn("synthetic-", str(unknown.warnings))

    def test_importance_coercion_is_integral_bounded_and_never_confidence_coercion(self):
        for value, expected in ((None, 50), ("", 50), (" 0 ", 0), ("100", 100), (100.0, 100), (0, 0)):
            result = normalize_daily_memory_input({"importance": value, "confidence": "50"})
            self.assertEqual(result.fields["importance"], expected)
            self.assertIs(type(result.fields["importance"]), int)
            self.assertEqual(result.fields["confidence"], "50")
        for value in (True, False, -1, 101, 1.1, float("nan"), float("inf"), "50%", "1e2", "50.0", "9" * 4000):
            with self.assertRaises(DailyMemoryInputError):
                normalize_daily_memory_input({"importance": value})

    def test_keyword_compatibility_never_sorts_deduplicates_or_splits_array_elements(self):
        for value, expected in (("甲,乙；丙\n丁", list("甲乙丙丁")),
                                ('["甲,乙", "甲,乙"]', ["甲,乙", "甲,乙"]),
                                (["a,b", "a,b"], ["a,b", "a,b"]), ("[]", []), (None, None)):
            self.assertEqual(normalize_daily_memory_input({"keywords": value}).fields["keywords"], expected)
        for value in ("{" + '"key":1}', "[" * 3000 + "]" * 3000, "[invalid", "x" * 32769):
            with self.assertRaises(DailyMemoryInputError):
                normalize_daily_memory_input({"keywords": value})

    def test_receipt_fields_are_not_normalized_even_when_null_blank_or_aliases(self):
        for value in (None, "", "moment", " JOY ", "50", ["a", "a"]):
            original = {"module": "emotional_memory", "rewrite_receipt": "synthetic-receipt",
                        "content": " exact body ", "kind": value, "emotion": value,
                        "importance": value, "keywords": value}
            self.assertEqual(normalize_daily_memory_input(original).fields, original)
        for module in (None, "auto", "emotion", "EMOTIONAL_MEMORY"):
            with self.assertRaises(DailyMemoryInputError):
                normalize_daily_memory_input({"module": module, "rewrite_receipt": "synthetic-receipt"})

    def test_input_is_not_mutated_and_security_fields_are_not_repaired(self):
        original = {"module": "auto", "content": " original ", "kind": "moment", "keywords": ["original"],
                    "source_basis": "obeserved", "track": "interal", "confidence": "100",
                    "write_context_ref": "$latest", "arbitrary_security_flag": True}
        before = copy.deepcopy(original)
        result = normalize_daily_memory_input(original)
        self.assertEqual(original, before)
        for field in ("source_basis", "track", "confidence", "write_context_ref", "arbitrary_security_flag"):
            self.assertEqual(result.fields[field], original[field])

    def test_blank_optional_display_fields_are_not_normalized_with_receipt(self):
        fields = {"module": "emotional_memory", "title": " ", "summary": "\t", "reason": "\n",
                  "content": "  keep body  ", "timezone": " ", "source_basis": " ", "track": " "}
        normalized = normalize_daily_memory_input(fields).fields
        for field in ("title", "summary", "reason"):
            self.assertIsNone(normalized[field])
        for field in ("content", "timezone", "source_basis", "track"):
            self.assertEqual(normalized[field], fields[field])
        frozen = {**fields, "rewrite_receipt": "synthetic-receipt"}
        self.assertEqual(normalize_daily_memory_input(frozen).fields, frozen)


if __name__ == "__main__":
    unittest.main()
