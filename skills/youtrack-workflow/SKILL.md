---
name: youtrack-workflow
description: Orchestrate YouTrack ticket workflow in projects that have a .youtrack.yml file. Trigger when the user is describing development work (a bug to fix, a feature to implement, a refactor, a TODO, or work that just completed) AND the mcp__youtrack__* tools are available. The skill checks for .youtrack.yml in the working directory or any ancestor, then orchestrates: searching YouTrack for matching existing issues, then either proposing actions and waiting for confirmation, or (when auto_confirm is true) acting silently with one-line URL notifications. Applies project conventions (language, summary prefix, custom fields). Do NOT trigger for pure Q&A, code explanations, or exploratory discussions without a clear actionable task.
---

# YouTrack Workflow

Bridges development work happening in a Claude Code session with YouTrack ticket tracking. Only active in projects that have explicitly opted in by adding a `.youtrack.yml` file.

## When to act

Trigger when ALL of these hold:

- The `mcp__youtrack__*` tools are loaded in the session.
- The user's message describes an actionable piece of work: a bug, feature, refactor, fix, or completed task. Examples in French/English: "il faut corriger", "j'ai un bug", "ajoutons", "on doit implémenter", "I need to fix", "let's add", or a `// TODO` comment they're pointing to.
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

If `found: false`, the skill is a no-op. Don't propose anything, don't mention YouTrack. The user opts in by dropping a `.youtrack.yml`; absence means they don't want auto-tracking here.

If `found: true` but `error` is set, surface the error briefly so the user can fix it, then no-op.

### 2. Project schema (handled by the server)

The MCP server caches the project schema for ~10 minutes and validates `custom_fields` values automatically:

- Unknown field name: server raises with the list of available fields.
- Enum/state value that doesn't match (case-insensitive and fuzzy normalization): server raises with the list of allowed values.

You don't need to fetch `get_project_fields` defensively. Only call it when:
- The user asks "what fields are available on project X?"
- You want to suggest valid values before drafting (e.g. let the user pick a Priority from the actual list)
- A validation error came back and you want to show the user what's allowed

### 3. Decide whether to propose, or just act (auto_confirm mode)

Read these fields from the parsed config:

- `auto_propose: false`: don't volunteer at all; only act if the user explicitly asks ("create a YouTrack ticket for this").
- `auto_propose: true`: continue.
- `auto_confirm: true`: **notify-only mode**. Skip the proposal step entirely. Decide, act, notify. See "Notify-only mode" below.
- `auto_confirm: false` (default): propose, wait for confirmation, then act.

For an inbound task description (work *about to be done* or *just done*):

- If `auto_search: true`: first call `mcp__youtrack__search_issues` with a query derived from the task. Build the query like `project: <project_id> summary: <keywords>` using 2-5 meaningful keywords from the user's request. Pull at most 5 hits.
- If hits look like a match (similar summary, unresolved): in confirm mode, propose linking ("This matches `PROJ-42`. Use it?"); in auto_confirm mode, use it directly.
- Otherwise: propose creation (confirm mode) or create directly (auto_confirm mode).
- If `auto_search: false`: skip search.

**Confirm mode** (`auto_confirm: false`): Make the proposal short, one or two lines, with the **rendered** `summary` (template applied, see step 4) and the project ID being targeted. Wait for confirmation. The user staying silent or saying "vas-y" / "fais-le" is confirmation.

**Notify-only mode** (`auto_confirm: true`): Don't ask. Apply the templates, build the issue, call the MCP, then emit a single-line notification:

- On create: `📝 Created PROJ-87. <url>`
- On comment: `💬 Commented on PROJ-87`
- On close: `✓ Closed PROJ-87 (Done). <url>`
- On create-and-close: `📝 ✓ Created and closed PROJ-87 (Done). <url>`

The notifications go in the assistant's normal text output, one line each, inline with the rest of the response. No separate confirmation, no follow-up question. If a validation error comes back from the server (invalid custom field value), surface it clearly so the user can correct the config. Don't loop attempting fixes silently.

### 4. Apply writing conventions before creating

Three things must be honored in the order below:

**a) Language**

`config.language` is `"fr"` or `"en"` (defaults to `"en"`). Write the entire ticket (summary, description, comments, closing recap) in that language. Don't switch mid-ticket. The user can mix languages in their prompts; you still produce the ticket in the configured language.

