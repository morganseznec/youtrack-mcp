# YouTrack MCP — setup

Lets Claude Code create / comment on / close YouTrack issues from any conversation.

## Prerequisites

- macOS or Linux
- [Claude Code](https://claude.com/claude-code) CLI installed
- [`uv`](https://github.com/astral-sh/uv) installed (`brew install uv` or see uv docs)
- Access to a YouTrack instance with a permanent token

## 1. Get your YouTrack token

In YouTrack, click your avatar → **Profile** → **Account Security** → **New token...**

- **Name:** `claude-mcp`
- **Scope:** YouTrack (the default scope is enough)

Copy the token — it looks like `perm-XXXX.YYYY.ZZZZ`. It's only shown once.

## 2. Register the server with Claude Code

Replace `<instance>`, `<token>`, and the default project ID with your values:

```bash
claude mcp add youtrack -s user \
  -e YOUTRACK_URL=https://<instance>.youtrack.cloud \
  -e YOUTRACK_TOKEN='perm-...' \
  -e YOUTRACK_DEFAULT_PROJECT_ID=0-1 \
  -- uvx --from git+https://github.com/<owner>/youtrack-mcp youtrack-mcp
```

- `YOUTRACK_URL` — your YouTrack base URL, no trailing slash, no `/api`.
- `YOUTRACK_TOKEN` — the permanent token from step 1. Single quotes protect against `=` and special chars.
- `YOUTRACK_DEFAULT_PROJECT_ID` — internal ID of your default project (format `<n>-<m>`). Optional: if omitted, every call must pass `project_id`. Find yours by calling `list_projects` from the MCP once.
- `-s user` registers it in your user-level `~/.claude.json`, so it's available across every project.
- `uvx --from git+https://...` runs the server straight from the repo — no local clone needed, updates pulled automatically.

Verify:

```bash
claude mcp list | grep youtrack
# expected: youtrack: ... - ✓ Connected
```

`✓ Connected` means Claude Code can spawn the server and the env vars are valid. If it says `✗`, check the env vars and the token.

## 3. Restart Claude Code

MCP tools are loaded at session start, so an already-open session won't see them. Quit and relaunch.

In a new session, ask Claude something like *"list my YouTrack projects"* — it should call the MCP and reply with your project list.

## 4. (Optional) Enable per-project automation

To make Claude proactively propose YouTrack tickets when working in a given repo, drop a `.youtrack.yml` at the root of that repo. Copy `youtrack.example.yml` and edit:

```yaml
project_id: "0-1"
language: "en"
auto_confirm: false      # set true for notify-only mode (no confirmations)
summary_template: "[{env}] {summary}"
variables:
  env: "prod"
custom_fields:
  Priority: "Normal"
  Type: "Bug"
```

The `youtrack-workflow` skill (also user-level, in `~/.claude/skills/youtrack-workflow/`) reads this file and orchestrates the search → propose → comment → close flow.

## Updating the server

With the `uvx --from git+...` install, just relaunch Claude Code — uvx fetches the latest commit on the next session start. To pin a specific version, append `@<tag-or-sha>`:

```
... -- uvx --from git+https://github.com/<owner>/youtrack-mcp@v1.0.0 youtrack-mcp
```

## Removing it

```bash
claude mcp remove youtrack -s user
```

## Troubleshooting

- **`✗` in `claude mcp list`** — usually the token is wrong, the URL has a trailing slash or `/api`, or `uvx` isn't on `PATH` in Claude's shell. Try running the same `uvx` command directly in a terminal to see the actual error.
- **`HTTP 401`** — token is invalid or revoked. Generate a new one.
- **`HTTP 404` on issue ops** — wrong issue ID format. Use either the readable form (`PROJ-42`) or the internal `2-128`.
- **Tool calls work but `create_issue` errors on a custom field** — the field name or value doesn't match your project schema. The server validates upfront and returns the allowed values; relay that to fix the input. You can also call `get_project_fields(project_id="...")` to inspect the full schema.
