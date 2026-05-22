"""YouTrack MCP server.

Exposes tools to create, comment on, close, and search YouTrack issues.

Env vars (read at startup):
  YOUTRACK_URL                Base URL (e.g. https://<instance>.youtrack.cloud)
  YOUTRACK_TOKEN              Permanent token (Bearer)
  YOUTRACK_DEFAULT_PROJECT_ID Optional default project ID (e.g. "0-1")
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from threading import Lock
from typing import Any

import yaml
from mcp.server.fastmcp import FastMCP

CONFIG_FILENAME = ".youtrack.yml"
CONFIG_FILENAMES = (".youtrack.yml", ".youtrack.yaml")

YOUTRACK_URL = os.environ.get("YOUTRACK_URL", "").rstrip("/")
YOUTRACK_TOKEN = os.environ.get("YOUTRACK_TOKEN", "")
YOUTRACK_DEFAULT_PROJECT_ID = os.environ.get("YOUTRACK_DEFAULT_PROJECT_ID", "")

if not YOUTRACK_URL or not YOUTRACK_TOKEN:
    raise SystemExit("YOUTRACK_URL and YOUTRACK_TOKEN must be set")

mcp = FastMCP("youtrack")


class YouTrackError(Exception):
    pass


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
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8")
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise YouTrackError(f"HTTP {e.code} on {method} {path}: {detail}") from e


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
    fields = []
    for entry in raw or []:
        f = entry.get("field") or {}
        bundle = entry.get("bundle") or {}
        values = [v.get("name") for v in (bundle.get("values") or []) if v.get("name")]
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
    (matching the project's casing/spelling) or None if no match.
    """
    if not allowed:
        return user_input  # field is open-valued
    for v in allowed:
        if v == user_input:
            return v
    lower_input = user_input.lower()
    for v in allowed:
        if v.lower() == lower_input:
            return v
    norm = _normalize(user_input)
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
    pid = ((result or {}).get("project") or {}).get("id")
    if not pid:
        raise YouTrackError(f"Could not resolve project for issue '{issue_id}'")
    return pid


# Map YouTrack field types to the $type discriminator the create payload needs.
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


def _build_custom_fields_payload(
    fields_input: dict[str, Any] | None,
    project_id: str,
) -> list[dict]:
    """Translate {field_name: value} into YouTrack's customFields payload list.

    Validates every entry against the project schema:
      - Unknown field name → raises YouTrackError listing available fields.
      - Enum/state value that doesn't match (after case-insensitive + normalized
        fuzzy match) → raises YouTrackError listing allowed values.

    Values can be:
      - str  (enum/state name, user login, simple string)
      - list (multi-enum / multi-user)
      - None (clear the field)
    """
    if not fields_input:
        return []

    try:
        schema = _get_project_schema_cached(project_id)
    except YouTrackError:
        schema = {"project_id": project_id, "fields": []}

    field_by_name = {f["name"]: f for f in schema.get("fields", []) if f.get("name")}
    available = sorted(field_by_name.keys())

    payload = []
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
            entry = {"name": name, "$type": ytype, "value": [{"name": v} for v in resolved]}
        elif ytype in ("SimpleIssueCustomField", "TextIssueCustomField", "PeriodIssueCustomField"):
            entry = {"name": name, "$type": ytype, "value": value}
        elif "User" in ytype:
            entry = {"name": name, "$type": ytype, "value": {"login": value}}
        else:
            m = _match_value(value, allowed) if allowed else value
            if allowed and m is None:
                raise YouTrackError(
                    f"Value '{value}' invalid for field '{name}' on project {project_id}. "
                    f"Allowed: {', '.join(allowed)}"
                )
            entry = {"name": name, "$type": ytype, "value": {"name": m}}

        payload.append(entry)

    return payload


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
        tags: Optional list of tag names to attach to the issue.

    Returns: {id, id_readable, summary, url}
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
    return {
        "id": result.get("id"),
        "id_readable": result.get("idReadable"),
        "summary": result.get("summary"),
        "url": _issue_url(result.get("idReadable", "")),
    }


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

    Returns: {ok, issue_id, state, project_id}
    """
    project_id = _get_project_id_from_issue(issue_id)
    schema = _get_project_schema_cached(project_id)
    state_field = next(
        (f for f in schema["fields"] if f.get("name") == "State"),
        None,
    )
    allowed = (state_field or {}).get("values") or []

    target_state = _resolve_state(state, allowed)
    if not target_state:
        if allowed:
            raise YouTrackError(
                f"Cannot resolve state '{state}' for project {project_id}. "
                f"Allowed: {', '.join(allowed)}"
            )
        target_state = state or "Fixed"  # last resort if project has no State field

    body: dict[str, Any] = {"query": f"State {target_state}"}
    if comment:
        body["comment"] = comment
    _request("POST", f"/issues/{issue_id}/commands", body=body)
    return {
        "ok": True,
        "issue_id": issue_id,
        "state": target_state,
        "project_id": project_id,
        "requested_state": state,
    }


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
    create_fn = create_issue.fn if hasattr(create_issue, "fn") else create_issue
    close_fn = close_issue.fn if hasattr(close_issue, "fn") else close_issue
    issue = create_fn(
        summary=summary,
        description=description,
        project_id=project_id,
        custom_fields=custom_fields,
        tags=tags,
    )
    close_result = close_fn(
        issue_id=issue["id_readable"],
        comment=closing_comment,
        state=state,
    )
    issue["state"] = close_result["state"]
    issue["closed"] = True
    return issue


@mcp.tool()
def search_issues(query: str, limit: int = 10) -> list[dict]:
    """Search issues using YouTrack query syntax.

    Examples:
        "project: 0-1 #Unresolved"
        "assignee: me State: Open"
        "summary: deploy created: 2026-05"
    """
    params = {
        "query": query,
        "$top": str(limit),
        "fields": "idReadable,summary,resolved,reporter(login),customFields(name,value(name))",
    }
    result = _request("GET", "/issues", params=params)
    return [
        {
            "id": item.get("idReadable"),
            "summary": item.get("summary"),
            "resolved": item.get("resolved") is not None,
            "url": _issue_url(item.get("idReadable", "")),
        }
        for item in result
    ]


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
    return [
        {"id": p.get("id"), "name": p.get("name"), "short_name": p.get("shortName")}
        for p in result
    ]


def main() -> None:
    """Console-script entry point declared in pyproject.toml."""
    mcp.run()


if __name__ == "__main__":
    main()
