"""Offline checks for agent-facing error handling and side-effect boundaries."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import openreview_tool as cli
import or_core as core


class CommandTests(unittest.TestCase):
    def invoke(self, argv):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = cli.main(argv)
        return code, json.loads(stream.getvalue())

    def test_missing_text_field_never_authenticates(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "reply.md"
            source.write_text("Approved text", encoding="utf-8")
            with patch.object(core, "get_client") as auth:
                code, data = self.invoke(["prepare", "--forum", "Forum123", "--invitation", "Venue/-/Reply", "--text-file", str(source)])
            self.assertEqual(code, 2)
            self.assertEqual(data["error"]["code"], "FIELD_REQUIRED")
            auth.assert_not_called()

    def test_existing_output_blocks_remote_operation(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "receipt.json"
            output.write_text("existing", encoding="utf-8")
            with patch.object(cli, "run") as action:
                code, data = self.invoke(["--output", str(output), "submit", "--draft", "d", "--confirm", "c"])
            self.assertEqual(code, 2)
            self.assertEqual(data["error"]["code"], "FILE_EXISTS")
            action.assert_not_called()
            self.assertEqual(output.read_text(), "existing")

    def test_completed_write_receipt_survives_output_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            receipt = {"status": "verified", "note_id": "SavedNote"}
            with patch.object(cli, "run", return_value=receipt), patch.object(core, "write_private_json", side_effect=core.ToolError("LOCAL_IO_ERROR", "Cannot write result.")):
                code, data = self.invoke(["--output", str(Path(folder)/"receipt.json"), "submit", "--draft", "d", "--confirm", "c"])
            self.assertEqual(code, 0)
            self.assertTrue(data["ok"])
            self.assertEqual(data["data"], receipt)
            self.assertIn("output_error", data)

    def test_sdk_exception_cannot_expose_credentials(self):
        secret = "private-session-token-example"
        with patch.object(cli, "run", side_effect=RuntimeError("password=private-pass Authorization: Bearer " + secret)):
            code, data = self.invoke(["whoami"])
        self.assertEqual(code, 2)
        self.assertNotIn(secret, json.dumps(data))
        self.assertNotIn("private-pass", json.dumps(data))

    def test_untrusted_api_host_never_authenticates(self):
        with patch.object(core, "get_client") as auth:
            code, data = self.invoke(["--baseurl", "https://example.invalid", "whoami"])
        self.assertEqual(code, 2)
        self.assertEqual(data["error"]["code"], "INVALID_BASEURL")
        auth.assert_not_called()


if __name__ == "__main__":
    unittest.main()
