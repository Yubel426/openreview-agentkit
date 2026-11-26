"""Conservative, approval-bound author replies for OpenReview API 2.

This module never signs in.  A caller supplies an authenticated SDK client.
Preparing and reconciling are read-only network operations.  Local schema checks
are deliberately a subset of OpenReview's validator; unsupported templates fail
closed, while venue preprocess/process code remains a server-side check.
"""
from __future__ import annotations

import copy
import hashlib
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from or_core import (
    ToolError, api_call, canonical_hash, load_sdk, normalize_baseurl, obj_json,
    parse_note_id, profile_id, read_json, state_dir, write_private_json,
)

_MISSING = object()
_DOLLAR = re.compile(r"\$\{(\d+)/([^}]+)\}")
_VISUAL = {"description", "order", "input", "markdown", "scroll", "default",
           "fieldName", "hidden", "presentation"}
_VALIDATION = {"type", "optional", "deletable", "const", "enum", "items", "regex",
               "range", "minLength", "maxLength", "minimum", "maximum", "maxSize",
               "extensions", "withInvitation", "withForum", "withVenueid", "inGroup"}
_NOTE_KEYS = {"id", "forum", "replyto", "signatures", "readers", "writers",
              "nonreaders", "content", "license", "ddate"}
_EDIT_KEYS = {"signatures", "readers", "writers", "nonreaders", "note"}


def _fail(code, message, **details):
    raise ToolError(code, message, details or None)


def _json(value):
    return copy.deepcopy(obj_json(value))


def _now():
    return int(time.time() * 1000)


def _value(value):
    return value.get("value") if isinstance(value, dict) and "value" in value else value


def _node_hash(note):
    return canonical_hash({k: note.get(k) for k in (
        "id", "forum", "replyto", "content", "signatures", "readers", "writers",
        "nonreaders", "invitations", "tmdate", "mdate", "ddate")})


def _inv_hash(inv):
    return canonical_hash({k: inv.get(k) for k in (
        "id", "edit", "invitees", "noninvitees", "cdate", "duedate", "expdate",
        "ddate", "maxReplies", "preprocess", "process", "invitations")})


class _Context:
    def __init__(self, client):
        self.client = client
        self.actor = profile_id(client)
        self.profile = _json(api_call(client.get_profile, self.actor))
        self.identities = {self.actor}
        pc = self.profile.get("content", {})
        for key in ("emailsConfirmed", "confirmedEmails"):
            self.identities.update(x for x in pc.get(key, []) if isinstance(x, str))
        uid = getattr(client, "user", {})
        if isinstance(uid, dict) and isinstance(uid.get("id"), str):
            self.identities.add(uid["id"])
        self.groups = {}
        self.notes = {}

    def note(self, note_id):
        if note_id not in self.notes:
            self.notes[note_id] = _json(api_call(self.client.get_note, note_id))
        return self.notes[note_id]

    def group(self, group_id):
        if group_id not in self.groups:
            self.groups[group_id] = _json(api_call(self.client.get_group, group_id))
        return self.groups[group_id]

    def member(self, group_id, identities=None, seen=None):
        identities = self.identities if identities is None else set(identities)
        if group_id in identities:
            return True
        if group_id == "everyone" or (group_id == "~" and self.actor.startswith("~")):
            return True
        seen = set() if seen is None else set(seen)
        if group_id in seen or len(seen) >= 8:
            return False
        seen.add(group_id)
        if not isinstance(group_id, str) or group_id.startswith("~") or "@" in group_id:
            return False
        try:
            group = self.group(group_id)
        except Exception:
            return False
        members = group.get("members", [])
        return any(m in identities or self.member(m, identities, seen) for m in members)


def _path_get(doc, path):
    out = doc
    for part in path:
        try:
            out = out[int(part)] if isinstance(out, list) else out[part]
        except (KeyError, IndexError, TypeError, ValueError):
            _fail("unsupported_schema", "A dynamic invitation value cannot be resolved locally.", path="/".join(map(str, path)))
    return out


