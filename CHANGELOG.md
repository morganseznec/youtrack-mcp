# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.2.0]: https://github.com/morganseznec/youtrack-mcp/releases/tag/v0.2.0
[0.1.1]: https://github.com/morganseznec/youtrack-mcp/releases/tag/v0.1.1
[0.1.0]: https://github.com/morganseznec/youtrack-mcp/releases/tag/v0.1.0
