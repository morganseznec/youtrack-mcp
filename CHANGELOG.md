# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-06-11

### Added

An **automatic evidence trail**: attach factual, attributed proof of work (test results, reports, logs, pipeline links) to the YouTrack issue behind a branch or MR, from the Claude Code session and/or from CI. Useful for change management and ISO/SOC2-style review.

- **`attach_file(issue_id, file_path, file_name?)`** tool (27 tools total). Uploads a local file to an issue via `POST /api/issues/{id}/attachments` (multipart). A new `_request_multipart` helper handles the upload; the shared HTTP error/redaction path was extracted into `_send` and reused. Reads are capped at 50 MB.
- **Evidence behavior in the `youtrack-workflow` skill.** A new section documents when to post evidence (tests / build / commit / deploy checkpoints), how to resolve the issue from the branch / commit / MR (`IS-\d+`), an idempotency rule (one comment per commit + checkpoint via `get_issue_comments`), and a structured, language-aware comment format. Integrity rules are explicit: never fabricate a result, always state provenance ("session locale Claude Code"), and anchor to the commit SHA. Artifacts are attached via `attach_file` when available.
- **`evidence` config block** in `.youtrack.yml` (`enabled`, `on`, `attach_artifacts`), wired into `find_youtrack_config` defaults and documented in `youtrack.example.yml`.
- **CI integration under `examples/ci/`.** A provider-agnostic `youtrack-evidence.sh` (auto-detects GitLab CI and GitHub Actions: resolves the issue, posts the pipeline result, optionally attaches a report and applies a YouTrack command), plus drop-in `gitlab-ci.example.yml` and `github-actions.example.yml`. Best-effort by default (never fails the build) and idempotent per pipeline. The accompanying README also documents the zero-code **YouTrack-native commit-command** path (`IS-87 #Fixed`).

### Notes

- CI and test-parsing logic deliberately live in the pipeline / skill, not in the MCP server, which stays a thin YouTrack API layer. The CI script talks to the same REST endpoints the MCP uses.
- `attach_file` is our own evidence-oriented tool with no JetBrains MCP equivalent.

## [0.3.0] - 2026-06-11

### Added