def _resolve(value, path, doc):
    if isinstance(value, str) and "${" in value:
        matches = list(_DOLLAR.finditer(value))
        if not matches:
            _fail("unsupported_schema", "Unrecognized dynamic invitation expression.")
        def lookup(match):
            up = int(match.group(1))
            if up > len(path):
                _fail("unsupported_schema", "Invitation expression escapes the note edit.")
            target = tuple(path[:-up] if up else path) + tuple(match.group(2).split("/"))
            return copy.deepcopy(_path_get(doc, target))
        if len(matches) == 1 and matches[0].span() == (0, len(value)):
            return lookup(matches[0])
        result = value
        for match in reversed(matches):
            replacement = lookup(match)
            if not isinstance(replacement, (str, int)):
                _fail("unsupported_schema", "A compound invitation expression is not scalar.")
            result = result[:match.start()] + str(replacement) + result[match.end():]
        return result
    if isinstance(value, list):
        result = []
        for index, item in enumerate(value):
            item = _resolve(item, path + (str(index),), doc)
            result.extend(item if isinstance(item, list) else [item])
        return result
    if isinstance(value, dict):
        return {k: _resolve(v, path + (k,), doc) for k, v in value.items()}
    return value


def _regex_matches(pattern, value):
    # Conservative full matches: unsupported JS regex constructs fail closed.
    try:
        return isinstance(value, str) and re.fullmatch(pattern, value) is not None
    except re.error:
        _fail("unsupported_schema", "Invitation uses an unsupported regular expression.")


def _validate(value, param, path, ctx):
    unknown = set(param) - _VALIDATION - _VISUAL
    if unknown:
        _fail("unsupported_schema", "Invitation has unsupported validation specifiers.", fields=sorted(unknown), path="/".join(path))
    typ = param.get("type")
    if typ is None and path[-1] in ("readers", "writers", "signatures", "nonreaders"):
        typ = "group[]"
    if typ is None and path[-1] in ("id", "forum", "replyto"):
        typ = "note"
    if typ is None:
        _fail("unsupported_schema", "A content field has no locally inferable type.", path="/".join(path))
    array = typ.endswith("[]")
    element_type = typ[:-2] if array else typ
    values = value if array and isinstance(value, list) else [value]
    if array and not isinstance(value, list):
        _fail("invalid_content", "Expected an array.", path="/".join(path))
    for item in values:
        good = ((element_type in ("string", "group", "profile", "note", "file") and isinstance(item, str))
                or (element_type in ("integer", "date") and type(item) is int)
                or (element_type in ("float", "number") and type(item) in (int, float))
                or (element_type == "boolean" and type(item) is bool))
        if not good:
            _fail("invalid_content", "Content does not match the invitation type.", path="/".join(path), expected=typ)
        if "regex" in param and not _regex_matches(param["regex"], item):
            _fail("invalid_content", "Value does not match the invitation pattern.", path="/".join(path))
        if "enum" in param and item not in param["enum"] and not any(
                isinstance(e, str) and _regex_matches(e, item) for e in param["enum"]):
            _fail("invalid_content", "Value is not allowed by the invitation.", path="/".join(path))
        for key, bound in (("minimum", "lower"), ("maximum", "upper")):
            if key in param and (type(item) not in (int, float) or (item < param[key] if bound == "lower" else item > param[key])):
                _fail("invalid_content", "Numeric value is outside the invitation range.", path="/".join(path))
        if "range" in param and (type(item) not in (int, float) or not param["range"][0] <= item <= param["range"][1]):
            _fail("invalid_content", "Numeric value is outside the invitation range.", path="/".join(path))
        for key in ("withInvitation", "withForum", "withVenueid"):
            if key in param:
                note = ctx.note(item)
                valid = (param[key] in note.get("invitations", []) if key == "withInvitation" else
                         (note.get("forum", note.get("id")) == param[key] if key == "withForum" else
                          _value(note.get("content", {}).get("venueid")) == param[key]))
                if not valid:
                    _fail("invalid_target", "Target does not satisfy the invitation relationship.", field=key)
        if "inGroup" in param and not ctx.member(param["inGroup"], {item}):
            _fail("permission_unverified", "Could not verify required group membership.", path="/".join(path))
    for key, lower in (("minLength", True), ("maxLength", False)):
        if key in param:
            if not isinstance(value, (str, list)) or (len(value) < param[key] if lower else len(value) > param[key]):
                _fail("invalid_content", "Content length violates the invitation.", path="/".join(path), constraint=key, limit=param[key])
    if "items" in param:
        if not array:
            _fail("unsupported_schema", "The items specifier is supported only for arrays.")
        items = param["items"]
        def matches(item, rule):
            if item == rule.get("value", _MISSING):
                return True
            prefix = rule.get("prefix")
            # OpenReview uses suffix wildcards in prefixes such as "~.*".
            return isinstance(prefix, str) and isinstance(item, str) and item.startswith(prefix.removesuffix(".*"))
        if any(not any(matches(item, rule) for rule in items) for item in value):
            _fail("invalid_content", "An array item is not permitted by the invitation.", path="/".join(path))
        if any(not rule.get("optional", False) and not any(matches(item, rule) for item in value) for rule in items):
            _fail("invalid_content", "A required array item is missing.", path="/".join(path))


