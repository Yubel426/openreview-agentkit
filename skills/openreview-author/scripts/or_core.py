"""Authentication, private local state and read-only OpenReview API v2 helpers.

The SDK is imported lazily. Importing this module never logs in or accesses the
network. Credentials are never returned by the public command helpers.
"""
from __future__ import annotations

import base64
import contextlib
import getpass
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse


DEFAULT_BASEURL = "https://api2.openreview.net"
ALLOWED_BASEURLS = frozenset({DEFAULT_BASEURL, "https://api2.dev.openreview.net"})
_SKILL_ROOT = Path(__file__).resolve().parent.parent
_CHECKOUT_ROOT = _SKILL_ROOT.parent.parent
_PRIVATE_DATA_BOUNDARY = (
    _CHECKOUT_ROOT if _SKILL_ROOT.parent.name == "skills"
    and (_CHECKOUT_ROOT / "scripts" / "package_skill.py").is_file() else _SKILL_ROOT
)
_MAX_JSON_BYTES = 16 * 1024 * 1024
_NOTE_ID = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_SESSION_KEYS = {
    "schema_version", "baseurl", "token", "profile_id", "created_at", "expires_at"
}


class ToolError(Exception):
    """An error with an intentionally non-secret, machine-readable description."""

    def __init__(self, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details if details is not None else {}

    def to_json(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


def load_sdk():
    try:
        return importlib.import_module("openreview")
    except ImportError:
        raise ToolError(
            "DEPENDENCY_MISSING",
            "Install the openreview-py package in the Python environment used by this command.",
        ) from None


def normalize_baseurl(baseurl: str = DEFAULT_BASEURL) -> str:
    if not isinstance(baseurl, str):
        raise ToolError("INVALID_BASEURL", "An OpenReview API v2 base URL is required.")
    value = baseurl.rstrip("/")
    if value not in ALLOWED_BASEURLS:
        raise ToolError(
            "INVALID_BASEURL",
            "Only the official production and development OpenReview API v2 hosts are supported.",
            {"allowed_baseurls": sorted(ALLOWED_BASEURLS)},
        )
    return value


def obj_json(obj: Any) -> Any:
    """Convert an SDK entity or ordinary JSON value without repr()/secret fallbacks."""
    if obj is None or isinstance(obj, (str, bool, int, float)):
        return obj
    if isinstance(obj, dict):
        if not all(isinstance(k, str) for k in obj):
            raise ToolError("INVALID_JSON", "JSON object keys must be strings.")
        return {k: obj_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [obj_json(v) for v in obj]
    if callable(getattr(obj, "to_json", None)):
        return obj_json(obj.to_json())
    raise ToolError("INVALID_JSON", "The value is not a supported JSON or OpenReview entity.")


def canonical_hash(value: Any) -> str:
    try:
        encoded = json.dumps(
            obj_json(value), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ToolError("INVALID_JSON", "The value cannot be encoded as canonical JSON.") from None
    return hashlib.sha256(encoded).hexdigest()


def _safe_path(path: Any, *, private: bool = False) -> Path:
    try:
        value = Path(path).expanduser().absolute()
    except (TypeError, ValueError, OSError):
        raise ToolError("INVALID_PATH", "A valid local filesystem path is required.") from None
    # Check every existing path component rather than silently following symlinks.
    for component in reversed((value, *value.parents)):
        try:
            if stat.S_ISLNK(component.lstat().st_mode):
                raise ToolError("UNSAFE_PATH", "Symbolic links are not accepted for local state files.")
        except FileNotFoundError:
            continue
        except OSError:
            raise ToolError("LOCAL_IO_ERROR", "The local path cannot be inspected.") from None
    value = value.resolve(strict=False)
    if private and (value == _PRIVATE_DATA_BOUNDARY or _PRIVATE_DATA_BOUNDARY in value.parents):
        raise ToolError(
            "UNSAFE_PATH", "Private session and response data must be stored outside the skill directory."
        )
    return value


def state_dir() -> Path:
    path = _safe_path(
        os.environ.get("OR_AUTHOR_HOME", str(Path.home() / ".config" / "openreview-author")),
        private=True,
    )
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = path.stat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise ToolError("UNSAFE_PATH", "The private state directory must be owned by the current user.")
        path.chmod(0o700)
    except ToolError:
        raise
    except OSError:
        raise ToolError("LOCAL_IO_ERROR", "Cannot create or secure the private state directory.") from None
    return path


def _open_json(path: Any, *, require_private: bool = False) -> Any:
    value = _safe_path(path, private=require_private)
    fd = None
    try:
        fd = os.open(value, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ToolError("UNSAFE_PATH", "The JSON input must be a regular file.")
        if require_private and (st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600):
            raise ToolError(
                "INSECURE_SESSION_FILE", "The session file must be owned by you and have mode 0600."
            )
        if st.st_size > _MAX_JSON_BYTES:
            raise ToolError("INVALID_JSON", "The JSON input exceeds the 16 MiB size limit.")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = None
            raw = handle.read(_MAX_JSON_BYTES + 1)
        if len(raw.encode("utf-8")) > _MAX_JSON_BYTES:
            raise ToolError("INVALID_JSON", "The JSON input exceeds the 16 MiB size limit.")

        def reject_constant(_):
            raise ValueError("Non-finite JSON number")

        return json.loads(raw, parse_constant=reject_constant)
    except ToolError:
        raise
    except FileNotFoundError:
        raise ToolError("FILE_NOT_FOUND", "The requested local JSON file does not exist.") from None
    except (ValueError, UnicodeError):
        raise ToolError("INVALID_JSON", "The local file is not valid UTF-8 JSON.") from None
    except OSError:
        raise ToolError("LOCAL_IO_ERROR", "The local JSON file cannot be read.") from None
    finally:
        if fd is not None:
            os.close(fd)


def read_json(path: Any) -> Any:
    return _open_json(path)


def write_private_json(path: Any, data: Any, overwrite: bool = False) -> Path:
    """Atomically publish a 0600 JSON file, without clobbering by default."""
    value = _safe_path(path, private=True)
    try:
        body = (json.dumps(obj_json(data), ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    except (ValueError, TypeError):
        raise ToolError("INVALID_JSON", "The value cannot be written as JSON.") from None
    parent_fd = None
    temp_name = ".or-author-" + secrets.token_hex(16) + ".tmp"
    try:
        value.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _safe_path(value, private=True)
        parent_fd = os.open(value.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        if os.fstat(parent_fd).st_uid != os.getuid():
            raise ToolError("UNSAFE_PATH", "The private output directory must be owned by the current user.")
        fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(temp_name, value.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        else:
            # link() is atomic and fails if the destination already exists.
            os.link(temp_name, value.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
            os.unlink(temp_name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        return value
    except ToolError:
        raise
    except FileExistsError:
        raise ToolError("FILE_EXISTS", "The output file already exists; it was not replaced.") from None
    except OSError:
        raise ToolError("LOCAL_IO_ERROR", "Cannot write the private JSON file atomically.") from None
    finally:
        if parent_fd is not None:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass
            os.close(parent_fd)


def classify_api_error(exc: Exception) -> ToolError:
    """Classify an SDK error without returning server bodies or exception strings."""
    if isinstance(exc, ToolError):
        return exc
    name = type(exc).__name__
    text = str(exc).lower()
    payload = exc.args[0] if getattr(exc, "args", ()) and isinstance(exc.args[0], dict) else {}
    status = getattr(getattr(exc, "response", None), "status_code", None)
    status = payload.get("status", payload.get("statusCode", status))
    details = {"exception_type": name}
    if isinstance(status, int):
        details["http_status"] = status
    if "mfa" in name.lower() or "mfa" in text or "multi-factor" in text:
        return ToolError("MFA_REQUIRED", "Complete OpenReview MFA with the login command in your local terminal.", details)
    if "challenge" in text or "captcha" in text or "human verification" in text:
        return ToolError("HUMAN_VERIFICATION_REQUIRED", "OpenReview requires human verification; complete it through OpenReview's normal interface.", details)
    if "expired" in text and any(s in text for s in ("token", "session", "jwt")):
        return ToolError("AUTH_EXPIRED", "The OpenReview session has expired; log in again.", details)
    if status == 401 or any(s in text for s in (
        "invalid token", "invalid password", "invalid username or password",
        "invalid credentials", "login required", "jwt malformed", "unauthenticated",
    )):
        return ToolError("AUTH_REQUIRED", "OpenReview did not accept the credentials; log in again.", details)
    if status == 403 or any(s in text for s in ("forbidden", "not authorized", "permission denied", "not allowed")):
        return ToolError("FORBIDDEN", "The current OpenReview account does not have permission for this operation.", details)
    if status == 404 or "notfound" in text or "not found" in text or isinstance(exc, IndexError):
        return ToolError("NOT_FOUND", "The requested OpenReview resource was not found or is not visible to this account.", details)
    if status == 429 or "rate limit" in text or "too many requests" in text:
        return ToolError("RATE_LIMITED", "OpenReview rate-limited this request; wait before trying again.", details)
    if isinstance(exc, (TimeoutError, ConnectionError)) or any(s in name.lower() for s in ("timeout", "connection", "proxy", "ssl", "retry")):
        return ToolError("NETWORK_ERROR", "The OpenReview request failed at the network layer.", details)
    return ToolError("API_ERROR", "OpenReview could not complete the request. Inspect the invitation and your account's access before retrying.", details)


def api_call(fn: Callable, *args, **kwargs):
    """Keep SDK retry/debug output out of the CLI's structured, non-secret output."""
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return fn(*args, **kwargs)
    except Exception as exc:
        raise classify_api_error(exc) from None


def _session_path(session_file=None) -> Path:
    return _safe_path(session_file, private=True) if session_file is not None else state_dir() / "session.json"


def _normalize_token(token: Any) -> str:
    if not isinstance(token, str):
        raise ToolError("INVALID_SESSION", "The session token must be a non-empty string.")
    token = token.strip()
    if token.startswith("Bearer "):
        token = token[7:].strip()
    if not token or len(token) > 32768 or any(c.isspace() for c in token):
        raise ToolError("INVALID_SESSION", "The session token has an invalid format.")
    return token


def _token_claims(token: str) -> dict:
    """Read expiry/identity hints only; remote validation remains mandatory."""
    try:
        part = token.split(".")[1]
        decoded = base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
        claims = json.loads(decoded)
        return claims if isinstance(claims, dict) else {}
    except (IndexError, ValueError, UnicodeError):
        return {}


def _check_expiry(token: str, stored_expiry=None) -> float | None:
    exp = _token_claims(token).get("exp")
    values = [v for v in (exp, stored_expiry) if isinstance(v, (int, float)) and not isinstance(v, bool)]
    expiry = min(values) if values else None
    if expiry is not None and expiry <= time.time():
        raise ToolError("AUTH_EXPIRED", "The OpenReview session has expired; log in again.")
    return expiry


@contextlib.contextmanager
def _without_password_env():
    # The SDK automatically reads these variables during construction. Explicit
    # login below ensures that no implicit login occurs before MFA policy is set.
    saved = {k: os.environ.pop(k) for k in ("OPENREVIEW_USERNAME", "OPENREVIEW_PASSWORD") if k in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


def _empty_client(baseurl):
    sdk = load_sdk()
    with _without_password_env():
        client = api_call(sdk.api.OpenReviewClient, baseurl=baseurl)
    # No implicit write retries. A timed-out write must be reconciled first.
    try:
        from urllib3.util.retry import Retry
        for adapter in client.session.adapters.values():
            if hasattr(adapter, "max_retries"):
                adapter.max_retries = Retry(total=0, connect=0, read=0, redirect=0, status=0)
    except (AttributeError, ImportError):
        pass  # Pure fake clients used by the offline tests have no HTTP session.
    return client


@contextlib.contextmanager
def _mfa_policy(interactive: bool):
    """Use the SDK MFA implementation, restricting prompts to a real terminal."""
    sdk = load_sdk()
    module = getattr(sdk, "mfa", None)
    if module is None:
        yield
        return
    original = {}
    replacements = {
        "_is_interactive": lambda: bool(interactive and sys.stdin.isatty() and sys.stderr.isatty()),
        "_default_mfa_code_prompt": lambda method: getpass.getpass("OpenReview verification code: ", stream=sys.stderr).strip(),
    }
    try:
        for name, replacement in replacements.items():
            if hasattr(module, name):
                original[name] = getattr(module, name)
                setattr(module, name, replacement)
        yield
    finally:
        for name, value in original.items():
            setattr(module, name, value)


def _login_client(username, password, baseurl, expires_in, interactive=False):
    client = _empty_client(baseurl)
    try:
        with _mfa_policy(interactive):
            if interactive:
                # SDK prompts go to stderr; no login result/token is printed.
                with contextlib.redirect_stdout(sys.stderr):
                    client.login_user(username=username, password=password, expiresIn=expires_in)
            else:
                api_call(client.login_user, username=username, password=password, expiresIn=expires_in)
    except Exception as exc:
        raise classify_api_error(exc) from None
    client._or_author_auth_source = "login"
    client._or_author_expires_at = _check_expiry(_normalize_token(getattr(client, "token", None)))
    _validate_auth(client)
    return client


def _identity_hint(client) -> str | None:
    cached = getattr(client, "profile", None)
    identity = cached.get("id") if isinstance(cached, dict) else getattr(cached, "id", None)
    if identity:
        return identity
    user = getattr(client, "user", {})
    if isinstance(user, dict):
        profile = user.get("profile", {})
        if isinstance(profile, dict) and profile.get("id"):
            return profile["id"]
        if user.get("id"):
            return user["id"]
    claims = _token_claims(getattr(client, "token", ""))
    user = claims.get("user", claims)
    if isinstance(user, dict):
        profile = user.get("profile", {})
        if isinstance(profile, dict) and profile.get("id"):
            return profile["id"]
        return user.get("id")
    return None


def _validate_auth(client) -> str:
    token = _normalize_token(getattr(client, "token", None))
    _check_expiry(token, getattr(client, "_or_author_expires_at", None))
    identity = _identity_hint(client)
    if not isinstance(identity, str) or not identity:
        raise ToolError("AUTH_REQUIRED", "Cannot determine the account identity from this session; log in again.")
    result = api_call(client.get_profile, identity)
    data = obj_json(result)
    actual = data.get("id") if isinstance(data, dict) else None
    if not isinstance(actual, str) or not actual.startswith("~"):
        raise ToolError("AUTH_REQUIRED", "The authenticated account has no usable OpenReview profile.")
    if identity.startswith("~") and actual != identity:
        raise ToolError("AUTH_REQUIRED", "OpenReview returned a different account identity than the session expected.")
    client.profile = result
    client._or_author_verified_profile_id = actual
    return actual


def profile_id(client) -> str:
    verified = getattr(client, "_or_author_verified_profile_id", None)
    if isinstance(verified, str) and verified.startswith("~"):
        _check_expiry(_normalize_token(getattr(client, "token", None)), getattr(client, "_or_author_expires_at", None))
        return verified
    return _validate_auth(client)


def _client_from_token(token, baseurl, source, expected_profile=None, expires_at=None):
    token = _normalize_token(token)
    expiry = _check_expiry(token, expires_at)
    client = _empty_client(baseurl)
    # These are the same public fields initialized by OpenReviewClient(token=...).
    # Setting them after construction avoids its broad exception-swallowing path.
    client.token = token
    client.headers["Authorization"] = "Bearer " + token
    claims = _token_claims(token)
    client.user = claims.get("user", claims)
    if not _identity_hint(client) and expected_profile:
        client.user = {"profile": {"id": expected_profile}}
    client._or_author_auth_source = source
    client._or_author_expires_at = expiry
    actual = _validate_auth(client)
    if expected_profile and actual != expected_profile:
        raise ToolError("INVALID_SESSION", "The saved session profile does not match the token's account.")
    return client


def _read_session(path, baseurl):
    data = _open_json(path, require_private=True)
    if not isinstance(data, dict) or set(data) != _SESSION_KEYS or type(data.get("schema_version")) is not int or data.get("schema_version") != 1:
        raise ToolError("INVALID_SESSION", "The file is not an openreview-author version 1 session JSON.")
    if data.get("baseurl") != baseurl:
        raise ToolError("SESSION_HOST_MISMATCH", "This session belongs to another OpenReview host; use a separate session for this host.")
    if not isinstance(data.get("profile_id"), str) or not data["profile_id"].startswith("~"):
        raise ToolError("INVALID_SESSION", "The saved session does not contain a valid profile identity.")
    for key in ("created_at", "expires_at"):
        value = data[key]
        if value is None and key == "expires_at":
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ToolError("INVALID_SESSION", "The saved session has invalid time metadata.")
    data["token"] = _normalize_token(data["token"])
    return data


def get_client(baseurl=DEFAULT_BASEURL, session_file=None):
    baseurl = normalize_baseurl(baseurl)
    # An explicit file selection is an account choice. Never silently replace
    # it with credentials inherited from the surrounding shell environment.
    if session_file is not None:
        path = _session_path(session_file)
        data = _read_session(path, baseurl)
        return _client_from_token(data["token"], baseurl, "session_file", data["profile_id"], data["expires_at"])
    env_token = os.environ.get("OPENREVIEW_TOKEN")
    if env_token:
        bound_host = normalize_baseurl(os.environ.get("OPENREVIEW_TOKEN_BASEURL", DEFAULT_BASEURL))
        if bound_host != baseurl:
            raise ToolError("SESSION_HOST_MISMATCH", "OPENREVIEW_TOKEN is bound to a different host. Set OPENREVIEW_TOKEN_BASEURL for that token's actual host.")
        return _client_from_token(env_token, baseurl, "environment_token")
    path = _session_path(session_file)
    if path.exists():
        data = _read_session(path, baseurl)
        return _client_from_token(data["token"], baseurl, "session_file", data["profile_id"], data["expires_at"])
    username = os.environ.get("OPENREVIEW_USERNAME")
    password = os.environ.get("OPENREVIEW_PASSWORD")
    if username and password:
        return _login_client(username, password, baseurl, 86400, interactive=False)
    raise ToolError("AUTH_REQUIRED", "Log in, import a token, or set OPENREVIEW_TOKEN (or both OPENREVIEW_USERNAME and OPENREVIEW_PASSWORD).")


def get_public_client(baseurl=DEFAULT_BASEURL):
    return _empty_client(normalize_baseurl(baseurl))


def whoami(client=None, baseurl=DEFAULT_BASEURL, session_file=None) -> dict:
    client = client if client is not None else get_client(baseurl, session_file)
    actual = _validate_auth(client)
    return {
        "authenticated": True,
        "profile_id": actual,
        "baseurl": normalize_baseurl(client.baseurl),
        "auth_source": getattr(client, "_or_author_auth_source", "provided_client"),
        "expires_at": getattr(client, "_or_author_expires_at", None),
    }


def _save_session(client, session_file=None, expires_in=None) -> dict:
    now = time.time()
    token = _normalize_token(client.token)
    expiry = _check_expiry(token, getattr(client, "_or_author_expires_at", None))
    if expiry is None and expires_in is not None:
        expiry = now + expires_in
    path = _session_path(session_file)
    data = {
        "schema_version": 1,
        "baseurl": normalize_baseurl(client.baseurl),
        "token": token,
        "profile_id": profile_id(client),
        "created_at": now,
        "expires_at": expiry,
    }
    write_private_json(path, data, overwrite=True)
    return {"authenticated": True, "profile_id": data["profile_id"], "baseurl": data["baseurl"], "session_file": str(path), "expires_at": expiry}


def login(username=None, expires_in=86400, session_file=None, baseurl=DEFAULT_BASEURL) -> dict:
    baseurl = normalize_baseurl(baseurl)
    if isinstance(expires_in, bool) or not isinstance(expires_in, int) or not 1 <= expires_in <= 604800:
        raise ToolError("INVALID_ARGUMENT", "Session lifetime must be an integer from 1 to 604800 seconds.")
    interactive = bool(sys.stdin.isatty() and sys.stderr.isatty())
    username = username or os.environ.get("OPENREVIEW_USERNAME")
    password = os.environ.get("OPENREVIEW_PASSWORD")
    if not username:
        if not interactive:
            raise ToolError("AUTH_REQUIRED", "Supply OPENREVIEW_USERNAME or run login from your own interactive terminal.")
        print("OpenReview username/email: ", end="", file=sys.stderr, flush=True)
        username = input().strip()
    if not password:
        if not interactive:
            raise ToolError("INTERACTIVE_LOGIN_REQUIRED", "Enter your password using login in your own interactive terminal, or use an environment-provided credential.")
        password = getpass.getpass("OpenReview password: ", stream=sys.stderr)
    if not username or not password:
        raise ToolError("AUTH_REQUIRED", "OpenReview username and password cannot be empty.")
    client = _login_client(username, password, baseurl, expires_in, interactive)
    return _save_session(client, session_file, expires_in)


def import_token(token=None, session_file=None, baseurl=DEFAULT_BASEURL) -> dict:
    """Validate and store a token; callers must never pass it as a shell argument."""
    baseurl = normalize_baseurl(baseurl)
    token_from_env = token is None and bool(os.environ.get("OPENREVIEW_TOKEN"))
    token = token if token is not None else os.environ.get("OPENREVIEW_TOKEN")
    if token_from_env:
        host = normalize_baseurl(os.environ.get("OPENREVIEW_TOKEN_BASEURL", DEFAULT_BASEURL))
        if host != baseurl:
            raise ToolError("SESSION_HOST_MISMATCH", "OPENREVIEW_TOKEN_BASEURL does not match the selected host.")
    if token is None:
        if not (sys.stdin.isatty() and sys.stderr.isatty()):
            raise ToolError("AUTH_REQUIRED", "Set OPENREVIEW_TOKEN or import a token through a local terminal prompt.")
        token = getpass.getpass("OpenReview session token (hidden): ", stream=sys.stderr)
    client = _client_from_token(token, baseurl, "imported_token")
    return _save_session(client, session_file)


def logout(session_file=None) -> dict:
    path = _safe_path(_session_path(session_file), private=True)
    removed = False
    try:
        if path.exists():
            if not path.is_file():
                raise ToolError("UNSAFE_PATH", "The session path is not a regular file.")
            data = _open_json(path, require_private=True)
            if not isinstance(data, dict) or set(data) != _SESSION_KEYS or type(data.get("schema_version")) is not int or data.get("schema_version") != 1:
                raise ToolError("INVALID_SESSION", "Refusing to delete a file that is not an openreview-author session.")
            path.unlink()
            removed = True
    except OSError:
        raise ToolError("LOCAL_IO_ERROR", "Cannot remove the local session file.") from None
    return {"logged_out_locally": True, "removed": removed, "session_file": str(path), "remote_session_revoked": False, "environment_credentials_unchanged": True}


def parse_note_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolError("INVALID_NOTE_ID", "Provide an OpenReview note ID or forum/note URL.")
    text = value.strip()
    if "://" in text:
        parsed = urlparse(text)
        if parsed.scheme != "https" or parsed.netloc not in {"openreview.net", "www.openreview.net", "dev.openreview.net"} or parsed.path.rstrip("/") not in {"/forum", "/note"}:
            raise ToolError("INVALID_NOTE_ID", "Only official OpenReview forum and note URLs are accepted.")
        ids = parse_qs(parsed.query).get("id", [])
        if len(ids) != 1:
            raise ToolError("INVALID_NOTE_ID", "The OpenReview URL must contain exactly one id parameter.")
        text = ids[0]
    if not _NOTE_ID.fullmatch(text):
        raise ToolError("INVALID_NOTE_ID", "The OpenReview note ID has an invalid format.")
    return text


def list_submissions(client) -> dict:
    """List API v2 venue submissions attributed to the authenticated profile."""
    account = profile_id(client)
    site = "https://openreview.net" if normalize_baseurl(client.baseurl) == DEFAULT_BASEURL else "https://dev.openreview.net"
    submissions = {}
    for author_field in ("authorids", "authors.username"):
        for obj in api_call(client.get_all_notes, content={author_field: account}):
            note = obj_json(obj)
            forum_id = note.get("id")
            if not forum_id or note.get("forum") != forum_id or note.get("replyto") or note.get("ddate"):
                continue
            content = note.get("content", {})
            title = content.get("title", {}).get("value")
            venue_id = content.get("venueid", {}).get("value")
            invitations = note.get("invitations", [])
            actions = {invitation.rsplit("/-/", 1)[-1].casefold() for invitation in invitations}
            if not title or "record" in actions or not (note.get("number") or venue_id or "submission" in actions):
                continue
            submissions[forum_id] = {
                "forum_id": forum_id, "url": site + "/forum?id=" + forum_id, "title": title,
                "number": note.get("number"), "domain": note.get("domain"),
                "venue": content.get("venue", {}).get("value"), "venue_id": venue_id,
                "submitted_at": note.get("cdate") or note.get("tcdate"),
            }
    return {"profile_id": account,
            "submissions": sorted(submissions.values(), key=lambda note: (note["submitted_at"] or 0, note["forum_id"]), reverse=True),
            "scope": "Visible OpenReview API v2 venue submissions attributed to this profile, newest first across stages. Venue labels are reported as supplied by OpenReview."}


def _note_type(note: dict) -> str:
    invitations = note.get("invitations") or ([note["invitation"]] if note.get("invitation") else [])
    segments = {re.sub(r"[^a-z0-9]", "", part.lower()) for inv in invitations if isinstance(inv, str) for part in inv.split("/")}
    if any("rebuttal" in s or s in {"authorresponse", "authorreply"} for s in segments):
        return "author_response"
    if any("metareview" in s for s in segments):
        return "meta_review"
    if "decision" in segments:
        return "decision"
    if segments.intersection({"officialreview", "review"}):
        return "review"
    if segments.intersection({"officialcomment", "comment"}):
        return "comment"
    return "other"


def _public_note(note: dict) -> dict | None:
    if "everyone" not in (note.get("readers") or []) or note.get("nonreaders"):
        return None
    fields = ("id", "forum", "replyto", "cdate", "tcdate", "number", "invitations", "signatures", "readers")
    public = {key: note[key] for key in fields if key in note}
    public["content"] = {
        key: field for key, field in (note.get("content") or {}).items()
        if isinstance(field, dict) and not field.get("nonreaders")
        and ("readers" not in field or "everyone" in (field["readers"] or []))
    }
    return public


def read_forum(client, forum_id, *, public_only=False) -> dict:
    requested = parse_note_id(forum_id)
    requested_note = obj_json(api_call(client.get_note, requested))
    root_id = requested_note.get("forum") or requested_note.get("id")
    if not root_id:
        raise ToolError("API_ERROR", "The returned note does not identify its forum.")
    submission = requested_note if requested_note.get("id") == root_id else obj_json(api_call(client.get_note, root_id))
    if public_only:
        submission = _public_note(submission)
        if submission is None or _public_note(requested_note) is None:
            raise ToolError("NOT_FOUND", "The requested note or its forum is not publicly readable.")
    notes = api_call(lambda: list(client.get_all_notes(forum=root_id)))
    replies = []
    for obj in notes:
        note = obj_json(obj)
        if note.get("id") == root_id:
            continue
        if public_only:
            note = _public_note(note)
            if note is None:
                continue
        note["note_type"] = _note_type(note)
        replies.append(note)
    replies.sort(key=lambda n: (n.get("cdate") or n.get("tcdate") or 0, n.get("id") or ""))
    return {
        "forum_id": root_id,
        "requested_note_id": requested,
        "submission": submission,
        "replies": replies,
        "review_notes": [n for n in replies if n["note_type"] == "review"],
        "meta_review_notes": [n for n in replies if n["note_type"] == "meta_review"],
        "scope": ("Only notes and content fields marked public without reader exclusions."
                  if public_only else "All notes returned by get_all_notes(forum=...) that are visible to the current API client."),
        "classification_method": "note_type is a display hint inferred from invitation names, with author responses and meta-reviews checked before ordinary reviews; raw invitation IDs are retained.",
    }


def inspect_invitation(client, invitation_id) -> dict:
    if not isinstance(invitation_id, str) or not invitation_id.strip() or len(invitation_id) > 2048 or any(c.isspace() for c in invitation_id):
        raise ToolError("INVALID_INVITATION", "Provide the exact OpenReview invitation ID.")
    return obj_json(api_call(client.get_invitation, invitation_id))


def list_reply_invitations(client, forum_id, replyto=None) -> dict:
    requested = parse_note_id(forum_id)
    root = obj_json(api_call(client.get_note, requested))
    root_id = root.get("forum") or root.get("id")
    if not root_id:
        raise ToolError("API_ERROR", "The returned note does not identify its forum.")
    target = parse_note_id(replyto) if replyto is not None else requested
    if target != root_id:
        reply = obj_json(api_call(client.get_note, target))
        if (reply.get("forum") or reply.get("id")) != root_id:
            raise ToolError("TARGET_MISMATCH", "The reply target belongs to a different paper forum.")
    account = profile_id(client)
    # The API v2 list endpoint supports replyForum. Its replytoNote detail
    # does not filter the list and can leave the SDK paging over unrelated forms.
    queries = [
        {"replyForum": root_id, "invitee": account},
        {"replyForum": root_id},
    ]
    candidates: dict[str, dict] = {}
    attempts = []
    for query in queries:
        try:
            if callable(getattr(client, "get_all_invitations", None)):
                result = api_call(lambda: list(client.get_all_invitations(**query)))
                limited = False
            else:
                result = api_call(client.get_invitations, **query, limit=1000)
                limited = len(result) == 1000
            attempts.append({"filters": query, "count": len(result), "possibly_truncated": limited})
            for item in result:
                inv = obj_json(item)
                identifier = inv.get("id")
                if not identifier:
                    continue
                record = candidates.setdefault(identifier, {"invitation": inv, "matched_by": [], "can_submit_verified": False})
                if query not in record["matched_by"]:
                    record["matched_by"].append(query)
        except ToolError as exc:
            if exc.code in {"AUTH_REQUIRED", "AUTH_EXPIRED", "MFA_REQUIRED", "HUMAN_VERIFICATION_REQUIRED"}:
                raise
            attempts.append({"filters": query, "error": exc.to_json()})
    return {
        "forum_id": root_id,
        "replyto": target,
        "profile_id": account,
        "candidates": [candidates[k] for k in sorted(candidates)],
        "attempts": attempts,
        "limitations": [
            "Discovery uses replyForum with and without an invitee filter. Inspect the invitation to check whether it accepts this reply target.",
            "A returned invitation is only a visible candidate. This result does not establish permission to submit, the correct author signature, or current stage/deadline eligibility.",
            "An empty or partially failed search does not prove that no reply invitation exists. Inspect an exact invitation ID from the paper's OpenReview interface when needed.",
        ],
    }
