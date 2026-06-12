"""YouTrack MCP server.

Exposes tools to create, comment on, close, and search YouTrack issues.

Env vars (read at startup):
  YOUTRACK_URL                Base URL (e.g. https://<instance>.youtrack.cloud)
  YOUTRACK_TOKEN              Permanent token (Bearer)
  YOUTRACK_DEFAULT_PROJECT_ID Optional default project ID (e.g. "0-1")
"""

import json
import mimetypes
import os
import re
import shlex
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

import yaml
from mcp.server.fastmcp import FastMCP

CONFIG_FILENAME = ".youtrack.yml"
CONFIG_FILENAMES = (".youtrack.yml", ".youtrack.yaml")


def _resolve_token() -> str:
    """Read the YouTrack token from the first available source.

    Order of precedence:
      1. YOUTRACK_TOKEN env var (raw value).
      2. YOUTRACK_TOKEN_FILE env var (path to a file containing the token).
      3. YOUTRACK_TOKEN_CMD env var (shell command whose stdout is the token).

    Returns an empty string if no source is set.
    """
    direct = os.environ.get("YOUTRACK_TOKEN", "").strip()
    if direct:
        return direct

    file_path = os.environ.get("YOUTRACK_TOKEN_FILE", "").strip()
    if file_path:
        try:
            return Path(file_path).expanduser().read_text(encoding="utf-8").strip()
        except OSError as e:
            raise SystemExit(f"YOUTRACK_TOKEN_FILE '{file_path}' could not be read: {e}")

    cmd_str = os.environ.get("YOUTRACK_TOKEN_CMD", "").strip()
    if cmd_str:
        try:
            result = subprocess.run(
                shlex.split(cmd_str),
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError as e:
            raise SystemExit(
                f"YOUTRACK_TOKEN_CMD failed (exit {e.returncode}): {e.stderr.strip()}"
            )
        except subprocess.TimeoutExpired:
            raise SystemExit("YOUTRACK_TOKEN_CMD timed out after 10s")
        except FileNotFoundError as e:
            raise SystemExit(f"YOUTRACK_TOKEN_CMD: {e}")

    return ""


YOUTRACK_URL = os.environ.get("YOUTRACK_URL", "").rstrip("/")
YOUTRACK_TOKEN = _resolve_token()
YOUTRACK_DEFAULT_PROJECT_ID = os.environ.get("YOUTRACK_DEFAULT_PROJECT_ID", "")

if not YOUTRACK_URL or not YOUTRACK_TOKEN:
    raise SystemExit(
        "YOUTRACK_URL and a token source must be set. "
        "Provide a token via YOUTRACK_TOKEN, YOUTRACK_TOKEN_FILE, or YOUTRACK_TOKEN_CMD."
    )

mcp = FastMCP("youtrack")


class YouTrackError(Exception):
    pass


def _redact(text: str) -> str:
    """Strip any occurrence of the live token (or 'Bearer <token>') from a string.

    YouTrack and proxies sometimes echo request headers back in error bodies. This
    keeps the token out of exception messages that flow up to the MCP client.
    """
    if not text or not YOUTRACK_TOKEN:
        return text
    return text.replace(YOUTRACK_TOKEN, "<redacted>").replace(
        f"Bearer {YOUTRACK_TOKEN}", "Bearer <redacted>"
    )


def _send(req: urllib.request.Request, method: str, path: str, timeout: int = 15) -> Any:
    """Execute a prepared Request and parse the JSON response, wrapping errors."""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8")
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        detail = _redact(e.read().decode("utf-8", errors="replace"))
        raise YouTrackError(f"HTTP {e.code} on {method} {path}: {detail}") from None
    except urllib.error.URLError as e:
        raise YouTrackError(f"network error on {method} {path}: {e.reason}") from None
    except json.JSONDecodeError as e:
        raise YouTrackError(f"invalid JSON in response from {method} {path}: {e}") from None


def _request(
    method: str,
    path: str,
    body: dict | None = None,
    params: dict | None = None,
) -> Any:
    url = f"{YOUTRACK_URL}/api{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    headers = {
        "Authorization": f"Bearer {YOUTRACK_TOKEN}",
        "Accept": "application/json",
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    return _send(req, method, path)


def _request_multipart(
    path: str,
    file_parts: list[tuple[str, str, bytes, str]],
    params: dict | None = None,
    timeout: int = 60,
) -> Any:
    """POST multipart/form-data (used for file attachments).

    file_parts is a list of (field_name, filename, content_bytes, content_type).
    YouTrack's attachments endpoint expects the file under the form field `upload`.
    """
    url = f"{YOUTRACK_URL}/api{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    boundary = f"----youtrackmcp{uuid.uuid4().hex}"
    bb = boundary.encode("ascii")
    body = bytearray()
    for field, filename, content, content_type in file_parts:
        safe_name = filename.replace('"', "").replace("\r", "").replace("\n", "")
        body += b"--" + bb + b"\r\n"
        body += (
            f'Content-Disposition: form-data; name="{field}"; filename="{safe_name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
        body += content
        body += b"\r\n"
    body += b"--" + bb + b"--\r\n"

    headers = {
        "Authorization": f"Bearer {YOUTRACK_TOKEN}",
        "Accept": "application/json",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    req = urllib.request.Request(url, data=bytes(body), headers=headers, method="POST")
    return _send(req, "POST", path, timeout=timeout)


def _issue_url(id_readable: str) -> str:
    return f"{YOUTRACK_URL}/issue/{id_readable}"


# ──────────────────────────────────────────────────────────────────────────────
# Project schema cache + fuzzy matching helpers
# ──────────────────────────────────────────────────────────────────────────────

_SCHEMA_TTL_SECONDS = 600  # 10 min. Long-lived MCP server, schemas change rarely.
_schema_cache: dict[str, tuple[float, dict]] = {}
_schema_cache_lock = Lock()


def _fetch_project_schema(project_id: str) -> dict:
    """Raw fetch. Bypasses cache. Returns {project_id, fields: [...]}."""
    raw = _request(
        "GET",
        f"/admin/projects/{project_id}/customFields",
        params={
            "fields": "field(name,fieldType(id)),canBeEmpty,emptyFieldText,"
                      "bundle(values(name))",
            "$top": "100",
        },
    )
    if not isinstance(raw, list):
        raise YouTrackError(
            f"Unexpected schema response shape for project {project_id}: {type(raw).__name__}"
        )
    fields = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        f = entry.get("field") or {}
        bundle = entry.get("bundle") or {}
        if not isinstance(f, dict):
            f = {}
        if not isinstance(bundle, dict):
            bundle = {}
        values = [
            v.get("name")
            for v in (bundle.get("values") or [])
            if isinstance(v, dict) and v.get("name")
        ]
        fields.append({
            "name": f.get("name"),
            "type": (f.get("fieldType") or {}).get("id"),
            "can_be_empty": entry.get("canBeEmpty"),
            "empty_text": entry.get("emptyFieldText"),
            "values": values or None,
        })
    return {"project_id": project_id, "fields": fields}


def _get_project_schema_cached(project_id: str) -> dict:
    """Return cached schema for a project; refresh on TTL expiry."""
    now = time.time()
    with _schema_cache_lock:
        cached = _schema_cache.get(project_id)
        if cached and now - cached[0] < _SCHEMA_TTL_SECONDS:
            return cached[1]
    schema = _fetch_project_schema(project_id)
    with _schema_cache_lock:
        _schema_cache[project_id] = (now, schema)
    return schema


def _normalize(s: str) -> str:
    """Strip case + non-alphanumerics for fuzzy comparison.
    'Won't fix' → 'wontfix', 'In Progress' → 'inprogress', 'S-0' → 's0'.
    """
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _match_value(user_input: str, allowed: list[str]) -> str | None:
    """Resolve user_input to a canonical value from `allowed`.

    Order: exact → case-insensitive → normalized. Returns the canonical form
    (matching the project's casing/spelling) or None if no match. Empty or
    whitespace-only input matches nothing.
    """
    if not allowed:
        return user_input  # field is open-valued
    if not user_input or not user_input.strip():
        return None
    for v in allowed:
        if v == user_input:
            return v
    lower_input = user_input.lower()
    for v in allowed:
        if v.lower() == lower_input:
            return v
    norm = _normalize(user_input)
    if not norm:
        return None
    for v in allowed:
        if _normalize(v) == norm:
            return v
    return None


# Synonym → preference order for the "completion" intent. The first match found
# in the project's actual State values wins. Add new entries as needed.
_CLOSE_STATE_PREFERENCES = [
    "Done", "Fixed", "Resolved", "Completed", "Verified", "Closed",
]
_STATE_SYNONYMS = {
    "fixed": ["Fixed", "Done", "Resolved", "Completed", "Verified"],
    "done": ["Done", "Fixed", "Completed", "Resolved", "Verified"],
    "resolved": ["Resolved", "Fixed", "Done", "Completed"],
    "completed": ["Completed", "Done", "Fixed", "Resolved"],
    "verified": ["Verified", "Done", "Fixed"],
    "closed": ["Closed", "Done", "Fixed", "Resolved"],
    "wontfix": ["Won't fix", "Wontfix", "Will not fix", "Rejected", "Cancelled"],
    "rejected": ["Rejected", "Won't fix", "Cancelled"],
    "duplicate": ["Duplicate"],
    "cancelled": ["Cancelled", "Rejected", "Won't fix"],
    "incomplete": ["Incomplete", "Cannot reproduce"],
}


def _resolve_state(requested: str | None, allowed: list[str]) -> str | None:
    """Pick a valid state name.

    If `requested` is None → pick the first available "completion" state in
    _CLOSE_STATE_PREFERENCES order.
    Otherwise: exact/case-insensitive/normalized match, then synonym list.
    Returns the canonical state name from `allowed`, or None if nothing fits.
    """
    if not allowed:
        return requested  # no constraints from server
    if requested is None:
        for candidate in _CLOSE_STATE_PREFERENCES:
            m = _match_value(candidate, allowed)
            if m:
                return m
        return None

    direct = _match_value(requested, allowed)
    if direct:
        return direct
    synonyms = _STATE_SYNONYMS.get(_normalize(requested), [])
    for cand in synonyms:
        m = _match_value(cand, allowed)
        if m:
            return m
    return None


def _get_project_id_from_issue(issue_id: str) -> str:
    """Look up the internal project ID for an issue ID (readable or internal)."""
    result = _request(
        "GET",
        f"/issues/{issue_id}",
        params={"fields": "project(id)"},
    )
    if not isinstance(result, dict):
        raise YouTrackError(
            f"Unexpected response shape for issue '{issue_id}': {type(result).__name__}"
        )
    project = result.get("project")
    pid = project.get("id") if isinstance(project, dict) else None
    if not pid:
        raise YouTrackError(f"Could not resolve project for issue '{issue_id}'")
    return pid


# Map YouTrack field types to the outer $type discriminator the create payload needs.
# Discovered via /admin/projects/{pid}/customFields response.
_CUSTOM_FIELD_TYPE_MAP = {
    "enum[1]": "SingleEnumIssueCustomField",
    "enum[*]": "MultiEnumIssueCustomField",
    "state[1]": "StateIssueCustomField",
    "user[1]": "SingleUserIssueCustomField",
    "user[*]": "MultiUserIssueCustomField",
    "ownedField[1]": "SingleOwnedIssueCustomField",
    "version[1]": "SingleVersionIssueCustomField",
    "version[*]": "MultiVersionIssueCustomField",
    "build[1]": "SingleBuildIssueCustomField",
    "string": "SimpleIssueCustomField",
    "text": "TextIssueCustomField",
    "integer": "SimpleIssueCustomField",
    "float": "SimpleIssueCustomField",
    "date": "SimpleIssueCustomField",
    "period": "PeriodIssueCustomField",
}

# Some YouTrack instances require an inner $type on the bundle element value too.
# Maps outer field $type to the corresponding inner value $type.
_INNER_VALUE_TYPE_MAP = {
    "SingleEnumIssueCustomField": "EnumBundleElement",
    "MultiEnumIssueCustomField": "EnumBundleElement",
    "StateIssueCustomField": "StateBundleElement",
    "SingleOwnedIssueCustomField": "OwnedBundleElement",
    "SingleVersionIssueCustomField": "VersionBundleElement",
    "MultiVersionIssueCustomField": "VersionBundleElement",
    "SingleBuildIssueCustomField": "BuildBundleElement",
}


def _period_value(value: Any) -> dict[str, Any]:
    """Coerce a period custom-field input into a YouTrack PeriodValue dict.

    A period field's value must be an object ({"minutes": N}), never a bare
    number: YouTrack rejects a raw int with a "this property only accepts a
    PeriodValue-type value" 400. Accepts an int/float (minutes), a human
    duration string ("1h 30m", "90m"; 'd'=8h, 'w'=5d by YouTrack defaults), or a
    pre-built dict ({"minutes": N} / {"presentation": "..."}) which passes through.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, bool):  # bool is an int subclass; reject it explicitly
        raise YouTrackError("invalid period value: expected minutes (int) or a duration string")
    if isinstance(value, (int, float)):
        return {"minutes": int(value)}
    if isinstance(value, str):
        minutes = _parse_duration_to_minutes(value)
        if minutes <= 0:
            raise YouTrackError(
                f"could not parse period '{value}'; use minutes (int) or a duration like '1h 30m'"
            )
        return {"minutes": minutes}
    raise YouTrackError(f"invalid period value {value!r}; expected minutes (int) or a duration string")


def _build_custom_fields_payload(
    fields_input: dict[str, Any] | None,
    project_id: str,
) -> list[dict]:
    """Translate {field_name: value} into YouTrack's customFields payload list.

    Validates every entry against the project schema:
      - Unknown field name → raises YouTrackError listing available fields.
      - Enum/state value that doesn't match (after case-insensitive + normalized
        fuzzy match) → raises YouTrackError listing allowed values.

    Values are shaped per field type:
      - enum/state/owned/version/build → name, matched against allowed values
      - user                           → {"login": value}
      - text                           → {"text": str(value)} (NOT a bare string)
      - date / date-time               → epoch millis ("YYYY-MM-DD" auto-converted)
      - period                         → {"minutes": N} (int or "1h 30m" accepted)
      - string / integer / float       → the bare value
      - list                           → multi-enum / multi-user
      - None                           → clears the field
    """
    if not fields_input:
        return []

    # Fail fast: if we can't fetch the schema, validation is meaningless and the
    # YouTrack API would reject our payload anyway with a less-helpful error.
    schema = _get_project_schema_cached(project_id)

    field_by_name = {f["name"]: f for f in schema.get("fields", []) if f.get("name")}
    available = sorted(field_by_name.keys())

    def _wrap_bundle_value(ytype: str, name_value: str) -> dict[str, str]:
        """Build the inner value dict, attaching the bundle $type if needed."""
        inner: dict[str, str] = {"name": name_value}
        inner_type = _INNER_VALUE_TYPE_MAP.get(ytype)
        if inner_type:
            inner["$type"] = inner_type
        return inner

    payload: list[dict[str, Any]] = []
    for name, value in fields_input.items():
        if field_by_name and name not in field_by_name:
            raise YouTrackError(
                f"Unknown custom field '{name}' on project {project_id}. "
                f"Available: {', '.join(available) if available else '(none discovered)'}"
            )

        field = field_by_name.get(name, {})
        ft_id = field.get("type") or "string"
        ytype = _CUSTOM_FIELD_TYPE_MAP.get(ft_id, "SimpleIssueCustomField")
        allowed = field.get("values")

        if value is None:
            entry: dict[str, Any] = {"name": name, "$type": ytype, "value": None}
        elif isinstance(value, list):
            resolved = []
            for v in value:
                m = _match_value(v, allowed) if allowed else v
                if allowed and m is None:
                    raise YouTrackError(
                        f"Value '{v}' invalid for field '{name}' on project {project_id}. "
                        f"Allowed: {', '.join(allowed)}"
                    )
                resolved.append(m)
            entry = {"name": name, "$type": ytype, "value": [_wrap_bundle_value(ytype, v) for v in resolved]}
        elif ytype == "TextIssueCustomField":
            # A text field's value is a TextFieldValue object, not a bare string.
            # YouTrack rejects a raw string with "this property only accepts a
            # ...TextFieldValue-type value". The outer $type lets YouTrack infer
            # the inner type, so no inner $type is needed.
            entry = {"name": name, "$type": ytype, "value": {"text": str(value)}}
        elif ytype == "PeriodIssueCustomField":
            entry = {"name": name, "$type": ytype, "value": _period_value(value)}
        elif ytype == "SimpleIssueCustomField":
            # string / integer / float take the bare value; date and date-time
            # take epoch milliseconds, so convert "YYYY-MM-DD" (ints pass
            # through). A raw date string yields "Incompatible value format".
            v = _date_to_ms(value) if "date" in (ft_id or "") else value
            entry = {"name": name, "$type": ytype, "value": v}
        elif "User" in ytype:
            entry = {"name": name, "$type": ytype, "value": {"login": value}}
        else:
            m = _match_value(value, allowed) if allowed else value
            if allowed and m is None:
                raise YouTrackError(
                    f"Value '{value}' invalid for field '{name}' on project {project_id}. "
                    f"Allowed: {', '.join(allowed)}"
                )
            entry = {"name": name, "$type": ytype, "value": _wrap_bundle_value(ytype, m)}

        payload.append(entry)

    return payload


def _create_issue_impl(
    summary: str,
    description: str = "",
    project_id: str | None = None,
    custom_fields: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Plain-Python implementation. Called by the @mcp.tool wrapper below and by
    create_and_close_issue, so that the latter doesn't depend on FastMCP internals.
    """
    pid = project_id or YOUTRACK_DEFAULT_PROJECT_ID
    if not pid:
        raise YouTrackError("project_id is required (no default configured)")

    body: dict[str, Any] = {
        "project": {"id": pid},
        "summary": summary,
        "description": description,
    }

    cf_payload = _build_custom_fields_payload(custom_fields, pid)
    if cf_payload:
        body["customFields"] = cf_payload

    if tags:
        body["tags"] = [{"name": t} for t in tags]

    result = _request(
        "POST",
        "/issues",
        body=body,
        params={"fields": "id,idReadable,summary"},
    )
    if not isinstance(result, dict):
        raise YouTrackError(f"Unexpected create_issue response shape: {type(result).__name__}")
    return {
        "id": result.get("id"),
        "id_readable": result.get("idReadable"),
        "summary": result.get("summary"),
        "url": _issue_url(result.get("idReadable", "")),
    }


@mcp.tool()
def create_issue(
    summary: str,
    description: str = "",
    project_id: str | None = None,
    custom_fields: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Create a YouTrack issue.

    Args:
        summary: Issue title (required).
        description: Markdown body (optional).
        project_id: Project internal ID like "0-1". Defaults to YOUTRACK_DEFAULT_PROJECT_ID.
        custom_fields: Optional {field_name: value} dict. Examples:
            {"Priority": "Critical", "Type": "Bug", "Assignee": "morgan.s"}
            Use get_project_fields() to discover valid field names and values.
            Enum/state values are matched case-insensitively against the schema.
            Text fields take a plain string; date fields take "YYYY-MM-DD" (or
            epoch millis); period fields take minutes (int) or a duration like
            "1h 30m"; multi-value fields take a list; None clears a field.
        tags: Optional list of tag names to attach to the issue.

    Returns: {id, id_readable, summary, url}
    """
    return _create_issue_impl(summary, description, project_id, custom_fields, tags)


@mcp.tool()
def add_comment(issue_id: str, text: str) -> dict:
    """Add a comment to an issue.

    Args:
        issue_id: Readable ID ("LBD-123") or internal ID.
        text: Markdown comment body.
    """
    result = _request(
        "POST",
        f"/issues/{issue_id}/comments",
        body={"text": text},
        params={"fields": "id"},
    )
    return {"ok": True, "comment_id": result.get("id"), "issue_id": issue_id}


def _close_issue_impl(
    issue_id: str,
    comment: str = "",
    state: str | None = None,
) -> dict[str, Any]:
    """Plain-Python implementation, called by the @mcp.tool wrapper and by
    create_and_close_issue.
    """
    project_id = _get_project_id_from_issue(issue_id)
    schema = _get_project_schema_cached(project_id)
    state_field = next(
        (f for f in schema.get("fields", []) if f.get("name") == "State"),
        None,
    )
    if state_field is None:
        raise YouTrackError(
            f"Project {project_id} has no 'State' field; cannot close issue {issue_id} "
            "this way. Set the state manually or use a different YouTrack command."
        )
    allowed = state_field.get("values") or []

    target_state = _resolve_state(state, allowed)
    if not target_state:
        raise YouTrackError(
            f"Cannot resolve state '{state}' for project {project_id}. "
            f"Allowed: {', '.join(allowed)}"
        )

    # Wrap the resolved state in braces ONLY if it contains whitespace or
    # non-alphanumerics. YouTrack's command parser requires braces for
    # multi-word values like {In Progress} or {Won't fix}, but REJECTS them
    # for plain single-word values: "State {Done}" yields HTTP 400.
    needs_braces = bool(re.search(r"[^A-Za-z0-9]", target_state))
    state_token = f"{{{target_state}}}" if needs_braces else target_state

    # The commands endpoint is /api/commands (singular, top-level), not
    # /api/issues/{id}/commands (which returns 404 "No subresource for path
    # commands"). The target issue is specified in the body, not the URL.
    body: dict[str, Any] = {
        "query": f"State {state_token}",
        "issues": [{"idReadable": issue_id}],
    }
    if comment:
        body["comment"] = comment
    _request("POST", "/commands", body=body)
    return {
        "ok": True,
        "issue_id": issue_id,
        "state": target_state,
        "project_id": project_id,
        "requested_state": state,
    }


@mcp.tool()
def close_issue(
    issue_id: str,
    comment: str = "",
    state: str | None = None,
) -> dict:
    """Close an issue by setting its State.

    Auto-resolves the target state against the project's actual State field:
      - state=None  → picks the project's "completion" state (Done > Fixed >
                       Resolved > Completed > Verified > Closed, first available).
      - state="..." → exact / case-insensitive / synonym match (e.g. "fixed" on
                       a project that only has "Done" resolves to "Done").

    If no valid state can be resolved, raises YouTrackError listing allowed values.

    Args:
        issue_id: Readable ID ("LBD-123") or internal ID ("2-128").
        comment: Optional closing comment (Markdown).
        state: Intent or canonical state name. Pass None to let the server pick.

    Returns: {ok, issue_id, state, project_id, requested_state}
    """
    return _close_issue_impl(issue_id, comment, state)


@mcp.tool()
def create_and_close_issue(
    summary: str,
    description: str = "",
    closing_comment: str = "",
    state: str | None = None,
    project_id: str | None = None,
    custom_fields: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Create an issue and immediately close it. Useful for after-the-fact tracking
    of work that's already done.

    `state` follows the same rules as close_issue: None lets the server pick the
    project's canonical "done" state; a string is resolved (with fuzzy match and
    synonyms) against the project's allowed values.

    Accepts the same custom_fields/tags as create_issue.
    Returns the created (and now closed) issue info.
    """
    issue = _create_issue_impl(
        summary=summary,
        description=description,
        project_id=project_id,
        custom_fields=custom_fields,
        tags=tags,
    )
    close_result = _close_issue_impl(
        issue_id=issue["id_readable"],
        comment=closing_comment,
        state=state,
    )
    issue["state"] = close_result["state"]
    issue["closed"] = True
    return issue


@mcp.tool()
def search_issues(query: str, limit: int = 10, offset: int = 0) -> list[dict]:
    """Search issues using YouTrack query syntax.

    Note: the `project:` operator expects the project's SHORT NAME (e.g. `IS`,
    `DLLBH`), not the internal ID (e.g. `0-19`). Internal IDs are accepted by
    `create_issue` and `get_project_fields` but YouTrack's search parser rejects
    them with `invalid_query`. Use `list_projects` to see the short names.

    Args:
        query: YouTrack search query.
        limit: Max results to return (page size).
        offset: Number of results to skip, for pagination.

    Examples:
        "project: IS #Unresolved"
        "assignee: me State: Open"
        "summary: deploy created: 2026-05"
    """
    params = {
        "query": query,
        "$top": str(limit),
        "$skip": str(offset),
        "fields": "idReadable,summary,resolved",
    }
    result = _request("GET", "/issues", params=params)
    if not isinstance(result, list):
        raise YouTrackError(
            f"Unexpected search_issues response shape: {type(result).__name__}"
        )
    return [
        {
            "id": item.get("idReadable"),
            "summary": item.get("summary"),
            "resolved": item.get("resolved") is not None,
            "url": _issue_url(item.get("idReadable", "")),
        }
        for item in result
        if isinstance(item, dict)
    ]


def _restore_evidence_on_key(evidence: dict) -> dict:
    """Undo YAML 1.1 boolean coercion of an unquoted ``on:`` key.

    PyYAML's ``safe_load`` parses the bare mapping key ``on`` as the boolean
    ``True`` (the YAML 1.1 "Norway problem"), so an evidence block written as
    ``on: [...]`` instead of ``"on": [...]`` arrives keyed by ``True`` rather than
    the string ``"on"``. Remap it back so consumers can read ``evidence["on"]``.

    An explicit string ``"on"`` key wins if both are present; the stray coerced
    key is dropped either way. Non-dict input is returned untouched.
    """
    if not isinstance(evidence, dict):
        return evidence
    restored: dict = {}
    coerced_on = None
    found_coerced = False
    for key, value in evidence.items():
        # `key is True` (identity) avoids matching an integer key 1, since
        # `1 == True` under `in`/`==` but not under `is`.
        if key is True:
            coerced_on = value
            found_coerced = True
        else:
            restored[key] = value
    if found_coerced:
        restored.setdefault("on", coerced_on)
    return restored


@mcp.tool()
def find_youtrack_config(start_path: str) -> dict:
    """Locate and parse a .youtrack.yml file in `start_path` or any ancestor directory.

    Walks upward from start_path until a .youtrack.yml (or .youtrack.yaml) is found,
    or the filesystem root is reached. Returns the parsed config merged with safe
    defaults plus metadata about where it was found.

    Args:
        start_path: Absolute directory path to start the search from (typically the
                    current working directory of the Claude Code session).

    Returns:
        {
          "found": bool,
          "path": str | None,        # absolute path of the .youtrack.yml file
          "project_root": str | None,# directory containing the file
          "config": {
            "project_id": str,
            "project_short": str | None,
            "auto_search": bool,
            "auto_propose": bool,
            "default_tags": list[str],
            "default_assignee": str | None,
            "ignore_paths": list[str],
          } | None,
          "error": str | None,       # parse error if any
        }
    """
    defaults = {
        "project_id": YOUTRACK_DEFAULT_PROJECT_ID or None,
        "project_short": None,
        "auto_search": True,
        "auto_propose": True,
        "auto_confirm": False,
        "language": "en",
        "summary_template": None,
        "description_template": None,
        "variables": {},
        "custom_fields": {},
        "default_tags": [],
        "default_assignee": None,
        "ignore_paths": [],
        "evidence": {
            "enabled": False,
            "on": ["tests", "build", "commit", "deploy"],
            "attach_artifacts": True,
        },
    }

    try:
        start = Path(start_path).expanduser().resolve()
    except (OSError, ValueError) as e:
        return {"found": False, "path": None, "project_root": None, "config": None, "error": f"invalid start_path: {e}"}

    current = start if start.is_dir() else start.parent
    for directory in [current, *current.parents]:
        for name in CONFIG_FILENAMES:
            candidate = directory / name
            if candidate.is_file():
                try:
                    raw = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
                except yaml.YAMLError as e:
                    return {
                        "found": True,
                        "path": str(candidate),
                        "project_root": str(directory),
                        "config": None,
                        "error": f"yaml parse error: {e}",
                    }
                if not isinstance(raw, dict):
                    return {
                        "found": True,
                        "path": str(candidate),
                        "project_root": str(directory),
                        "config": None,
                        "error": "config root must be a mapping",
                    }
                merged = {**defaults, **raw}
                # `evidence` is a nested mapping, so the shallow merge above lets a
                # user block replace the whole default (dropping keys it omits).
                # Deep-merge it over the defaults instead, and restore the `on:` key
                # that YAML 1.1 coerces to boolean True when written unquoted.
                raw_evidence = raw.get("evidence")
                if isinstance(raw_evidence, dict):
                    merged["evidence"] = {
                        **defaults["evidence"],
                        **_restore_evidence_on_key(raw_evidence),
                    }
                return {
                    "found": True,
                    "path": str(candidate),
                    "project_root": str(directory),
                    "config": merged,
                    "error": None,
                }

    return {"found": False, "path": None, "project_root": None, "config": None, "error": None}


@mcp.tool()
def get_project_fields(project_id: str) -> dict:
    """Inspect a project's custom fields and their allowed values.

    Returns the schema needed to set custom fields on issue creation: field names,
    types ("enum[1]", "state[1]", "user[1]", "string"...), whether they can be
    empty, and (for enum/state fields) the list of allowed value names.

    Use this once per project to discover what's configurable, then pass
    `custom_fields={...}` to create_issue/create_and_close_issue.

    Returns:
        {
          "project_id": "0-1",
          "fields": [
            {
              "name": "Priority",
              "type": "enum[1]",
              "can_be_empty": true,
              "empty_text": "No Priority",
              "values": ["Show-stopper", "Critical", "Major", "Normal", "Minor"]
            },
            ...
          ]
        }
    """
    return _get_project_schema_cached(project_id)


@mcp.tool()
def list_projects() -> list[dict]:
    """List YouTrack projects (id, name, shortName)."""
    result = _request(
        "GET",
        "/admin/projects",
        params={"fields": "id,name,shortName", "$top": "100"},
    )
    if not isinstance(result, list):
        raise YouTrackError(
            f"Unexpected list_projects response shape: {type(result).__name__}"
        )
    return [
        {"id": p.get("id"), "name": p.get("name"), "short_name": p.get("shortName")}
        for p in result
        if isinstance(p, dict)
    ]


# ──────────────────────────────────────────────────────────────────────────────
# Helpers for the JetBrains-parity tools (v0.3)
# ──────────────────────────────────────────────────────────────────────────────


def _article_url(id_readable: str) -> str:
    return f"{YOUTRACK_URL}/articles/{id_readable}"


def _ms_to_iso(ms: Any) -> Any:
    """Convert a YouTrack epoch-milliseconds timestamp to an ISO 8601 UTC string.

    YouTrack returns timestamps as epoch milliseconds. Returns the input
    unchanged if it is None or not a number we can convert.
    """
    if ms is None or not isinstance(ms, (int, float)):
        return ms
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return ms


def _date_to_ms(date: Any) -> int:
    """Accept a 'YYYY-MM-DD' string or epoch-millis int and return epoch millis."""
    if isinstance(date, bool):  # bool is an int subclass; reject it explicitly
        raise YouTrackError("invalid date: expected 'YYYY-MM-DD' or epoch millis")
    if isinstance(date, int):
        return date
    if isinstance(date, str):
        s = date.strip()
        if s.isdigit():
            return int(s)
        try:
            dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            raise YouTrackError(f"invalid date '{date}', expected 'YYYY-MM-DD'") from None
        return int(dt.timestamp() * 1000)
    raise YouTrackError(f"invalid date {date!r}, expected 'YYYY-MM-DD' or epoch millis")


# YouTrack default working time: 8h workday, 5-day workweek. Used to interpret
# 'd' and 'w' in human duration strings. Instances can reconfigure this, so 'd'
# and 'w' are best-effort; prefer 'h'/'m' for exactness.
def _parse_duration_to_minutes(s: str) -> int:
    """Parse a human duration like '1h 30m', '90m', '2h', '1d' into minutes.

    A bare integer string is treated as minutes. Supports w/d/h/m units. Returns
    0 if nothing parseable is found.
    """
    if not s:
        return 0
    s = s.strip().lower()
    if s.isdigit():
        return int(s)
    total = 0
    found = False
    for num, unit in re.findall(r"(\d+)\s*([wdhm])", s):
        found = True
        n = int(num)
        total += n * {"w": 5 * 8 * 60, "d": 8 * 60, "h": 60, "m": 1}[unit]
    return total if found else 0


# Friendly link-type aliases → the YouTrack command phrase. Keys are _normalize()d.
# Direction matters: "depends on X" means this issue depends on X.
_LINK_TYPE_PHRASES = {
    "relates": "relates to",
    "relatesto": "relates to",
    "related": "relates to",
    "relatedto": "relates to",
    "depends": "depends on",
    "dependson": "depends on",
    "requiredfor": "is required for",
    "isrequiredfor": "is required for",
    "duplicates": "duplicates",
    "duplicate": "duplicates",
    "duplicatedby": "is duplicated by",
    "isduplicatedby": "is duplicated by",
    "subtaskof": "subtask of",
    "subtask": "subtask of",
    "child": "subtask of",
    "childof": "subtask of",
    "parentfor": "parent for",
    "parent": "parent for",
}


def _resolve_link_phrase(link_type: str | None) -> str:
    """Map a user-supplied link type to a YouTrack command phrase.

    Defaults to "relates to" when link_type is None/empty. Raises YouTrackError
    listing supported phrases on an unrecognized value.
    """
    if not link_type or not link_type.strip():
        return "relates to"
    phrase = _LINK_TYPE_PHRASES.get(_normalize(link_type))
    if phrase is None:
        supported = sorted(set(_LINK_TYPE_PHRASES.values()))
        raise YouTrackError(
            f"Unknown link type '{link_type}'. Supported: {', '.join(supported)}"
        )
    return phrase


def _flatten_cf_value(value: Any) -> Any:
    """Reduce a customFields 'value' (dict | list | scalar | None) to a readable form.

    Picks the most meaningful attribute of a bundle/user value: name → fullName →
    login → text → presentation → minutes. Lists are flattened element-wise.
    Bare scalars (integer/float/date-as-millis fields) pass through unchanged.
    """
    if value is None:
        return None
    if isinstance(value, list):
        return [_flatten_cf_value(v) for v in value]
    if isinstance(value, dict):
        for key in ("name", "fullName", "login", "text", "presentation", "minutes"):
            if value.get(key) is not None:
                return value[key]
        return None
    return value


def _flatten_links(links: Any) -> list[dict]:
    """Flatten an issue's `links` array into [{relation, issues:[idReadable,...]}].

    Uses the direction to pick the human-readable relation name
    (OUTWARD → sourceToTarget, INWARD → targetToSource). Link entries with no
    linked issues are dropped.
    """
    out: list[dict] = []
    for link in links or []:
        if not isinstance(link, dict):
            continue
        link_type = link.get("linkType") or {}
        direction = link.get("direction")
        if direction == "OUTWARD":
            relation = link_type.get("sourceToTarget")
        elif direction == "INWARD":
            relation = link_type.get("targetToSource")
        else:
            relation = link_type.get("name")
        issues = [
            i.get("idReadable")
            for i in (link.get("issues") or [])
            if isinstance(i, dict) and i.get("idReadable")
        ]
        if issues:
            out.append({"relation": relation, "issues": issues})
    return out


def _map_user(u: Any) -> dict:
    """Project a YouTrack User entity into a compact dict."""
    if not isinstance(u, dict):
        return {}
    return {
        "id": u.get("id"),
        "login": u.get("login"),
        "full_name": u.get("fullName"),
        "email": u.get("email"),
    }


_ISSUE_DETAIL_FIELDS = (
    "idReadable,summary,description,created,updated,resolved,"
    "reporter(login,fullName),project(id,shortName,name),"
    "customFields(name,value(name,login,fullName,text,presentation,minutes)),"
    "commentsCount,tags(name),"
    "links(direction,linkType(name,sourceToTarget,targetToSource),issues(idReadable))"
)

_ARTICLE_DETAIL_FIELDS = (
    "idReadable,summary,content,created,updated,reporter(login),project(id,name),"
    "parentArticle(idReadable),childArticles(idReadable,summary)"
)


def _update_issue_impl(
    issue_id: str,
    summary: str | None = None,
    description: str | None = None,
    custom_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Shared issue-update path. Used by update_issue and change_issue_assignee."""
    body: dict[str, Any] = {}
    if summary is not None:
        body["summary"] = summary
    if description is not None:
        body["description"] = description
    if custom_fields:
        project_id = _get_project_id_from_issue(issue_id)
        cf_payload = _build_custom_fields_payload(custom_fields, project_id)
        if cf_payload:
            body["customFields"] = cf_payload
    if not body:
        raise YouTrackError(
            "update_issue: nothing to update "
            "(provide summary, description, and/or custom_fields)"
        )
    result = _request(
        "POST",
        f"/issues/{issue_id}",
        body=body,
        params={"fields": "idReadable,summary"},
    )
    if not isinstance(result, dict):
        raise YouTrackError(f"Unexpected update_issue response shape: {type(result).__name__}")
    id_readable = result.get("idReadable", issue_id)
    return {
        "ok": True,
        "issue_id": id_readable,
        "summary": result.get("summary"),
        "url": _issue_url(id_readable),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Issue tools (JetBrains parity)
# ──────────────────────────────────────────────────────────────────────────────


@mcp.tool()
def get_issue(issue_id: str) -> dict:
    """Retrieve full details of a single issue, including custom fields.

    Args:
        issue_id: Readable ID ("IS-87") or internal ID ("2-128").

    Returns a dict with summary, description, flattened custom fields (State,
    Priority, Assignee, ...), reporter, project short name, timestamps (ISO 8601),
    tags, link relations, comment count, and the issue URL.
    """
    result = _request(
        "GET",
        f"/issues/{issue_id}",
        params={"fields": _ISSUE_DETAIL_FIELDS},
    )
    if not isinstance(result, dict):
        raise YouTrackError(f"Unexpected get_issue response shape: {type(result).__name__}")

    fields: dict[str, Any] = {}
    for cf in result.get("customFields") or []:
        if isinstance(cf, dict) and cf.get("name"):
            fields[cf["name"]] = _flatten_cf_value(cf.get("value"))

    project = result.get("project") or {}
    reporter = result.get("reporter") or {}
    id_readable = result.get("idReadable", issue_id)
    return {
        "id": id_readable,
        "summary": result.get("summary"),
        "description": result.get("description"),
        "fields": fields,
        "reporter": reporter.get("login") if isinstance(reporter, dict) else None,
        "project": project.get("shortName") if isinstance(project, dict) else None,
        "created": _ms_to_iso(result.get("created")),
        "updated": _ms_to_iso(result.get("updated")),
        "resolved": _ms_to_iso(result.get("resolved")),
        "is_resolved": result.get("resolved") is not None,
        "comments_count": result.get("commentsCount"),
        "tags": [t.get("name") for t in (result.get("tags") or []) if isinstance(t, dict)],
        "links": _flatten_links(result.get("links")),
        "url": _issue_url(id_readable),
    }


@mcp.tool()
def update_issue(
    issue_id: str,
    summary: str | None = None,
    description: str | None = None,
    custom_fields: dict[str, Any] | None = None,
) -> dict:
    """Update an existing issue's summary, description, and/or custom fields.

    Only the provided arguments are changed. `custom_fields` is a
    {field_name: value} dict validated against the project schema exactly like
    create_issue (use get_project_fields to discover names/values). Pass a value
    of None to clear a field.

    Args:
        issue_id: Readable ID ("IS-87") or internal ID.
        summary: New title, or None to leave unchanged.
        description: New Markdown body, or None to leave unchanged.
        custom_fields: {field_name: value} to set, e.g. {"Priority": "Critical"}.

    Returns: {ok, issue_id, summary, url}
    """
    return _update_issue_impl(issue_id, summary, description, custom_fields)


@mcp.tool()
def change_issue_assignee(issue_id: str, assignee: str | None = None) -> dict:
    """Assign an issue to a user, or unassign it.

    Sets the project's "Assignee" custom field. The login is validated against
    the project schema. Pass an empty string or None to unassign.

    Args:
        issue_id: Readable ID ("IS-87") or internal ID.
        assignee: User login to assign, or None / "" to clear the assignee.

    Returns: {ok, issue_id, assignee, url}
    """
    login = (assignee or "").strip() or None
    result = _update_issue_impl(issue_id, custom_fields={"Assignee": login})
    result["assignee"] = login
    return result


@mcp.tool()
def create_draft_issue(
    summary: str,
    description: str = "",
    project_id: str | None = None,
) -> dict:
    """Create a draft issue, visible only to you until published.

    Drafts are a private staging area. To create a real, visible issue, use
    create_issue instead. Note: the drafts endpoint is a semi-public YouTrack API
    and may change between releases.

    Args:
        summary: Draft title.
        description: Markdown body (optional).
        project_id: Project internal ID. Defaults to YOUTRACK_DEFAULT_PROJECT_ID.

    Returns: {id, id_readable, summary, draft: true}
    """
    pid = project_id or YOUTRACK_DEFAULT_PROJECT_ID
    if not pid:
        raise YouTrackError("project_id is required (no default configured)")
    result = _request(
        "POST",
        "/admin/users/me/drafts",
        body={"project": {"id": pid}, "summary": summary, "description": description},
        params={"fields": "id,idReadable,summary"},
    )
    if not isinstance(result, dict):
        raise YouTrackError(f"Unexpected create_draft_issue response shape: {type(result).__name__}")
    return {
        "id": result.get("id"),
        "id_readable": result.get("idReadable"),
        "summary": result.get("summary"),
        "draft": True,
    }


@mcp.tool()
def get_issue_comments(issue_id: str, limit: int = 50, offset: int = 0) -> list[dict]:
    """List comments on an issue, most-recent pagination via limit/offset.

    Args:
        issue_id: Readable ID ("IS-87") or internal ID.
        limit: Max comments to return.
        offset: Number of comments to skip.

    Returns a list of {id, text, author, created, updated, deleted}.
    """
    result = _request(
        "GET",
        f"/issues/{issue_id}/comments",
        params={
            "fields": "id,text,created,updated,author(login,fullName),deleted",
            "$top": str(limit),
            "$skip": str(offset),
        },
    )
    if not isinstance(result, list):
        raise YouTrackError(f"Unexpected get_issue_comments response shape: {type(result).__name__}")
    out = []
    for c in result:
        if not isinstance(c, dict):
            continue
        author = c.get("author") or {}
        out.append({
            "id": c.get("id"),
            "text": c.get("text"),
            "author": author.get("login") if isinstance(author, dict) else None,
            "created": _ms_to_iso(c.get("created")),
            "updated": _ms_to_iso(c.get("updated")),
            "deleted": c.get("deleted"),
        })
    return out


# Cap in-memory attachment reads. Evidence files (reports, logs, coverage,
# screenshots) are small; this guards against accidentally loading a huge file.
_MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024


@mcp.tool()
def attach_file(issue_id: str, file_path: str, file_name: str | None = None) -> dict:
    """Attach a local file to an issue, e.g. as evidence of work done.

    Ideal for audit trails: attach a JUnit/coverage report, a test log, a diff,
    or a screenshot to the relevant ticket. The file is read from the local
    filesystem and uploaded to YouTrack.

    Args:
        issue_id: Readable ID ("IS-87") or internal ID.
        file_path: Absolute path to the local file to upload.
        file_name: Optional name to show in YouTrack (defaults to the file's name).

    Returns: {ok, issue_id, id, name, size, mime_type, url}
    """
    path_obj = Path(file_path).expanduser()
    if not path_obj.is_file():
        raise YouTrackError(f"attach_file: '{file_path}' is not a readable file")
    size = path_obj.stat().st_size
    if size > _MAX_ATTACHMENT_BYTES:
        raise YouTrackError(
            f"attach_file: '{file_path}' is {size} bytes, over the "
            f"{_MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB limit"
        )
    content = path_obj.read_bytes()
    name = file_name or path_obj.name
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"

    result = _request_multipart(
        f"/issues/{issue_id}/attachments",
        file_parts=[("upload", name, content, content_type)],
        params={"fields": "id,name,size,mimeType,url"},
    )
    # YouTrack returns an array (one element per uploaded file).
    item = result[0] if isinstance(result, list) and result else result
    if not isinstance(item, dict):
        raise YouTrackError(f"Unexpected attach_file response shape: {type(item).__name__}")
    url = item.get("url")
    if isinstance(url, str) and url.startswith("/"):
        url = f"{YOUTRACK_URL}{url}"
    return {
        "ok": True,
        "issue_id": issue_id,
        "id": item.get("id"),
        "name": item.get("name"),
        "size": item.get("size"),
        "mime_type": item.get("mimeType"),
        "url": url,
    }


@mcp.tool()
def link_issues(issue_id: str, target_issue_id: str, link_type: str = "relates") -> dict:
    """Link two issues with a typed relation.

    The relation reads "<issue_id> <link_type> <target_issue_id>", e.g.
    link_issues("IS-10", "IS-3", "depends on") means IS-10 depends on IS-3.

    Supported link_type values (aliases accepted): "relates to", "depends on",
    "is required for", "duplicates", "is duplicated by", "subtask of",
    "parent for". Defaults to "relates to".

    Returns: {ok, issue_id, target_issue_id, link_type, command}
    """
    phrase = _resolve_link_phrase(link_type)
    command = f"{phrase} {target_issue_id}"
    _request(
        "POST",
        "/commands",
        body={"query": command, "issues": [{"idReadable": issue_id}]},
    )
    return {
        "ok": True,
        "issue_id": issue_id,
        "target_issue_id": target_issue_id,
        "link_type": phrase,
        "command": command,
    }


@mcp.tool()
def manage_issue_tags(
    issue_id: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> dict:
    """Add and/or remove tags on an issue.

    Tags are matched by name (case-insensitive). YouTrack does NOT auto-create
    tags: a name in `add` that doesn't already exist (and isn't visible to you)
    is reported under "not_found" rather than created. A name in `remove` that
    isn't currently on the issue is likewise reported under "not_found".

    Args:
        issue_id: Readable ID ("IS-87") or internal ID.
        add: Tag names to attach.
        remove: Tag names to detach.

    Returns: {ok, issue_id, added, removed, not_found}
    """
    add = add or []
    remove = remove or []
    added: list[str] = []
    removed: list[str] = []
    not_found: list[str] = []

    if add:
        all_tags = _request("GET", "/tags", params={"fields": "id,name", "$top": "1000"})
        by_norm = {
            _normalize(t["name"]): t["id"]
            for t in (all_tags if isinstance(all_tags, list) else [])
            if isinstance(t, dict) and t.get("name") and t.get("id")
        }
        for name in add:
            tag_id = by_norm.get(_normalize(name))
            if tag_id is None:
                not_found.append(name)
                continue
            _request("POST", f"/issues/{issue_id}/tags", body={"id": tag_id})
            added.append(name)

    if remove:
        issue_tags = _request(
            "GET", f"/issues/{issue_id}/tags", params={"fields": "id,name"}
        )
        by_norm_issue = {
            _normalize(t["name"]): t["id"]
            for t in (issue_tags if isinstance(issue_tags, list) else [])
            if isinstance(t, dict) and t.get("name") and t.get("id")
        }
        for name in remove:
            tag_id = by_norm_issue.get(_normalize(name))
            if tag_id is None:
                not_found.append(name)
                continue
            _request("DELETE", f"/issues/{issue_id}/tags/{tag_id}")
            removed.append(name)

    return {
        "ok": True,
        "issue_id": issue_id,
        "added": added,
        "removed": removed,
        "not_found": not_found,
    }


@mcp.tool()
def get_saved_issue_searches() -> list[dict]:
    """List saved searches visible to the current user.

    Returns a list of {id, name, query, owner}. Use the `query` of one as input
    to search_issues.
    """
    result = _request(
        "GET",
        "/savedQueries",
        params={"fields": "id,name,query,owner(login)", "$top": "100"},
    )
    if not isinstance(result, list):
        raise YouTrackError(f"Unexpected get_saved_issue_searches response shape: {type(result).__name__}")
    out = []
    for q in result:
        if not isinstance(q, dict):
            continue
        owner = q.get("owner") or {}
        out.append({
            "id": q.get("id"),
            "name": q.get("name"),
            "query": q.get("query"),
            "owner": owner.get("login") if isinstance(owner, dict) else None,
        })
    return out


def _resolve_work_item_type(project_id: str, name: str) -> str | None:
    """Resolve a work-item-type name to its id (project types, then global)."""
    for path in (
        f"/admin/projects/{project_id}/timeTrackingSettings/workItemTypes",
        "/admin/timeTrackingSettings/workItemTypes",
    ):
        try:
            types = _request("GET", path, params={"fields": "id,name", "$top": "100"})
        except YouTrackError:
            continue
        for t in types if isinstance(types, list) else []:
            if isinstance(t, dict) and _normalize(t.get("name") or "") == _normalize(name):
                return t.get("id")
    return None


@mcp.tool()
def log_work(
    issue_id: str,
    minutes: int | None = None,
    duration: str | None = None,
    text: str = "",
    date: str | None = None,
    work_type: str | None = None,
) -> dict:
    """Log time spent on an issue (a work item).

    Provide the time as either `minutes` (int) or a human `duration` string like
    "1h 30m" / "90m" / "2h" (parsed to minutes; 'd'=8h, 'w'=5d by YouTrack
    defaults). One of the two is required.

    Args:
        issue_id: Readable ID ("IS-87") or internal ID.
        minutes: Time spent in minutes.
        duration: Human duration string, used if `minutes` is not given.
        text: Optional work description.
        date: Optional work date as "YYYY-MM-DD" (defaults to today).
        work_type: Optional work-item-type name (resolved against the project).

    Returns: {ok, issue_id, minutes, presentation, text, date, work_type}
    """
    mins = minutes if minutes is not None else (_parse_duration_to_minutes(duration or ""))
    if not mins or mins <= 0:
        raise YouTrackError(
            "log_work requires a positive 'minutes' (int) or 'duration' string like '1h 30m'"
        )
    body: dict[str, Any] = {"duration": {"minutes": int(mins)}}
    if text:
        body["text"] = text
    if date:
        body["date"] = _date_to_ms(date)
    if work_type:
        project_id = _get_project_id_from_issue(issue_id)
        type_id = _resolve_work_item_type(project_id, work_type)
        if type_id is None:
            raise YouTrackError(
                f"Unknown work_type '{work_type}' for the issue's project. "
                "Omit it or check the project's time-tracking settings."
            )
        body["type"] = {"id": type_id}

    result = _request(
        "POST",
        f"/issues/{issue_id}/timeTracking/workItems",
        body=body,
        params={"fields": "id,duration(minutes,presentation),text,date,type(name)"},
    )
    if not isinstance(result, dict):
        raise YouTrackError(f"Unexpected log_work response shape: {type(result).__name__}")
    dur = result.get("duration") or {}
    wtype = result.get("type") or {}
    return {
        "ok": True,
        "issue_id": issue_id,
        "minutes": dur.get("minutes") if isinstance(dur, dict) else int(mins),
        "presentation": dur.get("presentation") if isinstance(dur, dict) else None,
        "text": result.get("text"),
        "date": _ms_to_iso(result.get("date")),
        "work_type": wtype.get("name") if isinstance(wtype, dict) else None,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Project / user / group tools (JetBrains parity)
# ──────────────────────────────────────────────────────────────────────────────


@mcp.tool()
def get_project(project_id: str) -> dict:
    """Get details of a single project by internal ID or short name.

    Returns: {id, name, short_name, description, leader, archived, created_by}
    """
    result = _request(
        "GET",
        f"/admin/projects/{project_id}",
        params={
            "fields": "id,name,shortName,description,"
                      "leader(login,fullName),archived,createdBy(login)",
        },
    )
    if not isinstance(result, dict):
        raise YouTrackError(f"Unexpected get_project response shape: {type(result).__name__}")
    leader = result.get("leader") or {}
    created_by = result.get("createdBy") or {}
    return {
        "id": result.get("id"),
        "name": result.get("name"),
        "short_name": result.get("shortName"),
        "description": result.get("description"),
        "leader": leader.get("login") if isinstance(leader, dict) else None,
        "archived": result.get("archived"),
        "created_by": created_by.get("login") if isinstance(created_by, dict) else None,
    }


@mcp.tool()
def find_user(query: str, limit: int = 20) -> list[dict]:
    """Find users by login, full name, or email (substring, case-insensitive).

    Args:
        query: Text to match against login / full name / email.
        limit: Max users to return.

    Returns a list of {id, login, full_name, email}.
    """
    q = (query or "").strip()
    ql = q.lower()
    fields = "id,login,fullName,email"

    def _filter(users: Any) -> list[dict]:
        out = []
        for u in users if isinstance(users, list) else []:
            if not isinstance(u, dict):
                continue
            haystack = " ".join(
                str(u.get(k) or "") for k in ("login", "fullName", "email")
            ).lower()
            if not ql or ql in haystack:
                out.append(_map_user(u))
            if len(out) >= limit:
                break
        return out

    # Optimization: YouTrack Cloud accepts an (undocumented) `query` param on
    # /users. Try it, but ALWAYS re-filter client-side: a build that ignores the
    # param returns the full list, which must not leak through as "matches".
    try:
        res = _request("GET", "/users", params={"query": q, "fields": fields, "$top": "200"})
        filtered = _filter(res)
        if filtered:
            return filtered
    except YouTrackError:
        pass

    return _filter(_request("GET", "/users", params={"fields": fields, "$top": "500"}))


@mcp.tool()
def get_current_user() -> dict:
    """Return the authenticated user (the token owner).

    Returns: {id, login, full_name, email, banned, online}
    """
    result = _request(
        "GET",
        "/users/me",
        params={"fields": "id,login,fullName,email,banned,online"},
    )
    if not isinstance(result, dict):
        raise YouTrackError(f"Unexpected get_current_user response shape: {type(result).__name__}")
    user = _map_user(result)
    user["banned"] = result.get("banned")
    user["online"] = result.get("online")
    return user


@mcp.tool()
def find_user_groups(query: str = "", limit: int = 50) -> list[dict]:
    """Find user groups by name.

    Args:
        query: Name substring to search for (empty returns all visible groups).
        limit: Max groups to return.

    Returns a list of {id, name, users_count}.
    """
    params: dict[str, str] = {"fields": "id,name,usersCount", "$top": str(limit)}
    if query and query.strip():
        params["query"] = query.strip()
    result = _request("GET", "/groups", params=params)
    if not isinstance(result, list):
        raise YouTrackError(f"Unexpected find_user_groups response shape: {type(result).__name__}")
    return [
        {"id": g.get("id"), "name": g.get("name"), "users_count": g.get("usersCount")}
        for g in result
        if isinstance(g, dict)
    ]


@mcp.tool()
def get_user_group_members(group_id: str, limit: int = 100, offset: int = 0) -> list[dict]:
    """List the members of a user group.

    Args:
        group_id: Internal group ID (from find_user_groups).
        limit: Max members to return.
        offset: Number of members to skip.

    Returns a list of {id, login, full_name, email}.
    """
    result = _request(
        "GET",
        f"/groups/{group_id}/users",
        params={"fields": "id,login,fullName,email", "$top": str(limit), "$skip": str(offset)},
    )
    if not isinstance(result, list):
        raise YouTrackError(f"Unexpected get_user_group_members response shape: {type(result).__name__}")
    return [_map_user(u) for u in result if isinstance(u, dict)]


# ──────────────────────────────────────────────────────────────────────────────
# Article (knowledge base) tools (JetBrains parity)
# ──────────────────────────────────────────────────────────────────────────────


@mcp.tool()
def create_article(
    summary: str,
    content: str = "",
    project_id: str | None = None,
    parent_article_id: str | None = None,
) -> dict:
    """Create a knowledge base article.

    Args:
        summary: Article title.
        content: Markdown body (optional).
        project_id: Project internal ID. Defaults to YOUTRACK_DEFAULT_PROJECT_ID.
        parent_article_id: Optional parent article ID to nest this under.

    Returns: {id, summary, url}
    """
    pid = project_id or YOUTRACK_DEFAULT_PROJECT_ID
    if not pid:
        raise YouTrackError("project_id is required (no default configured)")
    body: dict[str, Any] = {"project": {"id": pid}, "summary": summary, "content": content}
    if parent_article_id:
        body["parentArticle"] = {"id": parent_article_id}
    result = _request("POST", "/articles", body=body, params={"fields": "idReadable,summary"})
    if not isinstance(result, dict):
        raise YouTrackError(f"Unexpected create_article response shape: {type(result).__name__}")
    id_readable = result.get("idReadable", "")
    return {
        "id": id_readable,
        "summary": result.get("summary"),
        "url": _article_url(id_readable),
    }


@mcp.tool()
def get_article(article_id: str) -> dict:
    """Get a knowledge base article, including its sub-articles.

    Args:
        article_id: Readable ID or internal ID of the article.

    Returns the article content plus metadata, parent, and child articles.
    """
    result = _request(
        "GET",
        f"/articles/{article_id}",
        params={"fields": _ARTICLE_DETAIL_FIELDS},
    )
    if not isinstance(result, dict):
        raise YouTrackError(f"Unexpected get_article response shape: {type(result).__name__}")
    project = result.get("project") or {}
    reporter = result.get("reporter") or {}
    parent = result.get("parentArticle") or {}
    id_readable = result.get("idReadable", article_id)
    return {
        "id": id_readable,
        "summary": result.get("summary"),
        "content": result.get("content"),
        "project": project.get("name") if isinstance(project, dict) else None,
        "reporter": reporter.get("login") if isinstance(reporter, dict) else None,
        "created": _ms_to_iso(result.get("created")),
        "updated": _ms_to_iso(result.get("updated")),
        "parent_article": parent.get("idReadable") if isinstance(parent, dict) else None,
        "child_articles": [
            {"id": c.get("idReadable"), "summary": c.get("summary")}
            for c in (result.get("childArticles") or [])
            if isinstance(c, dict)
        ],
        "url": _article_url(id_readable),
    }


@mcp.tool()
def update_article(
    article_id: str,
    summary: str | None = None,
    content: str | None = None,
    parent_article_id: str | None = None,
) -> dict:
    """Update a knowledge base article's title, content, and/or parent.

    Only the provided arguments are changed.

    Args:
        article_id: Readable ID or internal ID of the article.
        summary: New title, or None to leave unchanged.
        content: New Markdown body, or None to leave unchanged.
        parent_article_id: New parent article ID, or None to leave unchanged.

    Returns: {ok, id, summary, url}
    """
    body: dict[str, Any] = {}
    if summary is not None:
        body["summary"] = summary
    if content is not None:
        body["content"] = content
    if parent_article_id is not None:
        body["parentArticle"] = {"id": parent_article_id}
    if not body:
        raise YouTrackError(
            "update_article: nothing to update "
            "(provide summary, content, and/or parent_article_id)"
        )
    result = _request(
        "POST",
        f"/articles/{article_id}",
        body=body,
        params={"fields": "idReadable,summary"},
    )
    if not isinstance(result, dict):
        raise YouTrackError(f"Unexpected update_article response shape: {type(result).__name__}")
    id_readable = result.get("idReadable", article_id)
    return {
        "ok": True,
        "id": id_readable,
        "summary": result.get("summary"),
        "url": _article_url(id_readable),
    }


@mcp.tool()
def search_articles(query: str, limit: int = 20) -> list[dict]:
    """Search knowledge base articles by title (case-insensitive substring).

    Note: YouTrack's REST API has no full-text query for articles, so this
    matches the `query` against article titles. It tries a server-side filter
    first and falls back to listing titles and matching client-side.

    Args:
        query: Text to match against article titles.
        limit: Max articles to return.

    Returns a list of {id, summary, project}.
    """
    q = (query or "").strip()
    ql = q.lower()
    fields = "idReadable,summary,project(name)"

    def _filter(articles: Any) -> list[dict]:
        out = []
        for a in articles if isinstance(articles, list) else []:
            if not isinstance(a, dict):
                continue
            if not ql or ql in (a.get("summary") or "").lower():
                project = a.get("project") or {}
                out.append({
                    "id": a.get("idReadable"),
                    "summary": a.get("summary"),
                    "project": project.get("name") if isinstance(project, dict) else None,
                })
            if len(out) >= limit:
                break
        return out

    # Try a server-side query (undocumented for articles), but always re-filter
    # client-side so an instance that ignores it can't return non-matching titles.
    try:
        res = _request("GET", "/articles", params={"query": q, "fields": fields, "$top": "200"})
        filtered = _filter(res)
        if filtered:
            return filtered
    except YouTrackError:
        pass

    return _filter(_request("GET", "/articles", params={"fields": fields, "$top": "500"}))


def main() -> None:
    """Console-script entry point declared in pyproject.toml."""
    mcp.run()


if __name__ == "__main__":
    main()
