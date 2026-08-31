# YouTrack MCP

[Model Context Protocol](https://modelcontextprotocol.io/) server for [YouTrack](https://www.jetbrains.com/youtrack/). Lets Claude Code (and any other MCP client) manage YouTrack issues, articles, time tracking, and more directly from your conversations.

## Features

- **28 MCP tools** covering issues (create, read, update, comment, link, tag, assign, close, time-track, attach & download files), knowledge base articles (CRUD + search), projects, users, groups, and saved searches. This matches and extends the capability surface of [JetBrains' official YouTrack MCP server](https://www.jetbrains.com/help/youtrack/server/model-context-protocol-server.html). See the [Tool reference](#tool-reference-for-developers) for the full list.
- **Structured output for programmatic clients**: every tool declares an `outputSchema` and returns `structuredContent` (readable text stays for LLM clients), and failures come back as a normalized `{"error": {"code", "message", ...}}` envelope with retryable flags instead of raising. Lets an orchestrator drive the server over JSON-RPC/stdio and validate every response. See [Tool reference](#tool-reference-for-developers).
- **Automatic evidence trail**: post factual, attributed proof comments (and attach reports/logs) to the ticket behind a branch or MR, from the Claude Code session or from CI. See [Evidence trail](#evidence-trail-proof-on-tickets).
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

## Using it from Claude Code

You never call these tools directly. You talk to Claude in natural language and Claude picks the right tool based on what you ask. Here are common prompts and what they trigger behind the scenes:

| What you type | What Claude does |
|---|---|
| *"list my YouTrack projects"* | `list_projects()` |
| *"what custom fields are available on project IS?"* | `get_project_fields("0-19")` |
| *"is there a `.youtrack.yml` in this repo?"* | `find_youtrack_config(cwd)` |
| *"find open tickets that mention 'timeout'"* | `search_issues("project: IS summary: timeout #Unresolved")` |
| *"create a ticket for the login bug"* | `create_issue(...)` (drafts a title and description, then creates it) |
| *"add a comment to IS-87 saying we're investigating"* | `add_comment("IS-87", "...")` |
| *"show me the full details of IS-87"* | `get_issue("IS-87")` (description, comments, and attachment list) |
| *"download the log attached to IS-87 so I can investigate"* | `get_issue("IS-87")` for the attachment id, then `download_attachment("IS-87", "a-1")` |
| *"set IS-87 to Critical priority"* | `update_issue("IS-87", custom_fields={"Priority": "Critical"})` |
| *"assign IS-87 to morgan"* | `change_issue_assignee("IS-87", "morgan.s")` |
| *"mark IS-87 as a duplicate of IS-12"* | `link_issues("IS-87", "IS-12", "duplicates")` |
| *"log 90 minutes on IS-87 for the investigation"* | `log_work("IS-87", minutes=90, text="...")` |
| *"close IS-87 with a comment about the fix"* | `add_comment(...)` then `close_issue("IS-87")` |
| *"create and close a ticket recapping the work we just did"* | `create_and_close_issue(...)` |

If you have a [`.youtrack.yml`](#per-project-automation-optional) in the repo, the [companion skill](#companion-skill-for-claude-code) kicks in automatically: it detects task-shaped phrasing (*"we need to add…"*, *"there's a bug in…"*, *"let's refactor…"*, *"TODO: …"*) and proposes or creates a ticket without you having to say *"create a ticket"* explicitly. With `auto_confirm: true`, it acts silently and just reports the YouTrack URL. The skill triggers on any language Claude understands (English, French, Spanish, German…); set `language` in your `.youtrack.yml` to control what language the ticket body is written in.

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

1. Detects task-shaped phrasing in the user's prompt (`"there's a bug"`, `"we need to add"`, `"TODO"`, …) in any language Claude understands
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

## Evidence trail (proof on tickets)

Attach **proof of what ran and what the result was** to the YouTrack issue behind a branch or MR: test summaries, attached JUnit/coverage reports, logs, pipeline links. Useful for change management and ISO/SOC2-style review, where a ticket should carry evidence that a change was tested before it shipped.

Three complementary paths, usable together:

1. **In the Claude Code session.** Set `evidence.enabled: true` in `.youtrack.yml`. The [companion skill](#companion-skill-for-claude-code) posts a short, attributed comment at checkpoints (tests ran, build done, commit pushed) and attaches artifacts via `attach_file`. It never fabricates a result and always labels the comment as coming from the local session. When the work is in a PR/MR, Claude bridges the two sides: it reads the PR/MR and CI status and downloads artifacts (coverage, JUnit, screenshots) using the `gh` / `glab` CLI (which work on any tier, GitLab Free included) or a connected GitHub/GitLab MCP, attaches them to the ticket, and links the YouTrack issue back onto the PR/MR. GitLab's official MCP is Premium/Ultimate only, so `glab` is the universal path there.
2. **From CI (unattended).** Drop [`examples/ci/youtrack-evidence.sh`](examples/ci/youtrack-evidence.sh) into your pipeline. It finds the issue from the branch / MR / commit, posts the pipeline status, attaches a report, and can transition the ticket. Ready-made [GitLab](examples/ci/gitlab-ci.example.yml) and [GitHub Actions](examples/ci/github-actions.example.yml) snippets are included.
3. **YouTrack-native commit commands.** Zero custom code: connect the repo in YouTrack and write `IS-87 #Fixed` in commit messages. See [examples/ci/README.md](examples/ci/README.md) for all three.

The guiding principle: evidence should be **verifiable and attributed**, not asserted. The agent and the CI script anchor every comment to a commit SHA and a pipeline/source link, and prefer attaching a real report over paraphrasing it.

## Tool reference (for developers)

> This section is for developers building another MCP client or wiring the server into a different agent. End users of Claude Code should read [Using it from Claude Code](#using-it-from-claude-code) instead.

The MCP exposes 28 tools. They are prefixed `mcp__youtrack__` in Claude Code tool calls.

**Structured output.** Every tool returns a `structuredContent` block with a declared `outputSchema` (a programmatic client reads only that; the readable text stays for LLM clients). Failures return `{"error": {"code", "message", "youtrack_status?", "retryable"}}` in `structuredContent` instead of raising, with codes `NOT_FOUND` / `PERMISSION_DENIED` / `VALIDATION_FAILED` / `RATE_LIMITED` / `YOUTRACK_UNAVAILABLE` (the last two `retryable`). List tools return `{items, count}` (or `{results, total}` for `search_issues`).

**Issues**

| Tool | Purpose |
|---|---|
| `create_issue(summary, description?, project_id?, custom_fields?, tags?, idempotency_key?)` | Create a new issue. Validates custom fields against the project schema. `idempotency_key` dedupes via an `idem:{key}` tag (returns the existing issue with `idempotent_hit`) |
| `get_issue(issue_id, include_comments?, include_attachments?, max_comments?)` | The rich read for triage: full (32 KB-truncated) description, flattened custom fields, reporter, tags, links, timestamps, inline comments, and attachment metadata |
| `update_issue(issue_id, summary?, description?, custom_fields?, add_tags?, remove_tags?, create_missing_tags?)` | Triage in place: set fields (fuzzy-matched, `"__CLEAR__"` clears), add/remove tags. Returns the issue plus an `applied` block |
| `change_issue_assignee(issue_id, assignee?)` | Assign to a user login, or unassign with `None`/`""` |
| `create_draft_issue(summary, description?, project_id?)` | Create a private draft (semi-public YouTrack API) |
| `add_comment(issue_id, text, attachments?)` | Append a Markdown comment, optionally uploading local files with it |
| `get_issue_comments(issue_id, limit?, offset?)` | List comments, paginated |
| `attach_file(issue_id, file_path, file_name?)` | Upload a local file (test report, log, coverage, screenshot) as an attachment |
| `download_attachment(issue_id, attachment_id, dest_path?, max_size_bytes?)` | Download an attachment (log, screenshot, Sentry report) to disk; returns `path`, `sha256`, and a `text_preview` for text files. Refuses > 10 MB |
| `link_issues(issue_id, target_issue_id, link_type?)` | Link two issues. `link_type`: `relates to`, `depends on`, `is required for`, `duplicates`, `is duplicated by`, `subtask of`, `parent for` |
| `manage_issue_tags(issue_id, add?, remove?, create_missing?)` | Add/remove tags by name. Creates unknown add-tags only when `create_missing` is set |
| `log_work(issue_id, minutes?, duration?, text?, date?, work_type?)` | Log time. Accepts `minutes` or a `"1h 30m"` string |
| `close_issue(issue_id, comment?, state?)` | Close. `state=None` auto-picks the project's canonical "done" state |
| `create_and_close_issue(summary, ..., closing_comment?, state?)` | One-shot for after-the-fact tracking |
| `search_issues(query, limit?, offset?, fields?)` | YouTrack query syntax; `fields` = `"minimal"`/`"standard"`. Returns `{results, total}`. The `project:` operator expects the SHORT NAME (e.g. `IS`) |
| `get_saved_issue_searches()` | List the current user's saved searches |

**Knowledge base articles**

| Tool | Purpose |
|---|---|
| `create_article(summary, content?, project_id?, parent_article_id?)` | Create an article, optionally nested under a parent |
| `get_article(article_id)` | Article content plus parent and child articles |
| `update_article(article_id, summary?, content?, parent_article_id?)` | Update title, content, and/or parent |
| `search_articles(query, limit?)` | Title substring search (YouTrack has no full-text article query) |

**Projects, users, groups**

| Tool | Purpose |
|---|---|
| `list_projects()` | Discover project IDs |
| `get_project(project_id)` | Project details: name, short name, description, leader, archived |
| `get_project_fields(project_id)` | Inspect custom fields and allowed values |
| `find_user(query, limit?)` | Find users by login / full name / email |
| `get_current_user()` | The authenticated token owner |
| `find_user_groups(query?, limit?)` | Find user groups by name |
| `get_user_group_members(group_id, limit?, offset?)` | List a group's members |

**Local config (this server only)**

| Tool | Purpose |
|---|---|
| `find_youtrack_config(start_path)` | Locate the nearest `.youtrack.yml` walking up from start_path |

> **Naming vs JetBrains.** This server keeps its original short names for the tools that predate the JetBrains MCP: `add_comment` (JetBrains: `add_issue_comment`), `list_projects` (`find_projects`), `get_project_fields` (`get_issue_fields_schema`). All other tool names match JetBrains' server. `find_youtrack_config`, `create_and_close_issue`, `close_issue`, `attach_file`, and `download_attachment` are extras with no JetBrains equivalent.

## Requirements

- Python 3.10+
- MCP Python SDK 2.x (installed automatically; since v0.6.0 the server targets `mcp>=2,<3`)
- [`uv`](https://github.com/astral-sh/uv) for the recommended install path
- A YouTrack instance with a permanent token

## Versioning and changelog

This project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). See [CHANGELOG.md](CHANGELOG.md) for the history of each release.

## License

MIT. See [LICENSE](LICENSE).