def _materialize(schema, supplied, path, doc, ctx):
    if len(path) == 3 and path[:2] == ("note", "content") and isinstance(schema, dict):
        # Official forms put presentation metadata beside value, not only inside
        # param. It describes the editor and is not note content to be submitted.
        schema = {key: value for key, value in schema.items() if key not in _VISUAL}
    if (len(path) == 3 and path[:2] == ("note", "content")
            and supplied is _MISSING and isinstance(schema, dict)):
        value_schema = schema.get("value", {})
        param = value_schema.get("param", {}) if isinstance(value_schema, dict) else {}
        if "const" not in param and (param.get("optional") or param.get("deletable")):
            return _MISSING
    if isinstance(schema, dict) and "param" in schema:
        param = _resolve(schema["param"], path + ("param",), doc)
        if "const" in param:
            fixed = param["const"]
            if supplied is not _MISSING and supplied != fixed:
                _fail("invalid_content", "A constant invitation value cannot be changed.", path="/".join(path))
            return fixed
        if supplied is _MISSING:
            if param.get("optional") or param.get("deletable"):
                return _MISSING
            _fail("missing_field", "A required invitation field needs an explicit value.", path="/".join(path))
        _validate(supplied, param, path, ctx)
        return copy.deepcopy(supplied)
    if isinstance(schema, dict):
        if supplied is not _MISSING and not isinstance(supplied, dict):
            _fail("invalid_content", "Expected an object.", path="/".join(path))
        supplied = {} if supplied is _MISSING else supplied
        extra = set(supplied) - set(schema)
        if extra:
            _fail("invalid_content", "Content contains fields absent from the actual invitation.", path="/".join(path), fields=sorted(extra))
        result = {}
        # A standard OpenReview edit copies its readers from note.readers, so
        # materialize note before the outer edit's audience fields.
        order = {"signatures": 0, "id": 1, "note": 1, "forum": 2, "replyto": 3, "readers": 4, "writers": 5, "content": 6}
        for key in sorted(schema, key=lambda k: order.get(k, 7)):
            built = _materialize(schema[key], supplied.get(key, _MISSING), path + (key,), doc, ctx)
            if built is not _MISSING:
                result[key] = built
                target = doc
                for part in path:
                    target = target.setdefault(part, {})
                target[key] = built
        if len(path) == 3 and path[:2] == ("note", "content") and not result:
            return _MISSING
        return result
    fixed = _resolve(schema, path, doc)
    if supplied is not _MISSING and supplied != fixed:
        _fail("invalid_content", "A constant invitation value cannot be changed.", path="/".join(path))
    return copy.deepcopy(fixed)


def _collect_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _collect_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _collect_strings(item)


def _check_invitation(inv, ctx):
    now = _now()
    if inv.get("ddate"):
        _fail("invitation_closed", "Invitation has been deleted.")
    if inv.get("cdate", 0) > now:
        _fail("invitation_closed", "Invitation is not open yet.")
    for key in ("duedate", "expdate"):
        if inv.get(key) and inv[key] <= now:
            _fail("invitation_closed", "The invitation deadline has passed.", field=key, timestamp=inv[key])
    if any(ctx.member(g) for g in inv.get("noninvitees", [])):
        _fail("permission_denied", "This account is excluded by the invitation.")
    if not any(ctx.member(g) for g in inv.get("invitees", [])):
        _fail("permission_unverified", "Could not verify that this account may use the invitation.")
    edit = inv.get("edit")
    if not isinstance(edit, dict) or not isinstance(edit.get("note"), dict):
        _fail("unsupported_schema", "Only concrete API2 Note Edit invitations are supported.")
    if set(edit) - _EDIT_KEYS or set(edit["note"]) - _NOTE_KEYS:
        _fail("unsupported_schema", "This invitation requires unsupported edit or note fields.")
    return edit


