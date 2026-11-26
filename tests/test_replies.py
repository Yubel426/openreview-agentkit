"""Offline tests for approval, target and exactly-once failure boundaries."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import or_replies as replies
from or_core import ToolError


class Entity:
    def __init__(self, **data):
        self.data = copy.deepcopy(data)
        self.__dict__.update(data)

    def to_json(self):
        return copy.deepcopy(self.data)


class FakeClient:
    def __init__(self):
        self.baseurl = "https://api2.openreview.net"
        self.token = "offline-fake-token-not-a-credential"
        self.profile = Entity(id="~Alice_Author1")
        self._or_author_verified_profile_id = self.profile.id
        self.user = {"id": "alice@example.test"}
        self.invitation_id = "Test.cc/2026/Conference/Submission7/-/Rebuttal"
        self.authors = "Test.cc/2026/Conference/Submission7/Authors"
        self.chairs = "Test.cc/2026/Conference/Program_Chairs"
        self.invitation = {
            "id": self.invitation_id, "invitees": [self.authors],
            "cdate": 1, "expdate": 9999999999999,
            "edit": {
                "signatures": {"param": {"enum": [self.authors]}},
                "readers": [self.chairs, "${2/signatures}"],
                "writers": [self.chairs],
                "note": {
                    "id": {"param": {"withInvitation": self.invitation_id, "optional": True}},
                    "forum": "Paper00001",
                    "replyto": {"param": {"withForum": "Paper00001"}},
                    "signatures": ["${3/signatures}"],
                    "readers": [self.chairs, self.authors],
                    "writers": [self.chairs, "${3/signatures}"],
                    "content": {
                        "title": {"value": "Author response"},
                        "comment": {"value": {"param": {"type": "string", "maxLength": 5000}}},
                        "pdf": {"value": {"param": {"type": "file", "maxSize": 5, "extensions": ["pdf"], "optional": True}}},
                    },
                },
            },
        }
        self.notes = {
            "Paper00001": {"id": "Paper00001", "forum": "Paper00001", "content": {"title": {"value": "Test paper"}, "authorids": {"value": [self.profile.id]}}, "writers": [self.authors], "readers": [self.chairs, self.authors]},
            "Review0001": {"id": "Review0001", "forum": "Paper00001", "replyto": "Paper00001", "content": {"review": {"value": "Please explain the ablation."}}},
        }
        self.post_count = 0
        self.upload_count = 0
        self.uploads = {}
        self.raise_after_post = False
        self.fail_readback = False
        self.wrong_readback = False

    def get_profile(self, profile_id):
        return Entity(id=profile_id, content={"emailsConfirmed": ["alice@example.test"]})

    def get_group(self, group_id):
        if group_id == self.authors:
            return Entity(id=group_id, members=[self.profile.id])
        return Entity(id=group_id, members=[])

    def get_note(self, note_id):
        if self.fail_readback and note_id.startswith("Created"):
            raise TimeoutError()
        return Entity(**self.notes[note_id])

    def get_all_notes(self, forum):
        return [Entity(**n) for n in self.notes.values() if n.get("forum") == forum]

    def get_invitation(self, invitation_id):
        if invitation_id != self.invitation_id:
            raise KeyError(invitation_id)
        return Entity(**self.invitation)

    def put_attachment(self, path, invitation, name):
        self.upload_count += 1
        url = "/attachment/" + str(self.upload_count) + ".pdf"
        self.uploads[url] = Path(path).read_bytes()
        return url

    def get_attachment(self, field_name, id):
        return self.uploads[self.notes[id]["content"][field_name]["value"]]

    def post_note_edit(self, invitation, signatures, note, **kwargs):
        self.post_count += 1
        data = note.to_json()
        note_id = data.get("id") or "Created001"
        existing = self.notes.get(note_id, {})
        merged = {**existing, **data}
        merged["content"] = {**existing.get("content", {}), **data.get("content", {})}
        merged["id"] = note_id
        merged["invitations"] = [invitation]
        if self.wrong_readback:
            merged["content"]["comment"]["value"] = "Unexpected server modification"
        self.notes[note_id] = merged
        if self.raise_after_post:
            raise TimeoutError("response lost after committed write")
        return {"id": "Edit000001", "note": {"id": note_id}}


class ReplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"OR_AUTHOR_HOME": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.sdk = patch.object(replies, "load_sdk", return_value=types.SimpleNamespace(api=types.SimpleNamespace(Note=Entity)))
        self.sdk.start()
        self.addCleanup(self.sdk.stop)
        self.client = FakeClient()

    def prepare(self, **kwargs):
        return replies.prepare_reply(self.client, "Paper00001", self.client.invitation_id,
                                     kwargs.pop("content", {"comment": {"value": "Here is the approved response."}}),
                                     replyto=kwargs.pop("replyto", "Review0001"), **kwargs)

    def submit(self, draft):
        return replies.submit_reply(self.client, draft["draft_path"], draft["confirmation"])

    def assert_code(self, code, fn):
        with self.assertRaises(ToolError) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)

    def test_prepare_preview_are_read_only_and_private(self):
        draft = self.prepare()
        preview = replies.preview_reply(draft["draft_id"])
        self.assertEqual(preview["payload"]["note"]["signatures"], [self.client.authors])
        self.assertEqual(preview["payload"]["readers"], [self.client.chairs, self.client.authors])
        self.assertEqual(preview["payload"]["note"]["content"]["title"]["value"], "Author response")
        self.assertEqual(os.stat(draft["draft_path"]).st_mode & 0o777, 0o600)
        self.assertEqual((self.client.post_count, self.client.upload_count), (0, 0))

    def test_confirmation_must_bind_exact_draft(self):
        draft = self.prepare()
        self.assert_code("confirmation_mismatch", lambda: replies.submit_reply(self.client, draft["draft_path"], "yes"))
        self.assertEqual(self.client.post_count, 0)

    def test_changed_body_cannot_use_old_confirmation(self):
        draft = self.prepare()
        path = Path(draft["draft_path"])
        data = json.loads(path.read_text())
        data["prepared"]["payload"]["note"]["content"]["comment"]["value"] = "tampered"
        path.write_text(json.dumps(data))
        self.assert_code("draft_changed", lambda: self.submit(draft))
        self.assertEqual(self.client.post_count, 0)

    def test_success_and_repeated_submit_post_only_once(self):
        draft = self.prepare()
        first = self.submit(draft)
        second = self.submit(draft)
        self.assertEqual(first["status"], "verified")
        self.assertTrue(second["already_submitted"])
        self.assertEqual(self.client.post_count, 1)

    def test_different_draft_same_reply_reuses_existing_note(self):
        one, two = self.prepare(), self.prepare()
        self.submit(one)
        result = self.submit(two)
        self.assertEqual(result["status"], "verified")
        self.assertTrue(result["reused_existing"])
        self.assertEqual(self.client.post_count, 1)

    def test_closed_invitation_blocks_all_writes(self):
        draft = self.prepare()
        self.client.invitation["expdate"] = 2
        self.assert_code("invitation_closed", lambda: self.submit(draft))
        self.assertEqual((self.client.post_count, self.client.upload_count), (0, 0))

    def test_changed_review_blocks_submission(self):
        draft = self.prepare()
        self.client.notes["Review0001"]["content"]["review"]["value"] = "New criticism"
        self.assert_code("draft_stale", lambda: self.submit(draft))
        self.assertEqual(self.client.post_count, 0)

    def test_changed_schema_requires_fresh_approval(self):
        draft = self.prepare()
        self.client.invitation["edit"]["note"]["readers"].append("everyone")
        self.assert_code("draft_stale", lambda: self.submit(draft))
        self.assertEqual(self.client.post_count, 0)

    def test_cross_forum_reply_is_rejected(self):
        self.client.notes["OtherReview"] = {"id": "OtherReview", "forum": "OtherPaper"}
        self.assert_code("invalid_target", lambda: self.prepare(replyto="OtherReview"))

    def test_fabricated_content_field_is_rejected(self):
        self.assert_code("invalid_content", lambda: self.prepare(content={"rebuttal": {"value": "Wrong field name"}}))

    def test_pdf_is_hashed_without_upload_and_verified_after_submit(self):
        pdf = Path(self.tmp.name) / "rebuttal.pdf"
        pdf.write_bytes(b"%PDF-test approved bytes")
        draft = self.prepare(attachments={"pdf": str(pdf)})
        self.assertEqual(self.client.upload_count, 0)
        result = self.submit(draft)
        self.assertEqual(result["status"], "verified")
        self.assertEqual((self.client.post_count, self.client.upload_count), (1, 1))
        self.assertEqual(self.client.uploads["/attachment/1.pdf"], pdf.read_bytes())

    def test_changed_attachment_cannot_be_uploaded(self):
        pdf = Path(self.tmp.name) / "rebuttal.pdf"
        pdf.write_bytes(b"%PDF-original")
        draft = self.prepare(attachments={"pdf": str(pdf)})
        pdf.write_bytes(b"%PDF-replaced")
        self.assert_code("draft_stale", lambda: self.submit(draft))
        self.assertEqual((self.client.post_count, self.client.upload_count), (0, 0))

    def test_lost_response_blocks_retry_and_readonly_reconcile_recovers(self):
        draft = self.prepare()
        self.client.raise_after_post = True
        self.assert_code("submission_unknown", lambda: self.submit(draft))
        self.assert_code("submission_unknown", lambda: self.submit(draft))
        self.assertEqual(self.client.post_count, 1)
        result = replies.reconcile_reply(self.client, draft["draft_id"])
        self.assertEqual(result["status"], "verified")
        self.assertEqual(self.client.post_count, 1)

    def test_absent_readback_never_resends(self):
        draft = self.prepare()
        self.client.fail_readback = True
        result = self.submit(draft)
        self.assertEqual(result["status"], "accepted_unverified")
        self.submit(draft)
        self.assertEqual(self.client.post_count, 1)
        self.client.fail_readback = False
        self.assertEqual(replies.reconcile_reply(self.client, draft["draft_id"])["status"], "verified")

    def test_modified_server_body_is_not_reported_verified(self):
        draft = self.prepare()
        self.client.wrong_readback = True
        self.assertEqual(self.submit(draft)["status"], "accepted_unverified")

    def test_update_requires_same_forum_and_writer(self):
        original = self.prepare()
        self.submit(original)
        draft = self.prepare(content={"comment": {"value": "Updated approved text"}}, edit_note_id="Created001")
        self.assertEqual(self.submit(draft)["status"], "verified")
        self.assertEqual(self.client.post_count, 2)
        self.assertEqual(self.client.notes["Created001"]["content"]["comment"]["value"], "Updated approved text")

    def test_wrong_account_cannot_use_approval(self):
        draft = self.prepare()
        self.client._or_author_verified_profile_id = "~Other_Author2"
        self.assert_code("account_mismatch", lambda: self.submit(draft))
        self.assertEqual(self.client.post_count, 0)

    def test_transport_retries_are_disabled_then_restored(self):
        try:
            import requests
        except ImportError:
            self.skipTest("requests is not available in this test interpreter")
        self.client.session = requests.Session()
        before = self.client.session.adapters.copy()
        with replies._no_write_retries(self.client):
            for suffix in ("/notes/edits", "/attachment"):
                adapter = self.client.session.get_adapter(self.client.baseurl + suffix)
                self.assertEqual(adapter.max_retries.total, 0)
        self.assertEqual(self.client.session.adapters, before)

    def test_standard_rebuttal_items_dynamic_readers_and_optional_ddate(self):
        edit = self.client.invitation["edit"]
        edit["signatures"] = {"param": {"items": [{"value": self.client.authors}]}}
        edit["readers"] = ["${2/note/readers}"]
        edit["note"]["ddate"] = {"param": {"range": [0, 9999999999999], "optional": True, "deletable": True}}
        draft = self.prepare()
        self.assertEqual(draft["prepared"]["payload"]["readers"], [self.client.chairs, self.client.authors])
        self.assertNotIn("ddate", draft["prepared"]["payload"]["note"])
        self.assertEqual(self.submit(draft)["status"], "verified")

    def test_invitation_cannot_silently_convert_create_to_update(self):
        self.client.invitation["edit"]["note"]["id"] = "Review0001"
        self.assert_code("invalid_target", lambda: self.prepare())
        self.assertEqual(self.client.post_count, 0)

    def test_actual_sdk_note_serialization_offline(self):
        try:
            import openreview
        except ImportError:
            self.skipTest("openreview-py is not available in this test interpreter")
        draft = self.prepare()
        with patch.object(replies, "load_sdk", return_value=openreview):
            result = self.submit(draft)
        self.assertEqual(result["status"], "verified")

    def test_actual_sdk_comment_form_does_not_post_presentation_metadata(self):
        try:
            from openreview import default_content
        except ImportError:
            self.skipTest("openreview-py is not available in this test interpreter")
        self.client.invitation["edit"]["note"]["content"] = copy.deepcopy(default_content.comment_v2)
        draft = self.prepare()
        content = draft["prepared"]["payload"]["note"]["content"]
        self.assertNotIn("title", content)
        self.assertEqual(set(content["comment"]), {"value"})
        self.assertEqual(self.submit(draft)["status"], "verified")

    def test_symlink_draft_is_rejected(self):
        draft = self.prepare()
        link = Path(self.tmp.name) / "linked-draft.json"
        link.symlink_to(draft["draft_path"])
        self.assert_code("invalid_draft", lambda: replies.preview_reply(str(link)))

    def test_visible_different_author_cannot_be_overridden_by_group(self):
        self.client.notes["Paper00001"]["content"]["authorids"]["value"] = ["~Different_Person1"]
        self.assert_code("author_unverified", lambda: self.prepare())

    def test_new_draft_cannot_bypass_an_uncertain_prior_write(self):
        first, second = self.prepare(), self.prepare()
        with patch.object(self.client, "post_note_edit", side_effect=TimeoutError()):
            self.assert_code("submission_unknown", lambda: self.submit(first))
        self.assert_code("submission_unknown", lambda: self.submit(second))
        self.assertEqual(self.client.post_count, 0)

    def test_interrupted_upload_requires_reconcile_before_explicit_retry(self):
        pdf = Path(self.tmp.name) / "reply.pdf"
        pdf.write_bytes(b"%PDF-file")
        draft = self.prepare(attachments={"pdf": str(pdf)})
        with patch.object(self.client, "put_attachment", side_effect=TimeoutError()):
            self.assert_code("submission_unknown", lambda: self.submit(draft))
        self.assert_code("submission_unknown", lambda: self.submit(draft))
        result = replies.reconcile_reply(self.client, draft["draft_id"])
        self.assertEqual(result["status"], "safe_to_retry")
        self.assertEqual(self.client.post_count, 0)
        self.assertEqual(self.submit(draft)["status"], "verified")

    def test_reconciling_old_receipt_cannot_clear_new_uncertain_write(self):
        original = self.prepare()
        self.submit(original)
        next_draft = self.prepare(content={"comment": {"value": "Approved follow-up"}})
        with patch.object(self.client, "post_note_edit", side_effect=TimeoutError()):
            self.assert_code("submission_unknown", lambda: self.submit(next_draft))
        self.assertEqual(replies.reconcile_reply(self.client, original["draft_id"])["status"], "verified")
        third = self.prepare(content={"comment": {"value": "Approved follow-up"}})
        self.assert_code("submission_unknown", lambda: self.submit(third))
        self.assertEqual(self.client.post_count, 1)

    def test_development_api_does_not_return_a_production_url(self):
        self.client.baseurl = "https://api2.dev.openreview.net"
        draft = self.prepare()
        receipt = replies.submit_reply(self.client, draft["draft_id"], draft["confirmation"], baseurl=self.client.baseurl)
        self.assertEqual(receipt["status"], "verified")
        self.assertEqual(receipt["forum_id"], "Paper00001")
        self.assertIsNone(receipt["url"])


if __name__ == "__main__":
    unittest.main()
