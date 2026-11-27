#!/usr/bin/env python3
"""Agent-friendly OpenReview author commands. Help and doctor work offline."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import or_core as core


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="OpenReview author workflow: authenticate, read, prepare, approve, submit.",
        epilog="Commands return JSON. Only login/import-token may request terminal input. "
        "Only submit writes replies to OpenReview; prepare never uploads attachments.",
    )
    p.add_argument("--baseurl", default="https://api2.openreview.net", help="API v2 host")
    p.add_argument("--session-file", help="Private session JSON; default is outside the skill")
    p.add_argument("--output", help="Save the JSON result privately to a NEW file")
    s = p.add_subparsers(dest="command", required=True)
    s.add_parser("doctor", help="Check Python/SDK availability without network access")
    s.add_parser("setup", help="Install the pinned SDK into a private Python environment")
    a = s.add_parser("login", help="Log in interactively and save a private session")
    a.add_argument("--username", help="OpenReview login email; prompt when omitted")
    a.add_argument("--expires-in", type=int, default=86400)
    s.add_parser("import-token", help="Import OPENREVIEW_TOKEN or enter a token privately")
    s.add_parser("whoami", help="Verify the current session against the API")
    s.add_parser("submissions", help="List venue submissions for the signed-in profile")
    s.add_parser("logout", help="Remove this tool's local session, without remote changes")
    a = s.add_parser("pull", help="Read a paper and all replies visible to your account")
    a.add_argument("--forum", required=True, help="Forum ID or OpenReview forum URL")
    a.add_argument("--public", action="store_true", help="Read public information without a session by default")
    a.add_argument("--use-session", action="store_true", help="With --public, use an authorized session and return only public notes and fields")
    a = s.add_parser("invitations", help="Discover candidate reply forms; never auto-select")
    a.add_argument("--forum", required=True)
    a.add_argument("--replyto", help="Specific review/comment ID or URL")
    a = s.add_parser("inspect-invitation", help="Read a form's actual fields and permissions")
    a.add_argument("--invitation", required=True)
    a = s.add_parser("prepare", help="Validate and freeze a reply draft locally; no remote write")
    a.add_argument("--forum", required=True)
    a.add_argument("--invitation", required=True, help="Exact invitation ID, from inspection")
    a.add_argument("--replyto", help="Target note; inferred only when the form specifies a constant target")
    a.add_argument("--signature", help="Authorized author signing group required by the form")
    inputs = a.add_mutually_exclusive_group()
    inputs.add_argument("--text-file", help="UTF-8 file containing the exact final reply")
    inputs.add_argument("--content-json", help="JSON file with actual form content fields")
    a.add_argument("--field", help="Actual text field name; required with --text-file")
    a.add_argument("--title", help="Optional title, only if the form defines a title field")
    a.add_argument("--readers-json", help="JSON file with explicit allowed Note readers")
    a.add_argument("--attachment", action="append", default=[], metavar="FIELD=PATH",
                   help="Repeat for each form attachment; hashed locally, uploaded only on submit")
    a.add_argument("--edit-note", help="Existing author reply to update, in the same forum")
    a = s.add_parser("preview", help="Read the frozen draft and its required confirmation")
    a.add_argument("--draft", required=True, help="Draft ID or path returned by prepare")
    a = s.add_parser("submit", help="Submit the exact approved draft, then verify by reading back")
    a.add_argument("--draft", required=True)
    a.add_argument("--confirm", required=True, help="Exact confirmation string printed by preview")
    a = s.add_parser("reconcile", help="Read-only recovery of an uncertain previous submission")
    a.add_argument("--draft", required=True)
    return p


def load_content(args: argparse.Namespace) -> dict[str, Any]:
    if args.content_json:
        raw = core.read_json(Path(args.content_json).expanduser())
        if not isinstance(raw, dict):
            raise core.ToolError("INVALID_CONTENT", "Content JSON must be an object of form fields.")
        content = {}
        for key, value in raw.items():
            if isinstance(value, dict) and "value" in value:
                if set(value) - {"value", "readers"}:
                    raise core.ToolError("INVALID_CONTENT", "Wrapped fields support only value and readers.", {"field": key})
                content[key] = value
            else:
                content[key] = {"value": value}
    elif args.text_file:
        if not args.field:
            raise core.ToolError("FIELD_REQUIRED", "Inspect the invitation and pass its actual text field with --field.")
        try:
            body = Path(args.text_file).expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise core.ToolError("FILE_READ_ERROR", "Cannot read the UTF-8 reply file.", {"reason": type(exc).__name__}) from None
        content = {args.field: {"value": body}}
    else:
        content = {}
    if args.field and not args.text_file:
        raise core.ToolError("INVALID_ARGUMENTS", "--field is only used with --text-file.")
    if args.title is not None:
        if "title" in content:
            raise core.ToolError("DUPLICATE_FIELD", "Specify title once, in content JSON or --title.")
        content["title"] = {"value": args.title}
    if not content and not args.attachment:
        raise core.ToolError("EMPTY_REPLY", "Provide --text-file, --content-json, or an attachment.")
    return content


def attachment_map(values: list[str]) -> dict[str, str]:
    result = {}
    for value in values:
        key, sep, path = value.partition("=")
        if not sep or not key.strip() or not path.strip():
            raise core.ToolError("INVALID_ATTACHMENT", "Use --attachment FIELD=PATH.")
        if key in result:
            raise core.ToolError("DUPLICATE_ATTACHMENT", "An attachment field can only be specified once.", {"field": key})
        result[key] = str(Path(path).expanduser().resolve())
    return result


def setup_runtime() -> dict[str, Any]:
    """Set up the installed skill without importing the SDK or logging in."""
    import fcntl

    state = core.state_dir()
    environment = state / "runtime"
    python = environment / "bin" / "python"
    requirements = Path(__file__).resolve().parents[1] / "requirements.txt"
    # The setup subprocesses only need package installation settings.
    env = {name: value for name, value in os.environ.items() if not name.startswith("OPENREVIEW_")}
    options = {"env": env, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "check": True}
    step = "venv"
    try:
        with (state / "runtime.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not python.is_file() or not (environment / "pyvenv.cfg").is_file():
                subprocess.run([sys.executable, "-m", "venv", str(environment)], **options)
            elif subprocess.run([str(python), "-m", "pip", "--version"], **{**options, "check": False}).returncode:
                subprocess.run([str(python), "-m", "ensurepip", "--upgrade"], **options)
            step = "pip"
            subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                            "--no-input", "-r", str(requirements)], **options)
    except (OSError, subprocess.CalledProcessError) as exc:
        message = ("Could not create the private Python environment. Check that this Python includes venv and pip."
                   if step == "venv" else
                   "Could not install the OpenReview SDK. Check Python's package index and network access.")
        raise core.ToolError("SETUP_FAILED", message, {"step": step}) from exc
    return {"python_executable": str(python), "cli": str(Path(__file__).resolve())}


def sdk_status() -> dict[str, Any]:
    try:
        version = importlib.metadata.version("openreview-py")
    except importlib.metadata.PackageNotFoundError:
        version = None
    usable = False
    if version is not None:
        try:
            sdk = core.api_call(core.load_sdk)
            usable = callable(getattr(getattr(sdk, "api", None), "OpenReviewClient", None))
        except core.ToolError:
            pass
    return {"python": sys.version.split()[0], "sdk_version": version,
            "sdk_installed": version is not None, "sdk_usable": usable}


def inspect_managed_runtime(python: Path) -> dict[str, Any]:
    details = {"path": str(python), "exists": python.is_file(), "runnable": False}
    if not details["exists"]:
        return details
    probe = "import json; from openreview_tool import sdk_status; print(json.dumps(sdk_status()))"
    try:
        result = subprocess.run([str(python), "-c", probe], cwd=Path(__file__).resolve().parent,
                                check=True, capture_output=True, text=True, timeout=10)
        status = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return details
    return {**details, **status, "runnable": True} if isinstance(status, dict) else details


def doctor() -> dict[str, Any]:
    state = core.state_dir()
    current = sdk_status()
    managed = inspect_managed_runtime(state / "runtime" / "bin" / "python")
    runtime = managed["path"] if managed.get("sdk_usable") else None
    recommended = runtime or (sys.executable if current["sdk_usable"] else None)
    return {**current, "current_python": sys.executable,
            "managed_runtime": managed, "runtime_python": runtime,
            "python_executable": recommended,
            "next_action": "use_python" if recommended else "setup",
            "setup_command": None if recommended else [sys.executable, str(Path(__file__).resolve()), "setup"],
            "install": "python -m pip install -r <skill-dir>/requirements.txt",
            "state_directory": str(state), "network_used": False}


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "setup":
        return setup_runtime()
    if args.command == "doctor":
        return doctor()
    kwargs = {"baseurl": args.baseurl, "session_file": args.session_file}
    if args.command == "login":
        return core.login(username=args.username, expires_in=args.expires_in, **kwargs)
    if args.command == "import-token":
        return core.import_token(**kwargs)
    if args.command == "logout":
        return core.logout(session_file=args.session_file)
    if args.command == "whoami":
        return core.whoami(**kwargs)
    if args.command == "preview":
        from or_replies import preview_reply
        return preview_reply(args.draft)
    if args.command == "pull" and args.public:
        if args.use_session:
            return core.read_forum(core.get_client(**kwargs), args.forum, public_only=True)
        if args.session_file:
            raise core.ToolError("INVALID_ARGUMENTS", "Add --use-session to use the selected session with --public.")
        return core.read_forum(core.get_public_client(args.baseurl), args.forum)
    if args.command == "pull" and args.use_session:
        raise core.ToolError("INVALID_ARGUMENTS", "--use-session applies to --public reads.")
    # Validate local inputs before attempting authentication or any remote call.
    if args.command == "prepare":
        content = load_content(args)
        attachments = attachment_map(args.attachment)
        readers = core.read_json(Path(args.readers_json).expanduser()) if args.readers_json else None
        if readers is not None and (not isinstance(readers, list) or
                                    any(not isinstance(x, str) or not x for x in readers)):
            raise core.ToolError("INVALID_READERS", "Readers must be a JSON list of nonempty group IDs.")
    client = core.get_client(**kwargs)
    if args.command == "submissions":
        return core.list_submissions(client)
    if args.command == "pull":
        return core.read_forum(client, args.forum)
    if args.command == "invitations":
        return core.list_reply_invitations(client, args.forum, replyto=args.replyto)
    if args.command == "inspect-invitation":
        return core.inspect_invitation(client, args.invitation)
    if args.command == "prepare":
        from or_replies import prepare_reply
        return prepare_reply(
            client, forum_id=args.forum, invitation_id=args.invitation,
            content=content, replyto=args.replyto, signature=args.signature,
            readers=readers, attachments=attachments,
            edit_note_id=args.edit_note,
        )
    if args.command == "submit":
        from or_replies import submit_reply
        return submit_reply(client, args.draft, args.confirm, baseurl=args.baseurl)
    if args.command == "reconcile":
        from or_replies import reconcile_reply
        return reconcile_reply(client, args.draft)
    raise core.ToolError("UNKNOWN_COMMAND", "Unsupported command.")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        core.normalize_baseurl(args.baseurl)
        output = Path(args.output).expanduser().absolute() if args.output else None
        if output is not None and (output.exists() or output.is_symlink()):
            raise core.ToolError("FILE_EXISTS", "Choose a new --output path before running this operation.")
        result = {"ok": True, "command": args.command, "data": run(args)}
        if output is not None:
            try:
                core.write_private_json(output, result, overwrite=False)
                result = {"ok": True, "command": args.command, "output_file": str(output)}
            except core.ToolError as exc:
                # A response may already have been accepted. Preserve its receipt
                # in stdout instead of reporting the completed operation as failed.
                result["output_error"] = {"code": exc.code, "message": exc.message}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except core.ToolError as exc:
        result = {"ok": False, "error": {"code": exc.code, "message": exc.message}}
        if exc.details is not None:
            result["error"]["details"] = exc.details
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": {"code": "INTERRUPTED", "message": "Operation interrupted; reconcile any pending submission before retrying."}}))
        return 130
    except Exception as exc:
        # SDK errors can contain request details; do not print their raw repr/traceback.
        safe = core.classify_api_error(exc)
        print(json.dumps({"ok": False, "error": {"code": safe.code, "message": safe.message}}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