def _choose_signature(edit, inv, forum, ctx, signature):
    schema = edit.get("signatures")
    if schema is None:
        _fail("unsupported_schema", "Invitation does not expose a signing rule.")
    if signature is not None:
        if not isinstance(signature, str) or signature in ("everyone", "~"):
            _fail("invalid_signature", "Provide one concrete author signature.")
        candidates = [signature]
    else:
        candidates = sorted({s for s in _collect_strings([schema, inv.get("invitees", []), forum.get("writers", []), forum.get("readers", [])])
                             if s.endswith("/Authors") and "${" not in s and ctx.member(s)})
        if isinstance(schema, list) and len(schema) == 1 and schema[0] == ctx.actor:
            candidates = [ctx.actor]
    valid = []
    for candidate in candidates:
        if candidate != ctx.actor and (not candidate.endswith("/Authors") or not ctx.member(candidate)):
            continue
        try:
            _materialize(schema, [candidate], ("signatures",), {"signatures": [candidate]}, ctx)
            valid.append(candidate)
        except ToolError:
            pass
    if len(valid) != 1:
        _fail("signature_required", "No single verified author signature was selected; provide an allowed signature explicitly.", candidates=valid)
    signature = valid[0]
    authorids = _value(forum.get("content", {}).get("authorids")) or []
    proven_author = isinstance(authorids, list) and bool(set(authorids) & ctx.identities)
    if authorids and not proven_author:
        _fail("author_unverified", "Visible author IDs do not identify this account as an author of the target submission.")
    # When author IDs are hidden, use an Authors group exposed by the actual
    # submission. An unrelated invitation's Authors group is not sufficient.
    author_groups = {s for s in _collect_strings([forum.get("writers", []), forum.get("readers", [])])
                     if s.endswith("/Authors") and "${" not in s}
    proven_author = proven_author or any(ctx.member(g) for g in author_groups)
    if not proven_author:
        _fail("author_unverified", "Could not verify this account as an author of the target submission.")
    return signature


def _attachment_info(attachments, note_schema):
    result = {}
    for field, filename in (attachments or {}).items():
        path = Path(filename).expanduser()
        if not path.is_absolute() or not path.is_file():
            _fail("invalid_attachment", "Attachment must be an existing absolute file path.", field=field)
        path = path.resolve()
        schema = note_schema.get("content", {}).get(field, {})
        param = schema.get("value", {}).get("param", {}) if isinstance(schema.get("value"), dict) else {}
        if param.get("type") != "file":
            _fail("invalid_attachment", "Attachment field is not a file field in the actual invitation.", field=field)
        size = path.stat().st_size
        if "maxSize" in param and size > float(param["maxSize"]) * 1_000_000:
            _fail("invalid_attachment", "Attachment exceeds the invitation file size limit.", field=field)
        if "extensions" in param and path.suffix.lstrip(".").lower() not in [str(x).lower() for x in param["extensions"]]:
            _fail("invalid_attachment", "Attachment extension is not permitted.", field=field)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        result[field] = {"path": str(path), "sha256": digest.hexdigest(), "size": size}
    return result


def _all_replies(client, forum_id):
    return [_json(n) for n in api_call(client.get_all_notes, forum=forum_id)]


