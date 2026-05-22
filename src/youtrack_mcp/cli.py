"""Standalone CLI helpers. Run without spawning the full MCP server.

Entry points (declared in pyproject.toml):
  youtrack-projects   List all YouTrack projects with their IDs.

Env resolution order:
  1. YOUTRACK_URL and a token source in the process environment. A token source
     is one of YOUTRACK_TOKEN (raw), YOUTRACK_TOKEN_FILE (path), or
     YOUTRACK_TOKEN_CMD (command whose stdout is the token).
  2. mcpServers.youtrack.env in ~/.claude.json (so installs registered via
     `claude mcp add` work out of the box without re-exporting credentials).
"""

import json
import os
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def _load_env_from_claude_config() -> None:
    """Populate YOUTRACK_* from ~/.claude.json if not already in environ."""
    if os.environ.get("YOUTRACK_URL") and os.environ.get("YOUTRACK_TOKEN"):
        return
    cfg_path = Path.home() / ".claude.json"
    if not cfg_path.is_file():
        return
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    env = (
        data.get("mcpServers", {})
        .get("youtrack", {})
        .get("env", {})
    )
    for key, value in env.items():
        os.environ.setdefault(key, value)


def _resolve_token() -> str:
    """Read the token from YOUTRACK_TOKEN, then YOUTRACK_TOKEN_FILE, then YOUTRACK_TOKEN_CMD."""
    direct = os.environ.get("YOUTRACK_TOKEN", "").strip()
    if direct:
        return direct

    file_path = os.environ.get("YOUTRACK_TOKEN_FILE", "").strip()
    if file_path:
        try:
            return Path(file_path).expanduser().read_text(encoding="utf-8").strip()
        except OSError as e:
            sys.stderr.write(f"YOUTRACK_TOKEN_FILE '{file_path}' could not be read: {e}\n")
            sys.exit(2)

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
            sys.stderr.write(
                f"YOUTRACK_TOKEN_CMD failed (exit {e.returncode}): {e.stderr.strip()}\n"
            )
            sys.exit(2)
        except subprocess.TimeoutExpired:
            sys.stderr.write("YOUTRACK_TOKEN_CMD timed out after 10s\n")
            sys.exit(2)
        except FileNotFoundError as e:
            sys.stderr.write(f"YOUTRACK_TOKEN_CMD: {e}\n")
            sys.exit(2)

    return ""


def _require_env() -> tuple[str, str]:
    _load_env_from_claude_config()
    url = os.environ.get("YOUTRACK_URL", "").rstrip("/")
    token = _resolve_token()
    if not url or not token:
        sys.stderr.write(
            "Missing YOUTRACK_URL or a token source.\n"
            "Provide a token via YOUTRACK_TOKEN, YOUTRACK_TOKEN_FILE, or YOUTRACK_TOKEN_CMD,\n"
            "or register the MCP first with:\n"
            "  claude mcp add youtrack -s user -e YOUTRACK_URL=... -e YOUTRACK_TOKEN=... -- ...\n"
        )
        sys.exit(2)
    return url, token


def _redact(text: str, token: str) -> str:
    if not text or not token:
        return text
    return text.replace(token, "<redacted>").replace(
        f"Bearer {token}", "Bearer <redacted>"
    )


def _http_get(url: str, token: str, path: str, params: dict[str, str]) -> Any:
    full = f"{url}/api{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        full,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = _redact(e.read().decode("utf-8", errors="replace"), token)
        sys.stderr.write(f"HTTP {e.code}: {body}\n")
        sys.exit(1)
    except urllib.error.URLError as e:
        sys.stderr.write(f"Network error: {e.reason}\n")
        sys.exit(1)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"Invalid JSON in response: {e}\n")
        sys.exit(1)


def list_projects() -> None:
    """Print every YouTrack project as: ID  SHORT  NAME (aligned columns)."""
    url, token = _require_env()
    projects = _http_get(
        url,
        token,
        "/admin/projects",
        {"fields": "id,name,shortName", "$top": "200"},
    )

    if not isinstance(projects, list):
        sys.stderr.write(
            f"Unexpected response shape: {type(projects).__name__}\n"
        )
        sys.exit(1)
    if not projects:
        print("No projects found (token may not have read access).")
        return

    rows = [
        (
            p.get("id") or "",
            p.get("shortName") or "-",
            p.get("name") or "",
        )
        for p in projects
        if isinstance(p, dict)
    ]
    id_w = max(len(r[0]) for r in rows)
    short_w = max(len(r[1]) for r in rows)

    print(f"{'ID':<{id_w}}  {'SHORT':<{short_w}}  NAME")
    print(f"{'-' * id_w}  {'-' * short_w}  {'-' * 4}")
    for pid, short, name in rows:
        print(f"{pid:<{id_w}}  {short:<{short_w}}  {name}")
    print(f"\n{len(rows)} project(s) on {url}")
