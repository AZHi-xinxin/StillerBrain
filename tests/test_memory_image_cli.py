import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from runtime.memory_images import MAX_BYTES
from scripts.memory_images import (ImageMCPClient, ImageClientError, MAX_REQUEST_WIRE,
    MAX_RESPONSE_WIRE, endpoint, export_new_file, read_local_image, main)
from tests.test_memory_images import synthetic_image, maximum_png


class ImageLocalFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def receipt(self, data=None):
        data = synthetic_image() if data is None else data
        return {"decision": "read", "mime_type": "image/png", "byte_count": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "data_uri": "data:image/png;base64," + base64.b64encode(data).decode()}

    def test_selected_real_file_and_exclusive_download_preserve_exact_bytes(self):
        original = self.root / "chosen.png"; original.write_bytes(synthetic_image())
        data, mime = read_local_image(original)
        self.assertEqual(synthetic_image(), data); self.assertEqual("image/png", mime)
        destination = self.root / "new.png"
        digest = export_new_file(self.receipt(data), destination)
        self.assertEqual(data, destination.read_bytes())
        self.assertEqual(hashlib.sha256(data).hexdigest(), digest)
        with self.assertRaises(FileExistsError): export_new_file(self.receipt(), destination)
        self.assertEqual(data, destination.read_bytes())

    def test_invalid_hash_and_url_never_create_local_output(self):
        for changes in ({"sha256": "0" * 64}, {"data_uri": "https://example.invalid/image"}, {"byte_count": 1}):
            with self.assertRaises(ImageClientError): export_new_file({**self.receipt(), **changes}, self.root / "missing.png")
            self.assertFalse((self.root / "missing.png").exists())

    def test_exact_four_mib_wire_fits_both_mcp_result_copies(self):
        data = maximum_png(); receipt = self.receipt(data)
        self.assertEqual(MAX_BYTES, len(data))
        request = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "stbrain_manage",
            "arguments": {"action": "store_memory_image", "arguments": {"data_base64": receipt["data_uri"].split(",")[1],
            "description": "字" * 2000, "target_ref": "emotion://fixture@1", "mime_type": "image/png"}}}}
        wire = {"jsonrpc": "2.0", "result": {"structuredContent": receipt,
            "content": [{"type": "text", "text": json.dumps(receipt)}]}}
        self.assertLess(len(json.dumps(request).encode()), MAX_REQUEST_WIRE)
        self.assertLess(len(json.dumps(wire).encode()), MAX_RESPONSE_WIRE)
        destination = self.root / "maximum.png"
        export_new_file(receipt, destination)
        self.assertEqual(hashlib.sha256(data).hexdigest(), hashlib.sha256(destination.read_bytes()).hexdigest())

    def test_cancelled_local_action_contacts_nothing(self):
        original = self.root / "chosen.png"; original.write_bytes(synthetic_image())
        with patch("builtins.input", return_value="no"), patch("scripts.memory_images.ImageMCPClient", side_effect=AssertionError("contact forbidden")):
            self.assertEqual(0, main(["put", "--endpoint", "https://example.invalid/mcp", "--target-ref", "emotion://e1@1",
                                     "--file", str(original), "--description", "Synthetic picture"]))

    def test_endpoint_rejects_credentials_query_and_nonlocal_cleartext(self):
        for value in ("http://example.com/mcp", "https://user:secret@example.com/mcp", "https://example.com/mcp?token=x",
                      "file:///tmp/image", "http://169.254.169.254/"):
            with self.assertRaises(ImageClientError): endpoint(value)
        self.assertEqual("http://127.0.0.1:8000/mcp", endpoint("http://127.0.0.1:8000/mcp"))

    def test_mcp_client_never_redirects_retries_or_exposes_error_body(self):
        import httpx
        calls = []
        def redirect(request):
            calls.append(request)
            return httpx.Response(307, headers={"Location": "https://other.invalid/"}, text="PRIVATE_ERROR")
        client = ImageMCPClient("https://example.invalid/mcp", "synthetic-test-token", transport=httpx.MockTransport(redirect))
        self.addCleanup(client.close)
        with self.assertRaisesRegex(ImageClientError, "mcp_http_result_unknown"):
            client.initialize()
        self.assertEqual(1, len(calls))

    def test_business_rejection_and_removed_upload_are_not_cli_success(self):
        import contextlib
        import io
        from unittest.mock import MagicMock
        original = self.root / "chosen.png"; original.write_bytes(synthetic_image())
        for receipt in ({"decision": "reject", "reason_code": "module_one_required"},
                        {"decision": "reject", "reason_code": "PRIVATE_SECRET"},
                        {"decision": "removed", "active": False}):
            fake = MagicMock(); fake.action.return_value = receipt
            captured = io.StringIO()
            with patch("builtins.input", return_value="yes"), patch("getpass.getpass", return_value="synthetic-token"), \
                 patch("scripts.memory_images.ImageMCPClient", return_value=fake), contextlib.redirect_stderr(captured), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(1, main(["put", "--endpoint", "https://example.invalid/mcp", "--target-ref", "emotion://e1@1",
                    "--file", str(original), "--description", "Synthetic picture"]))
            self.assertNotIn("PRIVATE_SECRET", captured.getvalue())
            fake.close.assert_called_once()


if __name__ == "__main__": unittest.main()