def _prepare_data(client, forum_id, invitation_id, content, replyto, signature,
                  readers, attachments, edit_note_id):
    ctx = _Context(client)
    forum_id = parse_note_id(forum_id)
    forum = ctx.note(forum_id)
    if forum.get("id") != forum_id or forum.get("forum", forum_id) != forum_id or forum.get("replyto") or forum.get("ddate"):
        _fail("invalid_target", "The target must be a live submission forum, not a reply.")
    inv = _json(api_call(client.get_invitation, invitation_id))
    if inv.get("id") != invitation_id:
        _fail("invalid_target", "The returned invitation ID does not match the requested invitation.")
    edit = _check_invitation(inv, ctx)
    signature = _choose_signature(edit, inv, forum, ctx, signature)
    reply_schema = edit["note"].get("replyto", _MISSING)
    if replyto is None:
        # Only an explicit constant from this invitation is inferred.
        if isinstance(reply_schema, str) and "${" not in reply_schema:
            replyto = reply_schema
        elif isinstance(reply_schema, dict) and "const" in reply_schema.get("param", {}):
            replyto = reply_schema["param"]["const"]
        else:
            _fail("replyto_required", "This invitation requires an explicit reply target.")
    replyto = parse_note_id(replyto)
    target = ctx.note(replyto)
    if target.get("forum", target.get("id")) != forum_id or target.get("ddate"):
        _fail("invalid_target", "The reply target does not belong to this submission forum.")
    if not isinstance(content, dict):
        _fail("invalid_content", "Pass API2 content as a dictionary of field objects.")
    supplied_content = copy.deepcopy(content)
    for field, field_content in supplied_content.items():
        if not isinstance(field_content, dict) or "value" not in field_content or set(field_content) - {"value", "readers"}:
            _fail("invalid_content", "Each content field must contain value and optionally readers.", field=field)
    attachment_data = _attachment_info(attachments, edit["note"])
    for field, data in attachment_data.items():
        if field in supplied_content:
            _fail("invalid_attachment", "Specify a file field via attachments, not both content and attachments.", field=field)
        supplied_content[field] = {"value": "local-attachment:sha256:" + data["sha256"]}
    supplied = {"signatures": [signature], "note": {"forum": forum_id, "replyto": replyto, "content": supplied_content}}
    if readers is not None:
        if not isinstance(readers, list) or not readers:
            _fail("invalid_readers", "Explicit readers must be a non-empty list.")
        supplied["note"]["readers"] = readers
    original = None
    if edit_note_id:
        edit_note_id = parse_note_id(edit_note_id)
        original = ctx.note(edit_note_id)
        if original.get("forum") != forum_id or original.get("replyto") != replyto or original.get("ddate"):
            _fail("invalid_target", "The note to edit is not a live reply at this exact target.")
        if not any(ctx.member(w) for w in original.get("writers", [])):
            _fail("permission_unverified", "Could not verify write permission on the existing reply.")
        supplied["note"]["id"] = edit_note_id
    seed = copy.deepcopy(supplied)
    payload = _materialize(edit, supplied, (), seed, ctx)
    note = payload["note"]
    if note.get("id") != edit_note_id or note.get("ddate"):
        _fail("invalid_target", "Invitation would modify or delete a note that was not explicitly approved for editing.")
    if note.get("forum") != forum_id or note.get("replyto") != replyto:
        _fail("invalid_target", "Invitation does not bind the requested forum and reply target.")
    if not note.get("readers") or not note.get("signatures"):
        _fail("unsupported_schema", "The final note audience and signature must be resolved before approval.")
    if any(s != ctx.actor and (not s.endswith("/Authors") or not ctx.member(s)) for s in note["signatures"]):
        _fail("invalid_signature", "The resulting note is not signed by a verified author identity.")
    payload["invitation"] = invitation_id
    replies = _all_replies(client, forum_id)
    if inv.get("maxReplies") is not None and not edit_note_id:
        existing = [n for n in replies if invitation_id in n.get("invitations", []) and not n.get("ddate")]
        if len(existing) >= inv["maxReplies"]:
            _fail("reply_limit", "This invitation has reached its configured reply limit.")
    return {
        "account_id": ctx.actor, "baseurl": normalize_baseurl(client.baseurl),
        "forum_id": forum_id, "forum_hash": _node_hash(forum), "forum_title": _value(forum.get("content", {}).get("title")),
        "replyto": replyto, "replyto_hash": _node_hash(target),
        "invitation_id": invitation_id, "invitation_sha256": _inv_hash(inv),
        "edit_note_id": edit_note_id, "original_note_sha256": _node_hash(original) if original else None,
        "payload": payload, "attachments": attachment_data,
        "input": {"content": content, "signature": signature, "readers": readers},
        "before_note_ids": sorted(n["id"] for n in replies if n.get("id")),
        "validation": {"mode": "conservative_local_subset", "server_validation_only": [
            "OpenReview authorization and transitive signatory policy",
            "venue preprocess/process rules and any concurrent server-side changes"]},
    }


def _safe_draft_path(path_or_id):
    value = str(path_or_id)
    if re.fullmatch(r"[a-f0-9]{32}", value):
        return state_dir() / "drafts" / (value + ".json")
    # Preserve the original path until core.read_json checks all components.
    # Resolving first would hide a symlink from that check.
    path = Path(value).expanduser().absolute()
    if not path.is_file() or path.is_symlink():
        _fail("invalid_draft", "Draft file does not exist or is not a regular file.")
    return path


def _load_draft(path_or_id):
    path = _safe_draft_path(path_or_id)
    draft = read_json(path)
    if set(draft) != {"version", "draft_id", "created_at", "prepared", "payload_sha256"} or draft.get("version") != 1:
        _fail("invalid_draft", "Unsupported or altered draft structure.")
    unsigned = {k: v for k, v in draft.items() if k != "payload_sha256"}
    if canonical_hash(unsigned) != draft["payload_sha256"]:
        _fail("draft_changed", "Draft content changed after preparation. Prepare a new draft.")
    if not re.fullmatch(r"[a-f0-9]{32}", draft["draft_id"]):
        _fail("invalid_draft", "Invalid draft ID.")
    return path, draft


