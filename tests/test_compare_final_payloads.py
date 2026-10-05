"""Only synthetic bodies and test-owned temporary files; no gateway, model, user data or network."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "compare_final_payloads.py"
SPEC = importlib.util.spec_from_file_location("offline_payload_comparison", SCRIPT)
comparison = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(comparison)


def body(content="synthetic", **fields):
    return json.dumps({"messages": [{"role": "user", "content": content}], **fields},
                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class FinalPayloadComparisonTests(unittest.TestCase):
    def test_unicode_codepoints_are_not_utf8_bytes_or_tokens(self):
        a = body("合成🙂e\u0301")
        result = comparison.compare_bytes(a, a)
        self.assertEqual(len(a), result["raw_bodies"]["left"]["utf8_bytes"])
        self.assertEqual(len(a.decode("utf-8")), result["raw_bodies"]["left"]["unicode_codepoints"])
        self.assertNotEqual(result["raw_bodies"]["left"]["utf8_bytes"], result["raw_bodies"]["left"]["unicode_codepoints"])
        self.assertIsNone(result["raw_comparison"]["first_difference_utf8_byte"])
        self.assertFalse(result["provider_token_prefix_verified"])
        self.assertFalse(result["provider_cache_persistence_verified"])

    def test_first_difference_uses_original_utf8_zero_based_position(self):
        a, b = body("合成甲"), body("合成乙")
        expected = next(index for index, pair in enumerate(zip(a, b)) if pair[0] != pair[1])
        result = comparison.compare_bytes(a, b)
        self.assertEqual(expected, result["raw_comparison"]["first_difference_utf8_byte"])
        self.assertEqual(expected, result["raw_comparison"]["common_prefix_utf8_bytes"])
        self.assertEqual(0, result["message_comparison"]["first_difference_message_index"])

    def test_strict_raw_prefix_uses_end_offset_not_null(self):
        a = body()
        result = comparison.compare_bytes(a, a + b"\n")
        self.assertEqual(len(a), result["raw_comparison"]["first_difference_utf8_byte"])
        self.assertTrue(result["message_comparison"]["identical"])

    def test_key_order_is_structural_only_and_arrays_remain_ordered(self):
        a = b'{"messages":[{"role":"user","content":"synthetic"}],"temperature":1}'
        b = b'{"temperature":1,"messages":[{"content":"synthetic","role":"user"}]}'
        result = comparison.compare_bytes(a, b)
        self.assertFalse(result["raw_comparison"]["identical"])
        self.assertTrue(result["message_comparison"]["identical"])
        self.assertTrue(result["known_fields"]["temperature"]["equal"])
        x = body(tools=[{"name": "a"}, {"name": "b"}])
        y = body(tools=[{"name": "b"}, {"name": "a"}])
        self.assertFalse(comparison.compare_bytes(x, y)["known_fields"]["tools"]["equal"])

    def test_different_json_types_are_not_equated(self):
        self.assertFalse(comparison.compare_bytes(body(temperature=True), body(temperature=1))["known_fields"]["temperature"]["equal"])
        missing = comparison.compare_bytes(body(), body(model=None))["known_fields"]["model"]
        self.assertFalse(missing["left_present"]); self.assertTrue(missing["right_present"]); self.assertFalse(missing["equal"])

    def test_dynamic_tail_changes_do_not_change_already_equal_message_count(self):
        prefix = [{"role": "system", "content": "synthetic stable"}, {"role": "user", "content": "synthetic question"}]
        a = json.dumps({"messages": prefix + [{"role": "system", "content": "synthetic dynamic"}]}).encode()
        b = json.dumps({"messages": prefix}).encode()
        result = comparison.compare_bytes(a, b)
        self.assertEqual(2, result["message_comparison"]["common_prefix_messages"])
        self.assertEqual(2, result["message_comparison"]["first_difference_message_index"])
        self.assertEqual("system", result["message_comparison"]["left"]["last_role"])
        self.assertEqual("user", result["message_comparison"]["right"]["last_role"])
        self.assertFalse(result["provider_cache_persistence_verified"])

    def test_private_values_arbitrary_keys_roles_and_digests_never_appear(self):
        marker = "PRIVATE_SENTINEL_NOT_FOR_OUTPUT"
        raw = json.dumps({"messages": [{"role": marker, "content": marker}],
                          "model": marker, "tools": [{"private": marker}], marker: marker}).encode()
        text = json.dumps(comparison.compare_bytes(raw, raw))
        self.assertNotIn(marker, text)
        self.assertNotIn("digest", text); self.assertNotIn("sha256", text)
        self.assertEqual(1, comparison.compare_bytes(raw, raw)["message_comparison"]["left"]["role_counts"]["other"])

    def test_duplicate_keys_invalid_utf8_and_nonfinite_numbers_fail_closed(self):
        cases = [b'{"messages":[],"model":1,"model":2}',
                 b'{"messages":[{"role":"user","role":"tool"}]}', b'\xff',
                 b'{"messages":[],"x":NaN}', b'{"messages":[],"x":Infinity}', b'{"messages":[],"x":1e9999}']
        for value in cases:
            with self.subTest(value_type=cases.index(value)):
                with self.assertRaises(comparison.ComparisonError):
                    comparison.compare_bytes(value, body())

    def test_size_depth_message_limits_and_wrong_shape_are_explicit(self):
        with patch.object(comparison, "MAX_BODY_BYTES", 10):
            with self.assertRaisesRegex(comparison.ComparisonError, "^body_too_large$"):
                comparison.compare_bytes(body(), body())
        with patch.object(comparison, "MAX_DEPTH", 2):
            with self.assertRaisesRegex(comparison.ComparisonError, "^json_too_deep$"):
                comparison.compare_bytes(body(), body())
        with patch.object(comparison, "MAX_MESSAGES", 0):
            with self.assertRaisesRegex(comparison.ComparisonError, "^too_many_messages$"):
                comparison.compare_bytes(body(), body())
        for value in (b'[]', b'{}', b'{"messages":[1]}', b'{"messages":[{"content":"synthetic"}]}'):
            with self.assertRaises(comparison.ComparisonError):
                comparison.compare_bytes(value, body())

    def test_cli_reads_only_selected_files_and_leaves_source_bytes_unchanged(self):
        with tempfile.TemporaryDirectory(prefix="stbrain-synthetic-payload-") as directory:
            a, b = Path(directory) / "a.json", Path(directory) / "b.json"
            a.write_bytes(body("synthetic one")); b.write_bytes(body("synthetic two"))
            before = (a.read_bytes(), b.read_bytes(), sorted(path.name for path in Path(directory).iterdir()))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = comparison.main([str(a), str(b)])
            self.assertEqual(0, code); self.assertTrue(json.loads(output.getvalue())["ok"])
            self.assertNotIn(str(a), output.getvalue()); self.assertNotIn("synthetic one", output.getvalue())
            self.assertEqual(before, (a.read_bytes(), b.read_bytes(), sorted(path.name for path in Path(directory).iterdir())))

    def test_cli_failures_never_print_paths_or_decoder_source_fragments(self):
        marker = "PRIVATE_PATH_AND_TEXT_SENTINEL"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(2, comparison.main([marker]))
            self.assertEqual(2, comparison.main([marker, marker]))
        self.assertNotIn(marker, output.getvalue()); self.assertNotIn("Traceback", output.getvalue())
        with tempfile.TemporaryDirectory(prefix="stbrain-synthetic-payload-") as directory:
            path = Path(directory) / "synthetic.json"
            path.write_bytes((marker + "{invalid}").encode())
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(2, comparison.main([str(path), str(path)]))
            self.assertEqual({"ok": False, "error": "invalid_json"}, json.loads(output.getvalue()))

    def test_nonlocal_and_device_inputs_are_rejected_before_read(self):
        for name in ("https://example.invalid/request.json", "\\\\server\\share\\request.json", "CON", "NUL.txt", "COM1", "a\x00b"):
            with self.assertRaisesRegex(comparison.ComparisonError, "^local_regular_file_required$"):
                comparison._read_local_file(name)

    def test_source_change_during_open_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="stbrain-synthetic-payload-") as directory:
            path = Path(directory) / "synthetic.json"; path.write_bytes(body())
            opened = path.stat()
            class Changed:
                st_size = opened.st_size + 1
                st_mtime_ns = opened.st_mtime_ns
                st_ino = opened.st_ino
            with patch.object(comparison.os, "fstat", side_effect=[opened, Changed()]):
                with self.assertRaisesRegex(comparison.ComparisonError, "^source_changed$"):
                    comparison._read_local_file(str(path))


if __name__ == "__main__":
    unittest.main()
