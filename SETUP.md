# YouTrack MCP setup

Lets Claude Code create, comment on, and close YouTrack issues from any conversation.

## Prerequisites

- macOS or Linux
- [Claude Code](https://claude.com/claude-code) CLI installed
- [`uv`](https://github.com/astral-sh/uv) installed (`brew install uv` or see uv docs)
- Access to a YouTrack instance with a permanent token

## 1. Get your YouTrack token

Follow JetBrains' official guide: [Manage Permanent Token](https://www.jetbrains.com/help/youtrack/cloud/Manage-Permanent-Token.html) (self-hosted users: same flow, replace `/cloud/` with `/server/` in the URL).

Short version:

1. In YouTrack, click your avatar, then **Profile**.
2. Open the **Account Security** tab.
3. Click **New token...** in the *Tokens* section.
4. Set:
   - **Name:** `claude-mcp` (anything readable; helps you revoke later).
   - **Scope:** **YouTrack** (grants access to issues, tags, commands, comments, custom fields. This is the minimum scope this MCP needs).
5. Click **Create token**.
6. Copy the token immediately. It looks like `perm-XXXX.YYYY.ZZZZ` and **cannot be shown again**. Move on to step 2 to store it.

### What permissions does the MCP need?

The token can only do what your YouTrack user can do. The MCP calls these endpoints:

| Operation | YouTrack permission needed (per project) |
|---|---|
| `list_projects`, `get_project_fields` | Read Project |
| `search_issues` | Read Issue |
| `create_issue` | Create Issue |
| `add_comment` | Update Issue (Add Comment) |
| `close_issue` (state change via commands) | Update Issue (Apply Command) |

If your user already has the standard "Developer" or "Project Member" role on the projects you want to track from, you have everything you need. If you only want read access (search and inspect), you can use a token from a user who only has *Read Project* + *Read Issue* and the create/comment/close tools will simply fail with `HTTP 403` when called.

Admins can compare the built-in roles (Developer, Project Admin, Reporter, etc.) at [Permission Comparison for Default Roles](https://www.jetbrains.com/help/youtrack/cloud/permissions-comparison-for-default-roles.html). The full permission catalog is at [Permissions Reference](https://www.jetbrains.com/help/youtrack/cloud/youtrack-permissions-reference.html).

## 2. Choose how to store the token

The server accepts the token from three sources, in this order. Pick one based on how much friction vs. security you want.

### Option A. Inline env var (quick start, less secure)

The token sits in plaintext inside `~/.claude.json` (mode `600`, only readable by you). Fine for personal machines without unencrypted backups. Avoid if you sync `~/.claude.json` to a cloud drive.

To avoid putting the token in your shell history, type it interactively:

```bash
read -rs TOKEN && echo
claude mcp add youtrack -s user \
  -e YOUTRACK_URL=https://<instance>.youtrack.cloud \
  -e YOUTRACK_TOKEN="$TOKEN" \
  -e YOUTRACK_DEFAULT_PROJECT_ID=0-1 \
  -- uvx --from git+https://github.com/morganseznec/youtrack-mcp youtrack-mcp
unset TOKEN
```

`read -rs` does not echo the typed token, and the expanded value never reaches `~/.zsh_history`.

### Option B. Token file (better)

Store the token in a file with strict permissions. The MCP reads it lazily on each session start, so rotating the token is just rewriting the file.

```bash
# One-time setup
mkdir -p ~/.config/youtrack
read -rs > ~/.config/youtrack/token && echo
chmod 600 ~/.config/youtrack/token

# Register
claude mcp add youtrack -s user \
  -e YOUTRACK_URL=https://<instance>.youtrack.cloud \
  -e YOUTRACK_TOKEN_FILE=$HOME/.config/youtrack/token \
  -e YOUTRACK_DEFAULT_PROJECT_ID=0-1 \
  -- uvx --from git+https://github.com/morganseznec/youtrack-mcp youtrack-mcp
```

The token is no longer in `~/.claude.json`. Backups of `~/` still capture the file unless you exclude `~/.config/youtrack/`.

### Option C. OS keychain (recommended)

The token is stored encrypted at rest in your OS keychain. Nothing on disk in clear, nothing in `~/.claude.json`.

**macOS Keychain:**

```bash
# One-time: store the token (prompts for it without echoing)
security add-generic-password -a "$USER" -s youtrack-mcp -w

# Register
claude mcp add youtrack -s user \
  -e YOUTRACK_URL=https://<instance>.youtrack.cloud \
  -e YOUTRACK_TOKEN_CMD='security find-generic-password -s youtrack-mcp -w' \
  -e YOUTRACK_DEFAULT_PROJECT_ID=0-1 \
  -- uvx --from git+https://github.com/morganseznec/youtrack-mcp youtrack-mcp
```

The first time Claude Code spawns the MCP after a reboot, macOS may prompt you to allow `security` to read the keychain item. Accept once and check "Always allow".

To rotate: `security delete-generic-password -s youtrack-mcp` then re-add.

**Linux (GNOME / KDE / any provider implementing the Secret Service API):**

```bash
# One-time
secret-tool store --label="YouTrack MCP" service youtrack-mcp username "$USER"

# Register
claude mcp add youtrack -s user \
  -e YOUTRACK_URL=https://<instance>.youtrack.cloud \
  -e YOUTRACK_TOKEN_CMD='secret-tool lookup service youtrack-mcp username '"$USER" \
  -e YOUTRACK_DEFAULT_PROJECT_ID=0-1 \
  -- uvx --from git+https://github.com/morganseznec/youtrack-mcp youtrack-mcp
```

Any command that prints the token to stdout works, including 1Password CLI (`op read 'op://Private/YouTrack/token'`), `pass`, `bw get password youtrack`, etc.

### Reference table

| Env var | Source | When to use |
|---|---|---|
| `YOUTRACK_TOKEN` | Raw token string | Quick start, single machine |
| `YOUTRACK_TOKEN_FILE` | Path to a file containing only the token | Want token out of `~/.claude.json` |
| `YOUTRACK_TOKEN_CMD` | Shell command whose stdout is the token | Keychain, password manager |

Precedence: `YOUTRACK_TOKEN` wins, then `YOUTRACK_TOKEN_FILE`, then `YOUTRACK_TOKEN_CMD`. If none yields a non-empty token, the server exits with a clear error.

## 3. Other env vars

- `YOUTRACK_URL`: your YouTrack base URL, no trailing slash, no `/api`.
- `YOUTRACK_DEFAULT_PROJECT_ID`: internal ID of your default project (format `<n>-<m>`). Optional. If omitted, every call must pass `project_id`. To discover yours without setting up the MCP first, run `uvx --from git+https://github.com/morganseznec/youtrack-mcp youtrack-projects` (after providing a token via one of the three methods above).
- `-s user`: registers in your user-level `~/.claude.json`, available across every project.
- `uvx --from git+https://...`: runs the server straight from the repo. No local clone needed, updates pulled automatically on session start.

Verify:

```bash
claude mcp list | grep youtrack
# expected: youtrack: ... - ✓ Connected
```

`✓ Connected` means Claude Code can spawn the server and the credentials resolve. If it says `✗`, check `YOUTRACK_URL`, the token source, and that the binary referenced by `YOUTRACK_TOKEN_CMD` is on `PATH`.

## 4. Restart Claude Code

MCP tools are loaded at session start, so an already-open session will not see them. Quit and relaunch.

In a new session, ask Claude something like *"list my YouTrack projects"*. It should call the MCP and reply with your project list.

## 5. (Optional) Enable per-project automation

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

The `youtrack-workflow` skill (also user-level, in `~/.claude/skills/youtrack-workflow/`) reads this file and orchestrates the search, propose, comment, close flow.

## Updating the server

With the `uvx --from git+...` install, just relaunch Claude Code. uvx fetches the latest commit on the next session start. To pin a specific version, append `@<tag-or-sha>`:

```
... -- uvx --from git+https://github.com/morganseznec/youtrack-mcp@v0.1.0 youtrack-mcp
```

## Removing it

```bash
claude mcp remove youtrack -s user
```

If you used Option C, remember to remove the keychain entry too:
- macOS: `security delete-generic-password -s youtrack-mcp`
- Linux: `secret-tool clear service youtrack-mcp username "$USER"`

## Troubleshooting

- **`✗` in `claude mcp list`**: usually the token is wrong, the URL has a trailing slash or `/api`, or `uvx` is not on `PATH` in Claude's shell. Try running the same `uvx` command directly in a terminal to see the actual error.
- **`HTTP 401`**: token is invalid or revoked. Generate a new one.
- **`HTTP 404` on issue ops**: wrong issue ID format. Use either the readable form (`PROJ-42`) or the internal `2-128`.
- **`YOUTRACK_TOKEN_CMD failed`**: the command exited non-zero. Run the same command in your terminal to see why (often the keychain item is missing or named differently).
- **`YOUTRACK_TOKEN_FILE could not be read`**: the path does not exist or the file is not readable by the process. Check permissions.
- **Tool calls work but `create_issue` errors on a custom field**: the field name or value does not match your project schema. The server validates upfront and returns the allowed values; relay that to fix the input. You can also call `get_project_fields(project_id="...")` to inspect the full schema.
