"""Offline checks for authentication boundaries, private state, and forum reads."""
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "openreview-author" / "scripts"))
import or_core as core


def token_for(identity="~Author_One1", expiry=None):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return ".".join((
        encode({"alg": "none"}),
        encode({"user": {"profile": {"id": identity}}, "exp": expiry or time.time() + 600}),
        "offline-test-signature",
    ))


class FakeClient:
    instances = []

    def __init__(self, baseurl):
        self.baseurl = baseurl
        self.headers = {}
        self.session = SimpleNamespace(adapters={})
        self.profile = None
        self.profile_calls = []
        self.notes = {
            "ForumOne": {"id": "ForumOne", "forum": "ForumOne", "content": {"title": {"value": "Example"}}},
            "ReviewOne": {"id": "ReviewOne", "forum": "ForumOne", "invitations": ["Venue/Submission1/-/Official_Review"], "cdate": 1},
            "MetaOne": {"id": "MetaOne", "forum": "ForumOne", "invitations": ["Venue/Submission1/-/Meta_Review"], "cdate": 2},
            "ResponseOne": {"id": "ResponseOne", "forum": "ForumOne", "invitations": ["Venue/Submission1/Official_Review/-/Author_Response"], "cdate": 3},
            "OtherReview": {"id": "OtherReview", "forum": "OtherForum"},
        }
        self.invitation_calls = []
        FakeClient.instances.append(self)

    def get_profile(self, identity):
        self.profile_calls.append(identity)
        return {"id": identity, "content": {}}

    def get_note(self, identity):
        return dict(self.notes[identity])

    def get_all_notes(self, forum):
        return [dict(n) for n in self.notes.values() if n.get("forum") == forum]

    def get_all_invitations(self, **filters):
        self.invitation_calls.append(filters)
        return [{"id": "Venue/Submission1/-/Author_Response", "edit": {"note": {"content": {"response": {"param": {"type": "string"}}}}}}]


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="openreview-author-offline-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"OR_AUTHOR_HOME": str(self.base / "state")}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.sdk = patch.object(core, "load_sdk", return_value=SimpleNamespace(api=SimpleNamespace(OpenReviewClient=FakeClient)))
        self.sdk.start()
        self.addCleanup(self.sdk.stop)
        FakeClient.instances.clear()

    def make_session(self, path, identity="~Author_One1", host=core.DEFAULT_BASEURL):
        core.write_private_json(path, {
            "schema_version": 1, "baseurl": host, "token": token_for(identity),
            "profile_id": identity, "created_at": time.time(), "expires_at": time.time() + 600,
        })
        return path

    def client(self):
        os.environ["OPENREVIEW_TOKEN"] = token_for()
        return core.get_client()

    def test_auth_is_validated_and_summary_does_not_expose_token(self):
        client = self.client()
        self.assertEqual(client.profile_calls, ["~Author_One1"])
        summary = core.whoami(client)
        self.assertTrue(summary["authenticated"])
        self.assertEqual(summary["profile_id"], "~Author_One1")
        self.assertNotIn(client.token, json.dumps(summary))
        os.environ["OPENREVIEW_TOKEN"] = token_for(expiry=time.time() - 10)
        with self.assertRaises(core.ToolError) as caught:
            core.get_client()
        self.assertEqual(caught.exception.code, "AUTH_EXPIRED")
        self.assertEqual(len(FakeClient.instances), 1)

    def test_explicit_session_overrides_environment_account_without_fallback(self):
        os.environ["OPENREVIEW_TOKEN"] = token_for("~Wrong_Account1")
        session = self.make_session(self.base / "selected.json", "~Selected_Author1")
        chosen = core.get_client(session_file=session)
        self.assertEqual(core.profile_id(chosen), "~Selected_Author1")
        with self.assertRaises(core.ToolError) as caught:
            core.get_client(session_file=self.base / "missing.json")
        self.assertEqual(caught.exception.code, "FILE_NOT_FOUND")
        self.assertEqual(len(FakeClient.instances), 1)

    def test_tokens_are_bound_to_official_hosts(self):
        os.environ["OPENREVIEW_TOKEN"] = token_for()
        with self.assertRaises(core.ToolError) as caught:
            core.get_client(baseurl="https://api2.dev.openreview.net")
        self.assertEqual(caught.exception.code, "SESSION_HOST_MISMATCH")
        session = self.make_session(self.base / "prod.json")
        with self.assertRaises(core.ToolError) as caught:
            core.get_client(baseurl="https://api2.dev.openreview.net", session_file=session)
        self.assertEqual(caught.exception.code, "SESSION_HOST_MISMATCH")
        with self.assertRaises(core.ToolError) as caught:
            core.get_client(baseurl="https://api2.openreview.net.attacker.example")
        self.assertEqual(caught.exception.code, "INVALID_BASEURL")
        self.assertEqual(len(FakeClient.instances), 0)

    def test_private_files_no_clobber_and_logout_only_removes_session(self):
        session = self.make_session(self.base / "session.json")
        self.assertEqual(stat.S_IMODE(session.stat().st_mode), 0o600)
        original = session.read_bytes()
        with self.assertRaises(core.ToolError) as caught:
            core.write_private_json(session, {"unexpected": True})
        self.assertEqual(caught.exception.code, "FILE_EXISTS")
        self.assertEqual(session.read_bytes(), original)
        session.chmod(0o644)
        with self.assertRaises(core.ToolError) as caught:
            core.get_client(session_file=session)
        self.assertEqual(caught.exception.code, "INSECURE_SESSION_FILE")
        session.chmod(0o600)
        unrelated = self.base / "response-draft.json"
        core.write_private_json(unrelated, {"response": "A local draft."})
        with self.assertRaises(core.ToolError) as caught:
            core.logout(session_file=unrelated)
        self.assertEqual(caught.exception.code, "INVALID_SESSION")
        self.assertTrue(unrelated.exists())
        self.assertTrue(core.logout(session_file=session)["removed"])
        self.assertFalse(session.exists())
        self.assertTrue(unrelated.exists())

    def test_raw_sdk_output_and_exception_credentials_do_not_escape(self):
        output, errors = io.StringIO(), io.StringIO()
        def failing_call():
            print("token SECRET")
            print("password PRIVATE", file=sys.stderr)
            raise TimeoutError("token SECRET password PRIVATE")
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            with self.assertRaises(core.ToolError) as caught:
                core.api_call(failing_call)
        self.assertEqual(caught.exception.code, "NETWORK_ERROR")
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")
        self.assertNotIn("SECRET", json.dumps(caught.exception.to_json()))
        self.assertNotIn("PRIVATE", json.dumps(caught.exception.to_json()))

    def test_meta_reviews_and_author_responses_are_not_ordinary_reviews(self):
        result = core.read_forum(self.client(), "https://openreview.net/forum?id=ForumOne")
        self.assertEqual([n["id"] for n in result["review_notes"]], ["ReviewOne"])
        self.assertEqual([n["id"] for n in result["meta_review_notes"]], ["MetaOne"])
        self.assertEqual(result["replies"][-1]["note_type"], "author_response")
        self.assertEqual(len(result["replies"]), 3)
        with self.assertRaises(core.ToolError):
            core.parse_note_id("https://openreview.net.attacker.example/forum?id=ForumOne")

    def test_invitation_discovery_tracks_evidence_without_claiming_submit_permission(self):
        client = self.client()
        result = core.list_reply_invitations(client, "ForumOne", replyto="ReviewOne")
        self.assertEqual(len(result["candidates"]), 1)
        queries = [{"replyForum": "ForumOne", "invitee": "~Author_One1"}, {"replyForum": "ForumOne"}]
        self.assertEqual(client.invitation_calls, queries)
        self.assertEqual(result["candidates"][0]["matched_by"], queries)
        self.assertEqual(result["replyto"], "ReviewOne")
        self.assertFalse(result["candidates"][0]["can_submit_verified"])
        with self.assertRaises(core.ToolError) as caught:
            core.list_reply_invitations(client, "ForumOne", replyto="OtherReview")
        self.assertEqual(caught.exception.code, "TARGET_MISMATCH")

    def test_login_without_terminal_does_not_prompt_or_start_network(self):
        with patch.object(sys.stdin, "isatty", return_value=False), patch.object(sys.stderr, "isatty", return_value=False):
            with self.assertRaises(core.ToolError) as caught:
                core.login(username="author@example.invalid")
        self.assertEqual(caught.exception.code, "INTERACTIVE_LOGIN_REQUIRED")
        self.assertEqual(FakeClient.instances, [])


if __name__ == "__main__":
    unittest.main()