Feature parity with (and a superset of) [JetBrains' official YouTrack MCP server](https://www.jetbrains.com/help/youtrack/server/model-context-protocol-server.html). The server now exposes **26 tools** (up from 8). New tools, all matching JetBrains' names:

- **Issue reads and edits.** `get_issue` (full details with flattened custom fields, tags, links, and ISO timestamps), `update_issue` (summary / description / custom fields, with the same schema validation as `create_issue`), `change_issue_assignee`, and `create_draft_issue`.
- **Comments and links.** `get_issue_comments` (paginated) and `link_issues` (typed relations via the commands endpoint: `relates to`, `depends on`, `is required for`, `duplicates`, `is duplicated by`, `subtask of`, `parent for`, with friendly aliases).
- **Tags.** `manage_issue_tags` adds/removes tags by name, resolving each to its database id (YouTrack requires the id and never auto-creates tags). Unknown names are reported back rather than failing the whole call.
- **Time tracking.** `log_work` accepts either `minutes` or a human `"1h 30m"` duration string, with optional date and work-item type (resolved against the project's time-tracking settings).
- **Knowledge base.** `create_article`, `get_article`, `update_article`, and `search_articles` (title substring search, since YouTrack exposes no full-text article query).
- **Discovery.** `get_project`, `find_user`, `get_current_user`, `find_user_groups`, `get_user_group_members`, and `get_saved_issue_searches`.
- **Pagination** (`offset`) added to `search_issues`, matching JetBrains' offset/limit.
- **62 new unit tests** (108 total) covering duration parsing, link-type resolution, custom-field value flattening, date/timestamp conversion, client-side filtering of `find_user` / `search_articles`, and the exact request payloads for `update_issue`, `change_issue_assignee`, `link_issues`, `manage_issue_tags`, `log_work`, and `get_issue`.

### Notes

- The existing tools (`add_comment`, `list_projects`, `get_project_fields`) keep their original short names for backward compatibility. JetBrains calls these `add_issue_comment`, `find_projects`, and `get_issue_fields_schema` respectively.
- `create_draft_issue` uses YouTrack's `/admin/users/me/drafts` endpoint, which JetBrains documents as semi-public and subject to change. Prefer `create_issue` for normal use.
- Not yet implemented: per-call visibility restrictions (`permittedUsers` / `permittedGroups`) that JetBrains supports on `create_issue` / `add_comment` / `create_article`.

## [0.2.0] - 2026-05-22

### Security

- **Token redaction in error messages.** `_request` now strips any occurrence of the live token (and `Bearer <token>`) from HTTP error bodies before raising. Previously, if YouTrack or a proxy echoed the request headers back in a 401 / 403 body, the token would propagate up to the MCP client and could end up in chat transcripts. Same protection added to the `youtrack-projects` CLI.

### Fixed

- **Multi-word state values broken in `close_issue`.** `State In Progress` was being sent to the YouTrack commands endpoint and parsed as two arguments, silently failing or matching the wrong field. The resolved state is now wrapped in braces (`State {In Progress}`) so YouTrack treats it as a single literal token. Affects all states with spaces or apostrophes: `In Progress`, `Won't fix`, `Will not fix`, etc.

- **`State` custom field missing inner `$type` discriminator.** On stricter YouTrack instances, `StateIssueCustomField` payloads require `value: {"name": ..., "$type": "StateBundleElement"}`. Same for enum fields (`EnumBundleElement`), owned fields, version fields, and build fields. The new `_INNER_VALUE_TYPE_MAP` adds the inner `$type` automatically for every bundle-element-shaped field.

- **`create_and_close_issue` no longer depends on FastMCP internals.** The previous implementation reached into `create_issue.fn` / `close_issue.fn`, undocumented FastMCP attributes that could disappear in a future release. Extracted private `_create_issue_impl` / `_close_issue_impl` helpers that both the `@mcp.tool` wrappers and the combined tool call directly.

- **Schema cache fail-fast.** When the project schema fetch errored, `_build_custom_fields_payload` previously swallowed it and proceeded with an empty schema, meaning custom field values were sent unvalidated and YouTrack would reject the create with an opaque 400. The exception now propagates with full context.

- **Defensive `isinstance` checks** on responses in `_get_project_id_from_issue`, `_fetch_project_schema`, `search_issues`, and `list_projects`. Malformed YouTrack responses now raise `YouTrackError` with a clear "Unexpected response shape" message instead of crashing with `AttributeError`.

- **`close_issue` raises clearly when the project has no State field** instead of falling back to sending `State Fixed` and getting an opaque YouTrack error.

- **`_normalize` and `_match_value`** now guard against empty / whitespace input that would previously match fields whose names normalize to empty.

### Added

- **Network and JSON error handling** in `_request`. `URLError` (DNS / TCP failure) and `JSONDecodeError` are now wrapped in `YouTrackError` instead of leaking as internal errors.

- **`tests/` directory** with 45 unit tests covering normalization, fuzzy matching, state resolution, token resolution priority, custom-fields payload validation, and close-issue state quoting. Run with `uv run --extra dev pytest`.

- **GitHub Actions CI** (`.github/workflows/test.yml`) running pytest against Python 3.10, 3.11, and 3.12 on every push and pull request.

- **`dev` optional dependency** in `pyproject.toml` for pytest, installed via `uv run --extra dev`.

### Changed

- `search_issues` no longer requests `customFields` and `reporter` in the response; we were paying for the bandwidth but not using the data. The returned shape is unchanged.

## [0.1.1] - 2026-05-22

### Added

- **Secure token sources beyond the env var.** The server now reads the token from one of three sources, in priority order:
  - `YOUTRACK_TOKEN`: raw value (existing behavior).
  - `YOUTRACK_TOKEN_FILE`: path to a file containing only the token.
  - `YOUTRACK_TOKEN_CMD`: shell command whose stdout is the token (designed for macOS Keychain via `security`, Linux `secret-tool`, 1Password CLI, `pass`, etc.).

- **SETUP.md "Choose how to store the token"** section with three tiers and ready-to-copy commands.

- **README quick install uses `read -rs`** so the token never lands in shell history.

### Documentation

- Linked the official JetBrains [Manage Permanent Token](https://www.jetbrains.com/help/youtrack/cloud/Manage-Permanent-Token.html) guide.
- Added a "What permissions does the MCP need?" subsection mapping every MCP tool to the YouTrack permission it requires, so users can scope a token-bearing user account to the minimum needed.

## [0.1.0] - 2026-05-22

First public release.

### Added

- **MCP server** with 8 tools: `create_issue`, `add_comment`, `close_issue`, `create_and_close_issue`, `search_issues`, `list_projects`, `get_project_fields`, `find_youtrack_config`.
- **`youtrack-projects` CLI helper** to list project IDs (YouTrack's UI doesn't expose them). Reads env vars or falls back to credentials in `~/.claude.json`.
- **Companion Claude Code skill** (`skills/youtrack-workflow/SKILL.md`) that reads a per-project `.youtrack.yml` and orchestrates search, propose, comment, close. Supports a `notify-only` mode (`auto_confirm: true`).
- **Server-side validation** of custom field values against the live project schema, with case-insensitive and punctuation-normalized fuzzy matching.
- **Auto-resolution of close state** by querying the project's actual State field (Done > Fixed > Resolved > Completed > Verified > Closed) with synonym support.
- **In-process schema cache** with 10-minute TTL.
- **Per-project writing conventions** in `.youtrack.yml`: language, summary/description templates with variable interpolation, custom field defaults.

[0.4.0]: https://github.com/morganseznec/youtrack-mcp/releases/tag/v0.4.0
[0.3.0]: https://github.com/morganseznec/youtrack-mcp/releases/tag/v0.3.0
[0.2.0]: https://github.com/morganseznec/youtrack-mcp/releases/tag/v0.2.0
[0.1.1]: https://github.com/morganseznec/youtrack-mcp/releases/tag/v0.1.1
[0.1.0]: https://github.com/morganseznec/youtrack-mcp/releases/tag/v0.1.0
