---
name: youtrack-workflow
description: Orchestrate YouTrack ticket workflow in projects that have a .youtrack.yml file. Trigger when the user is describing development work (a bug to fix, a feature to implement, a refactor, a TODO, or work that just completed) AND the mcp__youtrack__* tools are available. The skill checks for .youtrack.yml in the working directory or any ancestor, then orchestrates: searching YouTrack for matching existing issues, then either proposing actions and waiting for confirmation, or (when auto_confirm is true) acting silently with one-line URL notifications. Applies project conventions (language, summary prefix, custom fields). Do NOT trigger for pure Q&A, code explanations, or exploratory discussions without a clear actionable task.
---

# YouTrack Workflow

Bridges development work happening in a Claude Code session with YouTrack ticket tracking. Only active in projects that have explicitly opted in by adding a `.youtrack.yml` file.

## When to act

Trigger when ALL of these hold:

- The `mcp__youtrack__*` tools are loaded in the session.
- The user's message describes an actionable piece of work — a bug, feature, refactor, fix, or completed task. Examples in French/English: "il faut corriger", "j'ai un bug", "ajoutons", "on doit implémenter", "I need to fix", "let's add", or a `// TODO` comment they're pointing to.
- The current working directory (or one of its ancestors) contains a `.youtrack.yml`.

Skip if:
- The user is asking a question, exploring code, or having a generic conversation.
- The current path is listed in `ignore_paths` of the config.
- The user has already declined a ticket proposal earlier in the session (don't re-ask).

## Workflow

### 1. Detect the config

At the start of a task-shaped exchange, call:

```
mcp__youtrack__find_youtrack_config(start_path=<absolute cwd>)
```

If `found: false`, the skill is a no-op — don't propose anything, don't mention YouTrack. The user opts in by dropping a `.youtrack.yml`; absence means they don't want auto-tracking here.

If `found: true` but `error` is set, surface the error briefly so the user can fix it, then no-op.

### 2. Project schema (handled by the server)

The MCP server caches the project schema for ~10 minutes and validates `custom_fields` values automatically:

- Unknown field name → server raises with the list of available fields.
- Enum/state value that doesn't match (case-insensitive + fuzzy normalization) → server raises with the list of allowed values.

You don't need to fetch `get_project_fields` defensively. Only call it when:
- The user asks "what fields are available on project X?"
- You want to suggest valid values before drafting (e.g. let the user pick a Priority from the actual list)
- A validation error came back and you want to show the user what's allowed

### 3. Decide whether to propose — or just act (auto_confirm mode)

Read these fields from the parsed config:

- `auto_propose: false` → don't volunteer at all; only act if the user explicitly asks ("create a YouTrack ticket for this").
- `auto_propose: true` → continue.
- `auto_confirm: true` → **notify-only mode**: skip the proposal step entirely. Decide, act, notify. See "Notify-only mode" below.
- `auto_confirm: false` (default) → propose, wait for confirmation, then act.

For an inbound task description (work *about to be done* or *just done*):

- If `auto_search: true`: first call `mcp__youtrack__search_issues` with a query derived from the task. Build the query like `project: <project_id> summary: <keywords>` using 2-5 meaningful keywords from the user's request. Pull at most 5 hits.
- If hits look like a match (similar summary, unresolved): in confirm mode, propose linking ("This matches `PROJ-42` — use it?"); in auto_confirm mode, use it directly.
- Otherwise: propose creation (confirm mode) or create directly (auto_confirm mode).
- If `auto_search: false`: skip search.

**Confirm mode** (`auto_confirm: false`): Make the proposal short — one or two lines, with the **rendered** `summary` (template applied — see step 4) and the project ID being targeted. Wait for confirmation. The user staying silent or saying "vas-y" / "fais-le" is confirmation.

**Notify-only mode** (`auto_confirm: true`): Don't ask. Apply the templates, build the issue, call the MCP, then emit a single-line notification:

- On create: `📝 Created PROJ-87 — <url>`
- On comment: `💬 Commented on PROJ-87`
- On close: `✓ Closed PROJ-87 (Done) — <url>`
- On create-and-close: `📝 ✓ Created and closed PROJ-87 (Done) — <url>`

The notifications go in the assistant's normal text output, one line each, inline with the rest of the response. No separate confirmation, no follow-up question. If a validation error comes back from the server (invalid custom field value), surface it clearly so the user can correct the config — don't loop attempting fixes silently.

### 4. Apply writing conventions before creating

Three things must be honored in the order below:

**a) Language**