def prepare_reply(client, forum_id, invitation_id, content, replyto=None,
                  signature=None, readers=None, attachments=None,
                  edit_note_id=None, draft_dir=None):
    """Prepare and persist an immutable approval package; never upload or post."""
    prepared = _prepare_data(client, forum_id, invitation_id, content, replyto,
                             signature, readers, attachments, edit_note_id)
    draft = {"version": 1, "draft_id": uuid.uuid4().hex, "created_at": _now(), "prepared": prepared}
    draft["payload_sha256"] = canonical_hash(draft)
    directory = Path(draft_dir).expanduser().absolute() if draft_dir else state_dir() / "drafts"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / (draft["draft_id"] + ".json")
    write_private_json(path, draft)
    return {**draft, "draft_path": str(path), "confirmation": _confirmation(draft)}


def _confirmation(draft):
    return "SUBMIT " + draft["draft_id"] + " " + draft["payload_sha256"]


def preview_reply(draft_path_or_id):
    path, draft = _load_draft(draft_path_or_id)
    data = draft["prepared"]
    return {
        "draft_id": draft["draft_id"], "draft_path": str(path), "payload_sha256": draft["payload_sha256"],
        "confirmation": _confirmation(draft), "account_id": data["account_id"],
        "forum_title": data["forum_title"], "forum_id": data["forum_id"], "replyto": data["replyto"],
        "invitation_id": data["invitation_id"], "edit_note_id": data["edit_note_id"],
        "payload": data["payload"], "attachments": data["attachments"], "validation": data["validation"],
        "attachment_note": "local-attachment markers are replaced only after confirmed upload; the approval binds exact file bytes.",
    }


