# YouTrack MCP

[Model Context Protocol](https://modelcontextprotocol.io/) server for [YouTrack](https://www.jetbrains.com/youtrack/). Lets Claude Code (and any other MCP client) create, comment on, search, and close YouTrack issues directly from your conversations.

## Features

- **8 MCP tools**: `create_issue`, `add_comment`, `close_issue`, `create_and_close_issue`, `search_issues`, `list_projects`, `get_project_fields`, `find_youtrack_config`
- **Per-project configuration** via a `.youtrack.yml` file dropped at the root of a repo: language, summary/description templates, custom field defaults, opt-in automation
- **Server-side validation** of custom field values against the live project schema, with case-insensitive and punctuation-normalized fuzzy matching (`critical` → `Critical`, `won't fix` → `Won't fix`)
- **Auto-resolution of close state** by querying the project's actual State field. No need to hardcode `Fixed` vs `Done` vs `Resolved`.
- **Schema cache** with TTL to avoid hammering the API
- **`youtrack-projects` CLI helper** to list project IDs (YouTrack's UI doesn't expose them)
- **Optional companion skill** for Claude Code that automates the search, create, comment, close lifecycle, with a `notify-only` mode that acts without prompts

## Quick install

```bash
read -rs TOKEN && echo
claude mcp add youtrack -s user \
  -e YOUTRACK_URL=https://<instance>.youtrack.cloud \
  -e YOUTRACK_TOKEN="$TOKEN" \
  -e YOUTRACK_DEFAULT_PROJECT_ID=0-1 \
  -- uvx --from git+https://github.com/morganseznec/youtrack-mcp youtrack-mcp
unset TOKEN
```

`read -rs` takes the token without echoing it and keeps the expanded value out of your shell history.

Create your token by following JetBrains' guide: [Manage Permanent Token](https://www.jetbrains.com/help/youtrack/cloud/Manage-Permanent-Token.html). Use the **YouTrack** scope. The token can only do what your user can do per project, so make sure you have Read/Create/Update Issue on the projects you want to track. Verify the install with `claude mcp list | grep youtrack`; it should show `✓ Connected`.

**Want better security?** The server also reads the token from a file (`YOUTRACK_TOKEN_FILE`) or a command (`YOUTRACK_TOKEN_CMD`, ideal for macOS Keychain / `secret-tool` / 1Password CLI), so it never has to sit in `~/.claude.json`. See [SETUP.md](SETUP.md) section 2 for the three options.

## Helper: list project IDs

YouTrack doesn't expose internal project IDs in its UI. Run this to see yours:

```bash
uvx --from git+https://github.com/morganseznec/youtrack-mcp youtrack-projects
```

Output (aligned columns):

```
ID    SHORT     NAME
----  --------  ----
0-1   PROJ      My Project
0-2   API       Backend API
...
```

Reads `YOUTRACK_URL` and `YOUTRACK_TOKEN` from your environment, or falls back to the values you already gave `claude mcp add youtrack` (read from `~/.claude.json`), so once the MCP is registered you don't need to re-export anything.

## Per-project automation (optional)

Drop a `.youtrack.yml` at the root of a repo to opt that project into automatic ticket tracking:

```yaml
project_id: "0-1"
language: "en"
auto_confirm: false      # true = notify-only mode (act without confirmations)

summary_template: "[{env}] {summary}"
description_template: |
  ## Context
  {description}
variables:
  env: "prod"

custom_fields:
  Priority: "Normal"
  Type: "Bug"
```

See **[youtrack.example.yml](youtrack.example.yml)** for the full annotated schema.

## Companion skill for Claude Code

A user-level skill at `~/.claude/skills/youtrack-workflow/` reads the `.youtrack.yml` and orchestrates the workflow:

1. Detects task language in the user's prompt (`"there's a bug"`, `"il faut corriger"`, `"TODO"`...)
2. Searches YouTrack for an existing matching issue
3. Either proposes (`auto_confirm: false`) or directly creates/updates (`auto_confirm: true`)
4. Tracks the issue through completion, then comments and closes with a recap

To install (one-liner, no clone required):

```bash
mkdir -p ~/.claude/skills/youtrack-workflow && \
  curl -fsSL https://raw.githubusercontent.com/morganseznec/youtrack-mcp/main/skills/youtrack-workflow/SKILL.md \
  -o ~/.claude/skills/youtrack-workflow/SKILL.md
```

Then restart Claude Code so the skill is picked up at session start.

## Tool reference

| Tool | Purpose |
|---|---|
| `create_issue(summary, description, project_id?, custom_fields?, tags?)` | Create a new issue. Validates custom field names and values against the project schema |
| `add_comment(issue_id, text)` | Append a Markdown comment |
| `close_issue(issue_id, comment?, state?)` | Close. `state=None` auto-picks the project's canonical "done" state |
| `create_and_close_issue(summary, ..., closing_comment?, state?)` | One-shot for after-the-fact tracking |
| `search_issues(query, limit?)` | YouTrack query syntax (`project: 0-1 #Unresolved`, etc.) |
| `list_projects()` | Discover project IDs |
| `get_project_fields(project_id)` | Inspect custom fields and allowed values |
| `find_youtrack_config(start_path)` | Locate the nearest `.youtrack.yml` walking up from start_path |

## Requirements

- Python 3.10+
- [`uv`](https://github.com/astral-sh/uv) for the recommended install path
- A YouTrack instance with a permanent token

## License

MIT. See [LICENSE](LICENSE).
