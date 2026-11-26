# OpenReview API v2 author operations

Verified against official documentation and `openreview/openreview-py` on 2026-11-25.
Use this as a technical reference; the live invitation and current venue instructions
determine what the author may actually submit.

## Sources

- [API versions](https://docs.openreview.net/getting-started/using-the-api)
- [Python client setup](https://docs.openreview.net/getting-started/using-the-api/installing-and-instantiating-the-python-client)
- [Notes and reply structure](https://docs.openreview.net/getting-started/objects-in-openreview/introduction-to-notes)
- [Note fields](https://docs.openreview.net/reference/api-v2/entities/note/fields)
- [Invitation fields](https://docs.openreview.net/reference/api-v2/entities/invitation/fields)
- [Invitation validation specifiers](https://docs.openreview.net/reference/api-v2/entities/invitation/specifiers)
- [Invitation types and structure](https://docs.openreview.net/reference/api-v2/entities/invitation/types-and-structure)
- [Edit fields](https://docs.openreview.net/reference/api-v2/entities/edit/fields)
- [Create and modify notes](https://docs.openreview.net/how-to-guides/workflow/how-to-create-change-and-delete-notes)
- [Retrieve submissions, reviews and rebuttals](https://docs.openreview.net/how-to-guides/data-retrieval-and-modification/how-to-get-all-notes-for-submissions-reviews-rebuttals-etc)
- [Official Python client source](https://github.com/openreview/openreview-py/blob/master/openreview/api/client.py)
- [Official MFA helpers](https://github.com/openreview/openreview-py/blob/master/openreview/mfa.py)

## Objects and identifiers

Use `openreview.api.OpenReviewClient(baseurl="https://api2.openreview.net", ...)`.
API v1 has different object formats; report an unsupported API version instead of
silently translating a v1 invitation or posting to the other API.

An author response is a Note created or changed through a Note Edit:

| Field | Meaning and handling |
| --- | --- |
| `invitation` | Exact live invitation governing the edit. Discover it; do not invent it from a paper number. |
| `note.id` | Existing response's ID for an update. Omit for a new reply. Do not put the submission ID here when creating a rebuttal. |
| `note.forum` | Top-level submission Note ID, shared by the thread. |
| `note.replyto` | Direct parent Note ID: the specific review or submission being answered. |
| `note.content` | Invitation-defined fields, each normally wrapped as `{"value": ...}`. |
| `signatures` | Identity signing the Edit, constrained by the invitation and authenticated account. |
| `note.signatures` | Identity shown for the Note; may be supplied by the invitation. |

Read both the submission and intended parent. Check that the parent belongs to the
same forum. A displayed paper number is not a Note ID. Match the intended review by
ID and content, not by list position. Invitations can supply constant `forum`,
`replyto`, or permissions; omit inferred constants instead of overriding them.

## Read the invitation before preparing a payload

Read `client.get_invitation(invitation_id)` and inspect `invitation.edit` plus
`invitation.edit["note"]`. A Note Edit invitation is different from a parent
invitation that creates other invitations. Use the concrete author-accessible one.

Inspect these parts together:

1. `invitees`, `noninvitees`, availability dates, and reply limits.
2. Edit-level signatures/readers/writers and Note-level signatures/readers/writers.
3. `edit.note.forum`, `edit.note.replyto`, and any existing-note ID constraint.
4. `edit.note.content`: field names, types, required/optional status, lengths,
   enums/items, regexes, file constraints, and representation instructions.

`param` describes requested input. A literal value or `param.const` is fixed and
usually need not be supplied. Dollar expressions may be resolved by OpenReview's
inference system; do not send unresolved expressions as literal group IDs.
Validate supported constraints locally and preserve the full schema for inspection.
Do not treat a partial local validator as proof that every server rule will pass.

Names such as `comment`, `rebuttal`, `response`, `title`, or `pdf` are examples,
not universal fields. Identify the actual text/file field from the live schema.
If two fields or invitations are plausible, show their descriptions and resolve
the intended form before preparing a write.

## Identity and visibility

`readers` controls visibility; `nonreaders` excludes groups; `writers` controls
modification. These exist separately on an Edit and on its resulting Note. Passing
`readers=` to `post_note_edit()` sets Edit readers, not Note readers.
Content fields can also have their own reader restrictions.

Use the invitation's permitted author signature, which may be an anonymous author
group. Never assume the personal `~Name1` profile is the right public signature.
The invitation's own `signatures` identifies its creator; it is not the signature
to copy for an author response. An authenticated account must be entitled to use
the selected identity; a matching string or regex does not establish membership.

Preserve invitation-derived visibility. Do not default to `everyone`, copy the
submission's readers indiscriminately, or guess reviewer/AC groups. If a reader
choice is part of the form, establish the intended audience in the preview.
Check the resulting Note after posting, including inferred readers and signature.

## Text responses and PDF attachments

For text, preserve the approved Markdown/LaTeX exactly and measure the actual
field content against the current schema. Do not silently truncate. A PDF-only
rebuttal form must receive a PDF, not text in an invented field.

For an allowed attachment, use:

```python
uploaded_path = client.put_attachment(local_path, invitation_id, field_name)
note_content[field_name] = {"value": uploaded_path}
```

`put_attachment()` performs a remote write and returns the path to store in the
Note. It does not itself submit the author response. Prepare file hashes and
metadata without uploading during dry-run. Validate the actual file type/size and
venue requirements before upload; a local path is not an OpenReview attachment URL.
If an upload succeeds but the Note write fails, retain that returned path locally
for recovery instead of repeatedly uploading the same file.

## Authentication and MFA

Use a user-provided token/session or the user's own username/password through a
private local environment. Do not place secrets in commands, artifacts, logs, or
chat messages. Never scrape browser credential stores or reuse unrelated accounts.
Verify the authenticated profile before accessing private notes or applying edits.

The current official client supports `token=...` and username/password login.
Login can require MFA: current source handles authenticator codes, email OTP, and
passkey flows. Noninteractive login can raise `MfaRequiredException`.
Newer versions expose `mfa_code_provider` for TOTP; do not assume older installed
versions do. Delegate an interactive login/MFA step to the user when needed, then
resume with the authorized session. Do not request or store a TOTP seed, recovery
codes, or a permanent bypass. Session expiry and 401/403 errors require diagnosis,
not repeated writes or changes to account security settings.

## Deadlines and venue-specific restrictions

Invitations expose activation `cdate`, UI deadline `duedate`, and server expiry
`expdate` as Unix milliseconds. These dates can differ. Respect the stated venue
deadline; a later server expiry is not authorization for a late response.
Check for deletion, reply-count restrictions, and current author eligibility.

The platform supports many workflows: one response per paper, one per review,
discussion comments, confidential AC comments, and PDF responses. Do not infer
these rules from the conference name or a previous year. Re-check the current
venue's permitted response type, page/character limits, attachment/link rules,
anonymity requirements, and whether an existing response can still be edited.
If rules or permissions block a write, preserve the prepared draft and explain
the actual blocker instead of changing invitations or requesting elevated roles.

## Apply once, then verify

Use `post_note_edit(invitation=..., signatures=[...], note=Note(...))` after the
prepared target, content, identity and audience are authorized. For an update,
set `note.id` to the exact response ID and use its permitted edit invitation.
Do not overwrite unrelated content or submit a second response to simulate editing.

Current `post_note_edit()` returns a dictionary containing the Edit and nested
Note identifiers. Save the receipt without secrets. `await_process=True` waits
for server-side processing; processing can fail after an Edit has been created.
Read back the Note and compare its content, forum, parent, signature, and readers.
A request being accepted is not sufficient evidence of the final rendered result.

On a timeout, disconnect, or ambiguous server/process error, mark the outcome
unknown and reconcile before another create request. Use a returned Note/Edit ID
when available, or read the target forum/reply thread and compare the intended
parent, author signature, exact content, attachments, and time. Consult Note Edit
history/process logs only when the account may access them. If uncertainty remains,
report it; do not blindly retry. Local duplicate checks cannot guarantee atomic
deduplication across concurrent agents or browser submissions.

The SDK may retry selected transport failures internally; this is separate from
creating a new application-level submission after an uncertain result. Never add
an outer write-retry loop that can create a second response.
