"""Offline checks for agent-facing error handling and side-effect boundaries."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "openreview-author" / "scripts"))
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

    def test_login_classifies_openreview_400_without_exposing_response(self):
        rejection = Exception({"status": 400, "message": "Invalid username or password (private diagnostic)"})
        client = SimpleNamespace(login_user=Mock(side_effect=rejection))
        credentials = {"OPENREVIEW_USERNAME": "author@example.invalid", "OPENREVIEW_PASSWORD": "local-secret"}
        with patch.dict(core.os.environ, credentials), patch.object(core, "_empty_client", return_value=client), patch.object(core, "_mfa_policy", return_value=contextlib.nullcontext()):
            code, data = self.invoke(["login"])
        self.assertEqual(code, 2)
        self.assertEqual(data["error"]["code"], "AUTH_REQUIRED")
        self.assertEqual(data["error"]["details"]["http_status"], 400)
        self.assertNotIn("private diagnostic", json.dumps(data))
        self.assertNotIn(credentials["OPENREVIEW_PASSWORD"], json.dumps(data))

    def test_untrusted_api_host_never_authenticates(self):
        with patch.object(core, "get_client") as auth:
            code, data = self.invoke(["--baseurl", "https://example.invalid", "whoami"])
        self.assertEqual(code, 2)
        self.assertEqual(data["error"]["code"], "INVALID_BASEURL")
        auth.assert_not_called()

    def test_public_pull_uses_a_client_without_authentication(self):
        client = object()
        with patch.object(core, "get_client") as auth, patch.object(core, "get_public_client", return_value=client):
            with patch.object(core, "read_forum", return_value={"review_notes": []}) as read:
                code, data = self.invoke(["pull", "--forum", "Forum123", "--public"])
        self.assertEqual(code, 0)
        self.assertEqual(data["data"], {"review_notes": []})
        read.assert_called_once_with(client, "Forum123")
        auth.assert_not_called()

    def test_doctor_distinguishes_managed_python_from_current_sdk(self):
        current_sdk = {"sdk_usable": True, "sdk_version": "current"}
        managed_sdk = {"sdk_usable": True, "sdk_version": "managed"}
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            python = state / "runtime" / "bin" / "python"
            with patch.object(core, "state_dir", return_value=state), patch.object(cli, "sdk_status", return_value=current_sdk):
                code, result = self.invoke(["doctor"])
                self.assertEqual(code, 0)
                self.assertEqual(result["data"]["python_executable"], sys.executable)
                self.assertIsNone(result["data"]["runtime_python"])
                self.assertFalse(result["data"]["managed_runtime"]["exists"])

                python.parent.mkdir(parents=True)
                python.touch()
                with patch.object(cli.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(managed_sdk))) as probe:
                    code, result = self.invoke(["doctor"])
                self.assertEqual(code, 0)
                self.assertEqual(probe.call_args.args[0][0], str(python))
                self.assertEqual(result["data"]["managed_runtime"]["sdk_version"], "managed")
                self.assertEqual(result["data"]["python_executable"], str(python))

    def test_doctor_recommends_setup_when_managed_executable_cannot_run(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            python = state / "runtime" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.touch(mode=0o600)
            with patch.object(core, "state_dir", return_value=state), patch.object(cli, "sdk_status", return_value={"sdk_usable": False}):
                code, result = self.invoke(["doctor"])
        self.assertEqual(code, 0)
        data = result["data"]
        self.assertTrue(data["managed_runtime"]["exists"])
        self.assertFalse(data["managed_runtime"]["runnable"])
        self.assertIsNone(data["python_executable"])
        self.assertEqual(data["next_action"], "setup")
        self.assertEqual(data["setup_command"][-1], "setup")


if __name__ == "__main__":
    unittest.main()