**b) Summary template**

If `config.summary_template` is set, render it with placeholders:

- `{summary}`: the title you drafted from the user's request.
- Any key from `config.variables` (e.g. `{country}`, `{env}`).

Example: template `"[{country}][{env}] {summary}"` with `variables: {country: CI, env: PROD}` and a drafted summary `"Fix timeout on /orders"` becomes `"[CI][PROD] Fix timeout on /orders"`.

If a placeholder has no matching variable, leave it as literal text and warn the user once.

**c) Description template**

Same rule applies to `config.description_template`. Additional placeholder available: `{description}` is the body you drafted.

If no `description_template` is set, send the raw drafted description.

### 5. Apply custom fields and tags

Build the `custom_fields` dict for the API call by merging `config.custom_fields` with any user-specified overrides in the current request (the user's request wins). Same for `tags`: start from `config.default_tags`.

Pass the merged dict straight to `create_issue`. The server validates each value against the project's enums (with case-insensitive and punctuation-normalized matching, so `"critical"` matches `"Critical"` and `"showstopper"` matches `"Show-stopper"`). If a value is genuinely invalid, the server raises with the list of allowed values; relay that to the user and ask them to pick one.

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

- Call `mcp__youtrack__add_comment` with a recap of what was done, in the configured language, terse 3-6 bullets.
- Then `mcp__youtrack__close_issue(issue_id, comment="...")` **without specifying `state`**. The server detects the project automatically and picks the canonical "done" state (Done / Fixed / Resolved, whichever exists). Only pass `state` explicitly when the user signals a different intent: `"wontfix"`, `"duplicate"`, `"rejected"`. Synonyms are translated to the project's actual state names.

## Evidence trail (automatic proof comments)

Beyond create/comment/close, the skill can maintain an **audit trail** on the tracked ticket: short, factual comments that record what was done and what the result was. This is valuable for change-management and ISO/SOC2-style review, where a ticket should carry proof that a change was tested before it shipped.

Only active when `config.evidence.enabled` is true.

### What counts as a checkpoint

Post evidence at meaningful milestones, listed in `config.evidence.on` (default: `tests`, `build`, `commit`, `deploy`). Do NOT post on every command:

- `tests`: a test suite ran for the tracked issue (e.g. `pytest`, `npm test`, `go test`).
- `build`: a build / lint / typecheck completed.
- `commit`: a commit referencing the issue was pushed.
- `deploy`: a deploy, migration, or release was applied.

Skip exploratory or unrelated runs, and runs you cannot tie to a tracked issue.

### Which issue does the evidence attach to

Resolve in this order:

1. The issue already tracked in this session (held from steps 6-7).
2. Otherwise extract `<SHORT>-<number>` (e.g. `IS-87`) from, in order: the current branch (`git branch --show-current`), recent commit subjects/bodies (`git log -n 20 --format=%s%n%b`), or the MR/PR title. Constrain the prefix with `config.project_short` when set.

If no issue resolves, do nothing. Never guess a ticket.

### Integrity rules (non-negotiable for audit)

- **Never fabricate a result.** Report only what actually ran, with the real exit status and the tool's own summary. If a run failed, say it failed. If you did not run it, post nothing.
- **State the provenance** explicitly: evidence from the session is labeled *"session locale Claude Code"*, never dressed up as a CI/system fact.
- **Anchor to immutable references**: commit SHA (`git rev-parse --short HEAD`), branch name, the exact command, and a UTC timestamp. Auditable evidence points at things that can be re-checked.
- Prefer pasting the tool's actual summary line (e.g. `111 passed in 0.31s`) over paraphrasing it.

### Idempotency

Before posting, call `get_issue_comments(issue_id)` and skip if an evidence comment for the **same commit SHA and same checkpoint** already exists. One evidence comment per (commit, checkpoint) keeps the trail clean.

### Format

A compact structured Markdown comment, written in `config.language`. Pass/fail examples:

```
🤖 Preuve : tests (session locale Claude Code)
- Commit : `abc1234` · branche `feature/IS-87-redact`
- Commande : `uv run pytest -q`
- Résultat : ✅ 111 passed, 0 failed (0.31s)
- 2026-06-11 14:40 UTC
```
```
🤖 Preuve : tests (session locale Claude Code)
- Commit : `abc1234`
- Commande : `uv run pytest -q`
- Résultat : ❌ 2 failed, 109 passed → `test_redact`, `test_close`
- 2026-06-11 14:40 UTC
```

### Attach artifacts (audit-grade)

When `config.evidence.attach_artifacts` is true and a machine-readable artifact exists (`junit.xml`, `coverage.xml`, a captured log, a screenshot), attach it and reference its name in the comment:

```
attach_file(issue_id="IS-87", file_path="/abs/path/junit.xml")
```

An attached report is far stronger proof than agent prose, because it is a verifiable artifact rather than a claim. Keep artifacts small and relevant.

### Linking GitHub / GitLab (in-session)

When the work lives in a GitHub PR or a GitLab MR, enrich the evidence with the real VCS/CI state and make the link two-way. **Claude is the bridge**: it reads from GitHub/GitLab and writes to YouTrack via this MCP. The YouTrack MCP itself never talks to GitHub/GitLab.

Pick the channel in this order (use the first available):

1. **The `gh` / `glab` CLI** via Bash, when authenticated. This is the default: both CLIs talk to the standard API and work on **every tier, including GitLab Free**. Detect the provider from `git remote get-url origin` (github.com → `gh`; a GitLab host → `glab`).
2. **A connected GitHub/GitLab MCP** (tools like `mcp__github__*` / `mcp__gitlab__*`), if present, for the same data. Caveat: GitLab's **official** MCP is gated to Premium/Ultimate (Beta), so `glab` is the universal path on Free; community/open-source GitLab MCP servers work on any tier with a PAT. GitHub's official MCP is free.
3. Neither available: skip VCS enrichment, still post the local evidence.

**GitHub (`gh`)** for the current branch:

```bash
gh pr view --json number,url,state,title,headRefName     # the PR
gh pr checks                                              # CI check states + links
gh run list --branch "$(git branch --show-current)" -L 1 \
  --json databaseId,workflowName,conclusion,url           # latest run
gh run download <run-id> -D evidence                      # pull artifacts (coverage, junit, screenshots)
gh pr comment <number> --body "Tracked in YouTrack: <issue-url>"   # link back (once)
```

**GitLab (`glab`)** for the current branch:

```bash
glab mr view                                  # the MR
glab ci status                                # pipeline status for the branch
glab ci artifact "<branch>" "<job-name>"      # pull a job's artifacts from the last pipeline
glab mr note -m "Tracked in YouTrack: <issue-url>"   # link back (once)
```

Then on the YouTrack side:

- Post a comment that includes the PR/MR URL, the pipeline/run URL, and the real pass/fail (label it as coming from the CI, which is stronger than a local run).
- `attach_file(issue_id, "evidence/<file>")` for each downloaded artifact: test report, coverage, log, or a screenshot.
- Add the YouTrack issue URL back onto the PR/MR exactly once (skip if already present), so the trace works both directions.

This is the in-session path. When no Claude session is running, the MR pipeline itself posts the same kind of evidence: see `examples/ci/` in the MCP server repo.

### Respecting the mode

- **Notify-only** (`auto_confirm: true`): post the evidence and emit one line, e.g. `📎 Evidence on IS-87: ✅ tests 111/111 (commit abc1234)` or, with an artifact, `📎 Evidence on IS-87: ✅ tests + junit.xml attached`.
- **Confirm mode** (`auto_confirm: false`): fold evidence into the natural flow (e.g. when you would already comment or close). Evidence is low-friction; do not add extra prompts or nag for it.

For the **CI pipeline** side (evidence posted by the MR pipeline itself, without a Claude session), see the snippets and the YouTrack-native commit-command guide under `examples/ci/` in the MCP server repo. That path is stronger for audit because the evidence comes from the system of record, not the agent.

## Etiquette

- One proposal per task. If the user says no, drop it. Don't re-propose for the same task.
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
auto_confirm: false         # true: notify-only mode (act without asking)

language: "en"              # "en" or "fr". Claude writes tickets in this language.
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

evidence:                   # automatic proof comments on the tracked ticket
  enabled: false            # opt-in
  on: [tests, build, commit, deploy]
  attach_artifacts: true    # also upload junit.xml / coverage / logs via attach_file
```

A working example lives at the `youtrack.example.yml` in the MCP server's repo.