`config.language` is `"fr"` or `"en"` (defaults to `"en"`). Write the entire ticket — summary, description, comments, closing recap — in that language. Don't switch mid-ticket. The user can mix languages in their prompts; you still produce the ticket in the configured language.

**b) Summary template**

If `config.summary_template` is set, render it with placeholders:

- `{summary}` — the title you drafted from the user's request
- Any key from `config.variables` (e.g. `{country}`, `{env}`)

Example: template `"[{country}][{env}] {summary}"` with `variables: {country: CI, env: PROD}` and a drafted summary `"Fix timeout on /orders"` becomes `"[CI][PROD] Fix timeout on /orders"`.

If a placeholder has no matching variable, leave it as literal text and warn the user once.

**c) Description template**

Same rule applies to `config.description_template`. Additional placeholder available: `{description}` — the body you drafted.

If no `description_template` is set, send the raw drafted description.

### 5. Apply custom fields and tags

Build the `custom_fields` dict for the API call by merging `config.custom_fields` with any user-specified overrides in the current request (the user's request wins). Same for `tags` — start from `config.default_tags`.

Pass the merged dict straight to `create_issue` — the server validates each value against the project's enums (with case-insensitive + punctuation-normalized matching, so `"critical"` matches `"Critical"` and `"showstopper"` matches `"Show-stopper"`). If a value is genuinely invalid, the server raises with the list of allowed values; relay that to the user and ask them to pick one.

### 6. Create

Call:

```
mcp__youtrack__create_issue(
  summary=<rendered summary>,
  description=<rendered description>,
  project_id=<config.project_id>,
  custom_fields=<merged dict>,
  tags=<merged list>,
)
```

For a "create and close" one-shot use `create_and_close_issue` with the same args plus `closing_comment` and `state`.

### 7. Track through completion

Hold onto the issue ID for the rest of the conversation. When the work is verifiably done (tests pass, change pushed, user says "c'est bon" / "ferme-le" / "terminé"):

- Call `mcp__youtrack__add_comment` with a recap of what was done — in the configured language, terse 3-6 bullets.
- Then `mcp__youtrack__close_issue(issue_id, comment="...")` **without specifying `state`**. The server detects the project automatically and picks the canonical "done" state (Done / Fixed / Resolved, whichever exists). Only pass `state` explicitly when the user signals a different intent: `"wontfix"`, `"duplicate"`, `"rejected"` — synonyms are translated to the project's actual state names.

## Etiquette

- One proposal per task. If the user says no, drop it — don't re-propose for the same task.
- Don't make YouTrack noise during pure exploration. Wait for the user to articulate a concrete task or decision.
- When closing a ticket, *only* do it if the work is actually done. Failing tests, partial implementation, or unresolved blockers = no close, regardless of the user's optimism. Add a comment with status instead.
- If the user explicitly references a ticket ID (e.g. `PROJ-42`), use that directly without searching.

## Config reference

The full `.youtrack.yml` schema (parsed by `find_youtrack_config`):

```yaml
project_id: "0-1"           # required
project_short: "PROJ"       # optional, display only

auto_search: true
auto_propose: true
auto_confirm: false         # true → notify-only mode (act without asking)

language: "en"              # "en" or "fr" — Claude writes tickets in this language
summary_template: "[{env}] {summary}"
description_template: |
  ## Context
  {description}
variables:
  env: "prod"

custom_fields:              # discover with get_project_fields
  Priority: "Normal"
  Type: "Bug"

default_tags: []
default_assignee: null
ignore_paths: []
```

A working example lives at the `youtrack.example.yml` in the MCP server's repo.