@contextmanager
def _lock(data):
    import fcntl
    directory = state_dir() / "locks"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = canonical_hash([data["baseurl"], data["account_id"], data["forum_id"]])
    fd = os.open(directory / (name + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextmanager
def _no_write_retries(client):
    """Disable SDK transport retries for this client's write endpoints only."""
    session = getattr(client, "session", None)
    if session is None:  # Fake clients in offline tests have no transport.
        yield
        return
    from requests.adapters import HTTPAdapter
    old = session.adapters.copy()
    adapter = HTTPAdapter(max_retries=0)
    try:
        session.mount(client.baseurl.rstrip("/") + "/notes/edits", adapter)
        session.mount(client.baseurl.rstrip("/") + "/attachment", adapter)
        yield
    finally:
        session.adapters = old
        adapter.close()


def _receipt_path(path):
    return path.with_suffix(".receipt.json")


def _operation_key(data):
    return canonical_hash({k: data.get(k) for k in (
        "baseurl", "account_id", "forum_id", "replyto", "invitation_id", "edit_note_id")})


def _operation_path(key):
    if not re.fullmatch(r"[a-f0-9]{64}", key):
        _fail("invalid_receipt", "Invalid pending operation identifier.")
    return state_dir() / "operations" / (key + ".json")


def _save_receipt(path, receipt):
    receipt["updated_at"] = _now()
    write_private_json(_receipt_path(path), receipt, overwrite=True)
    # Also block a newly prepared draft from bypassing an uncertain earlier
    # write to the same target, even if the draft lives in a custom directory.
    if receipt.get("operation_key"):
        operation_path = _operation_path(receipt["operation_key"])
        existing = read_json(operation_path) if operation_path.exists() else {}
        # Rechecking an old successful receipt must not erase a newer uncertain
        # attempt at this same target.
        if (existing.get("draft_id") != receipt["draft_id"]
                and existing.get("status") in ("preparing_write", "uploading", "submitting", "unknown")):
            return
        write_private_json(operation_path, {
            "draft_id": receipt["draft_id"], "draft_path": str(path),
            "status": receipt["status"], "updated_at": receipt["updated_at"],
        }, overwrite=True)


def _read_receipt(path, draft):
    receipt_path = _receipt_path(path)
    if not receipt_path.exists():
        return None
    receipt = read_json(receipt_path)
    if receipt.get("draft_id") != draft["draft_id"] or receipt.get("payload_sha256") != draft["payload_sha256"]:
        _fail("invalid_receipt", "Stored receipt does not belong to this approval package.")
    if receipt.get("operation_key") != _operation_key(draft["prepared"]):
        _fail("invalid_receipt", "Stored receipt does not match this submission target.")
    return receipt


def _matches(client, note, data, verify_attachments=False):
    expected = data["payload"]["note"]
    if note.get("ddate") or note.get("forum") != data["forum_id"] or note.get("replyto") != data["replyto"]:
        return False
    if data["invitation_id"] not in note.get("invitations", []):
        return False
    for field in ("signatures", "readers", "writers", "nonreaders"):
        if field in expected and set(note.get(field, [])) != set(expected[field]):
            return False
    for field, content in expected.get("content", {}).items():
        actual = note.get("content", {}).get(field, {})
        if field in data["attachments"]:
            if not isinstance(_value(actual), str) or not _value(actual):
                return False
            if verify_attachments:
                blob = api_call(client.get_attachment, field_name=field, id=note["id"])
                if not isinstance(blob, bytes) or hashlib.sha256(blob).hexdigest() != data["attachments"][field]["sha256"]:
                    return False
        elif _value(actual) != content.get("value"):
            return False
        if "readers" in content and set(actual.get("readers", [])) != set(content["readers"]):
            return False
    return True


def _verified_receipt(client, path, draft, receipt, note_id):
    try:
        note = _json(api_call(client.get_note, note_id))
        if not _matches(client, note, draft["prepared"], verify_attachments=True):
            receipt["status"] = "accepted_unverified"
            receipt["verification"] = "Readback differs from the approved content, target, signature, visibility, or attachment. Do not resend."
        else:
            receipt["status"] = "verified"
            receipt["verification"] = "Server note and any attachment bytes match the approved package."
            receipt["verified_at"] = _now()
    except Exception:
        receipt["status"] = "accepted_unverified"
        receipt["verification"] = "The server accepted a note ID but readback could not be verified. Use reconcile; do not resend."
    receipt["note_id"] = note_id
    receipt["forum_id"] = draft["prepared"]["forum_id"]
    receipt["url"] = ("https://openreview.net/forum?id=" + receipt["forum_id"] + "&noteId=" + note_id
                      if draft["prepared"]["baseurl"] == "https://api2.openreview.net" else None)
    _save_receipt(path, receipt)
    return receipt


def _refresh(client, data, baseurl):
    if normalize_baseurl(baseurl) != data["baseurl"] or normalize_baseurl(client.baseurl) != data["baseurl"]:
        _fail("account_mismatch", "Client endpoint does not match the approved draft.")
    live = _prepare_data(client, data["forum_id"], data["invitation_id"], data["input"]["content"],
                         data["replyto"], data["input"]["signature"], data["input"]["readers"],
                         {k: v["path"] for k, v in data["attachments"].items()}, data["edit_note_id"])
    fields = ("account_id", "forum_hash", "replyto_hash", "invitation_sha256", "original_note_sha256", "payload", "attachments")
    changed = [key for key in fields if live[key] != data[key]]
    if changed:
        _fail("draft_stale", "Account, target, invitation, edited note, or attachment changed. Prepare and approve a new draft.", changed=changed)


def submit_reply(client, draft_path_or_id, confirmation, baseurl="https://api2.openreview.net"):
    """Submit exactly one approved package; ambiguous writes are never retried."""
    path, draft = _load_draft(draft_path_or_id)
    if confirmation != _confirmation(draft):
        _fail("confirmation_mismatch", "Confirmation must match this exact draft ID and SHA256.")
    data = draft["prepared"]
    with _lock(data):
        if profile_id(client) != data["account_id"] or normalize_baseurl(client.baseurl) != data["baseurl"]:
            _fail("account_mismatch", "The logged-in account or endpoint differs from the approved draft.")
        receipt = _read_receipt(path, draft)
        if receipt:
            if receipt["status"] in ("verified", "accepted_unverified"):
                return {**receipt, "already_submitted": True}
            if receipt["status"] != "safe_to_retry":
                _fail("submission_unknown", "An earlier write may have succeeded. Run read-only reconcile; automatic resubmission is blocked.", status=receipt["status"])
        operation_path = _operation_path(_operation_key(data))
        if operation_path.exists():
            pending = read_json(operation_path)
            if pending.get("status") in ("preparing_write", "uploading", "submitting", "unknown"):
                _fail("submission_unknown", "An earlier draft has an uncertain write to this target. Reconcile that draft before any new submission.", draft_id=pending.get("draft_id"), draft_path=pending.get("draft_path"))
        _refresh(client, data, baseurl)
        matches = [n for n in _all_replies(client, data["forum_id"])
                   if (not data["edit_note_id"] or n.get("id") == data["edit_note_id"])
                   and _matches(client, n, data, verify_attachments=True)]
        if len(matches) > 1:
            _fail("duplicate_ambiguous", "Multiple existing notes match; resolve them before submitting.", note_ids=[n["id"] for n in matches])
        receipt = {"draft_id": draft["draft_id"], "payload_sha256": draft["payload_sha256"],
                   "operation_key": _operation_key(data), "status": "preparing_write", "post_attempted": False, "uploads": {}}
        if matches:
            receipt["reused_existing"] = True
            return _verified_receipt(client, path, draft, receipt, matches[0]["id"])
        payload = copy.deepcopy(data["payload"])
        # Freeze exact approved bytes before upload; later edits to the source file
        # cannot change the file that this submission sends.
        with tempfile.TemporaryDirectory(prefix="openreview-author-") as tempdir:
            staged = {}
            for field, meta in data["attachments"].items():
                source = Path(meta["path"])
                blob = source.read_bytes()
                if len(blob) != meta["size"] or hashlib.sha256(blob).hexdigest() != meta["sha256"]:
                    _fail("attachment_changed", "Attachment changed before upload. Prepare a new draft.", field=field)
                stage = Path(tempdir) / (hashlib.sha256(field.encode()).hexdigest() + source.suffix)
                fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as output:
                    output.write(blob)
                staged[field] = stage
            _save_receipt(path, receipt)
            try:
                with _no_write_retries(client):
                    for field, stage in staged.items():
                        receipt["status"] = "uploading"
                        _save_receipt(path, receipt)
                        uploaded = api_call(client.put_attachment, str(stage), data["invitation_id"], field)
                        if not isinstance(uploaded, str) or not uploaded.startswith("/"):
                            _fail("invalid_upload_response", "OpenReview did not return a valid attachment path.")
                        receipt["uploads"][field] = uploaded
                        payload["note"]["content"][field]["value"] = uploaded
                        _save_receipt(path, receipt)
                    kwargs = {k: v for k, v in payload.items() if k != "note"}
                    kwargs["note"] = load_sdk().api.Note(**payload["note"])
                    kwargs["await_process"] = False
                    receipt["status"] = "submitting"
                    receipt["post_attempted"] = True
                    receipt["submitted_payload_sha256"] = canonical_hash(payload)
                    _save_receipt(path, receipt)
                    response = _json(api_call(client.post_note_edit, **kwargs))
                note_id = response.get("note", {}).get("id")
                if not note_id:
                    _fail("invalid_submit_response", "The submission response contained no note ID.")
                receipt["note_id"] = note_id
                receipt["edit_id"] = response.get("id")
                receipt["status"] = "accepted_unverified"
                _save_receipt(path, receipt)
            except BaseException:
                receipt["status"] = "unknown"
                receipt["verification"] = "A write was interrupted or failed; whether it took effect is not established. Do not resend."
                _save_receipt(path, receipt)
                _fail("submission_unknown", "Write outcome is uncertain. Run read-only reconcile; automatic resubmission is blocked.", draft_id=draft["draft_id"])
        return _verified_receipt(client, path, draft, receipt, note_id)


def reconcile_reply(client, draft_path_or_id):
    """Read existing notes to recover a receipt; never upload, post, or resend."""
    path, draft = _load_draft(draft_path_or_id)
    data = draft["prepared"]
    if profile_id(client) != data["account_id"] or normalize_baseurl(client.baseurl) != data["baseurl"]:
        _fail("account_mismatch", "Reconciliation requires the account and endpoint from the draft.")
    with _lock(data):
        receipt = _read_receipt(path, draft)
        if not receipt:
            _fail("no_submission", "This draft has no write attempt to reconcile.")
        if receipt.get("note_id"):
            return _verified_receipt(client, path, draft, receipt, receipt["note_id"])
        matches = [n for n in _all_replies(client, data["forum_id"])
                   if (n.get("id") == data["edit_note_id"] if data["edit_note_id"] else n.get("id") not in data["before_note_ids"])
                   and _matches(client, n, data, verify_attachments=True)]
        if len(matches) == 1:
            receipt["reconciled"] = True
            return _verified_receipt(client, path, draft, receipt, matches[0]["id"])
        if not matches and not receipt.get("post_attempted"):
            receipt["status"] = "safe_to_retry"
            receipt["verification"] = "The journal shows no note POST was attempted; read-only reconciliation found no matching note. An explicit confirmed submit may resume."
            _save_receipt(path, receipt)
            return receipt
        receipt["status"] = "unknown"
        receipt["verification"] = "No unique matching note was found. Absence does not prove failure; resubmission remains blocked."
        receipt["matching_note_ids"] = [n["id"] for n in matches]
        _save_receipt(path, receipt)
        return receipt
