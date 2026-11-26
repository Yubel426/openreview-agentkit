# OpenReview AgentKit

An agent skill and Python CLI for OpenReview author workflows: read reviews, inspect reply forms, prepare text or PDF responses, and submit or update an approved reply through the official [openreview-py SDK](https://github.com/openreview/openreview-py).

This is an independent project, not an official OpenReview product. Access and submission depend on your account's permissions and the venue's current forms and deadlines.

**Validation status:** 41 offline tests pass. Real-account write operations have **not** been validated against a live conference. Local validation and a successful preview do not establish that OpenReview will accept a submission.

## Quick start

Requires Python 3.9+ on Linux, macOS, or WSL, plus network access to the OpenReview API. Run these commands in the environment where your agent will execute the tool:

```bash
git clone https://github.com/Yubel426/openreview-agentkit.git
cd openreview-agentkit

python3 -m venv "$HOME/.venvs/openreview-agentkit"
source "$HOME/.venvs/openreview-agentkit/bin/activate"
python -m pip install -r requirements.txt

python scripts/openreview_tool.py doctor
python scripts/openreview_tool.py --help
```

`doctor` checks local dependencies without contacting OpenReview. Use the same Python environment for installation and subsequent commands. Operational commands return JSON; help prints command usage.

### Use with an agent

Give your agent [SKILL.md](SKILL.md) as the workflow instructions and let it invoke `python scripts/openreview_tool.py`. An agent host that supports directory-based skills can load this directory using its own skill installation mechanism. Resolve the script path from the installed skill directory, since a host may rename that directory.

## Authenticate privately

Check an existing session first:

```bash
python scripts/openreview_tool.py whoami
```

For first login, run the following yourself in a **private interactive terminal in the same environment as the agent**:

```bash
python scripts/openreview_tool.py login
python scripts/openreview_tool.py whoami
```

The tool prompts for your username and password, follows the SDK's MFA flow when required, and saves a private session. Do not put passwords, tokens, or TOTP secrets in chat or command arguments. A browser login or a session saved on your Mac does not automatically authenticate a separate cloud runtime.

For an environment with credentials already configured securely, the tool also supports `OPENREVIEW_TOKEN` or `OPENREVIEW_USERNAME` and `OPENREVIEW_PASSWORD`. Use `import-token` to validate and save an environment-provided token, or enter it through a hidden terminal prompt:

```bash
python scripts/openreview_tool.py import-token
```

Sessions and frozen drafts live outside the repository, under `~/.config/openreview-author` by default. Set `OR_AUTHOR_HOME` to choose another private directory. Session files use mode `0600`. `logout` removes this tool's local session; it does not revoke the remote token or clear environment credentials.

## Author response workflow

Replace the placeholders below with values read from your paper's actual OpenReview forum. A numeric submission number is not a forum ID.

### 1. Read the discussion and inspect the actual reply form

```bash
python scripts/openreview_tool.py pull --forum '<forum URL or ID>'

python scripts/openreview_tool.py invitations \
  --forum '<forum ID>' --replyto '<review ID>'

python scripts/openreview_tool.py inspect-invitation \
  --invitation '<exact invitation ID>'
```

`pull` returns the submission and all discussion notes visible to the authenticated account. Invitation discovery returns candidates and how they were found; it does not prove permission to submit. Inspect the actual form to determine the reply target, author signature, allowed readers, required fields, and deadline. Do not assume that a venue uses a particular `Author_Response`, `Rebuttal`, or `Official_Comment` invitation.

### 2. Prepare the exact response

For a form that defines a text field named `comment`:

```bash
python scripts/openreview_tool.py prepare \
  --forum '<forum ID>' \
  --replyto '<review ID>' \
  --invitation '<exact invitation ID>' \
  --signature '<permitted author signing group>' \
  --text-file '/absolute/path/reply.md' \
  --field comment
```

Use the form's actual field name when it differs. For multiple fields, use `--content-json /absolute/path/content.json`; plain values and API v2 `{"value": ...}` wrappers are supported. Use `--readers-json` when the form requires an explicit audience, and `--title` only when it defines a title field.

For a permitted PDF attachment, add `--attachment '<actual attachment field>=/absolute/path/rebuttal.pdf'`. Attachments can accompany text or be used alone for an attachment-only form. To update an existing author reply, add `--edit-note '<existing reply ID>'`.

`prepare` reads the relevant API state, validates supported form constraints, hashes attachments, and saves a frozen local draft. It does not upload files or post a response. Keep input files containing reviews or drafts outside this repository.

### 3. Preview, obtain approval, and submit

```bash
python scripts/openreview_tool.py preview --draft '<draft ID or path>'
```

Review the complete response, paper and reviewer target, signing identity, audience, and attachments with the author. After the author has explicitly approved that exact result, use the confirmation string returned by the preview:

```bash
python scripts/openreview_tool.py submit \
  --draft '<draft ID or path>' \
  --confirm '<exact confirmation string from preview>'
```

The confirmation string binds the prepared content; it is not a substitute for the author's authorization. `submit` rechecks the relevant state, uploads approved attachments, posts the response, and reads it back. Check both the write result and readback status before reporting success.

### 4. Reconcile an uncertain result

After a timeout, interruption, or unclear submission result, reconcile the existing draft before attempting another write:

```bash
python scripts/openreview_tool.py reconcile --draft '<draft ID or path>'
```

Reconciliation reads remote state without reposting. If the outcome remains ambiguous, inspect the forum before authorizing another attempt.

## Options and scope

Global flags go **before** the subcommand:

```bash
python scripts/openreview_tool.py \
  --session-file '/private/path/session.json' whoami

python scripts/openreview_tool.py \
  --output '/private/path/new-forum-export.json' \
  pull --forum '<forum ID>'
```

`--output` writes a private JSON result to a new file. An explicit session file takes precedence over environment credentials. The default API is `https://api2.openreview.net`; the official development host, `https://api2.dev.openreview.net`, requires matching credentials. Environment tokens default to the production host; set `OPENREVIEW_TOKEN_BASEURL` explicitly for a token issued by the development host.

This version handles API v2 author replies and comments, including supported text and attachment forms. API v1, original-paper submission, withdrawals, deletion, reviewer reports, and venue administration are outside its scope. A closed stage, missing author rights, or unsupported schema requires resolving the actual venue condition; the tool does not bypass it.

See [SKILL.md](SKILL.md) for the agent workflow and [API details](references/openreview-api.md) for schema handling, identity, attachments, and recovery limits.

## Run the offline tests

```bash
python -m unittest discover -s tests -v
```

The 41-test suite uses fake clients and local fixtures. It checks authentication boundaries, account and host binding, private state, form validation, draft integrity, submission/readback behavior, and uncertain-write recovery without logging in or publishing anything to OpenReview.
