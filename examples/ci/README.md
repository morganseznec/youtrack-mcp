# YouTrack evidence from CI

Automatically attach **proof of what ran and what the result was** to the YouTrack
issue behind a branch / MR / PR. This is the unattended counterpart to the
in-session evidence that the [`youtrack-workflow` skill](../../skills/youtrack-workflow/SKILL.md)
posts while Claude Code is driving.

There are three complementary paths. Use whichever fit; they stack.

| Path | Who posts | Strength as audit evidence | Setup |
|---|---|---|---|
| In-session skill | Claude Code | Medium (agent + links) | Set `evidence.enabled: true` in `.youtrack.yml` |
| CI script (here) | The pipeline | Strong (system of record) | Vendor the script + 2 secrets |
| YouTrack-native commit commands | YouTrack VCS integration | Strong, zero custom code | Connect the repo in YouTrack |

For the in-session path, Claude pulls the PR/MR status and downloads CI artifacts (coverage, JUnit, screenshots) through a connected GitHub/GitLab MCP or the `gh` / `glab` CLI, then attaches them to the ticket and links the issue back onto the PR/MR. See the skill's "Linking GitHub / GitLab" section. The CI script below does not need `gh`/`glab`: inside the pipeline it already has the native `CI_*` / `GITHUB_*` environment.

## 1. CI script

[`youtrack-evidence.sh`](youtrack-evidence.sh) posts a factual comment to the
issue referenced by the current branch / MR / commit, and can also upload an
artifact (JUnit, coverage, a log) and apply a YouTrack command (transition,
tag). It auto-detects GitLab CI and GitHub Actions.

### Setup

1. Add two CI secrets (masked): `YOUTRACK_URL` and `YOUTRACK_TOKEN`. The token only
   needs *Update Issue (Add Comment)* on the target project (plus *Apply Command*
   if you use `YT_COMMAND`). Scope it to a bot user with minimal rights.
2. Vendor `youtrack-evidence.sh` in your repo (e.g. copy it to `ci/`), or `curl`
   it at job time.
3. Wire it into your pipeline. See [`gitlab-ci.example.yml`](gitlab-ci.example.yml)
   and [`github-actions.example.yml`](github-actions.example.yml).

### How the issue is found

In order: `YT_ISSUE` (explicit) → the MR/PR source branch → the MR/PR title →
the latest commit message. The match pattern is `[A-Z][A-Z0-9_]*-[0-9]+`
(e.g. `IS-87`), overridable with `YT_ISSUE_PATTERN`. If nothing matches, the
script does nothing (it never guesses a ticket).

### Environment variables

| Var | Required | Meaning |
|---|---|---|
| `YOUTRACK_URL` | yes | e.g. `https://acme.youtrack.cloud` |
| `YOUTRACK_TOKEN` | yes | permanent token (masked secret) |
| `YT_ISSUE` | no | explicit issue id; skips auto-detection |
| `YT_ISSUE_PATTERN` | no | regex for the id (default `[A-Z][A-Z0-9_]*-[0-9]+`) |
| `YT_STATUS` | no | `success` / `failed`; defaults to the CI job status |
| `YT_SUMMARY` | no | one-line test summary, e.g. `111 passed in 0.31s` |
| `YT_ARTIFACTS` | no | space/comma-separated files to attach |
| `YT_COMMAND` | no | a YouTrack command, e.g. `add tag ci-green` or `State In Review` |
| `YT_FORCE` | no | `1` to post even if this pipeline already left evidence |
| `YT_STRICT` | no | `1` to fail the job if posting fails (default: best-effort) |

The script is **best-effort**: by default a YouTrack outage logs a warning and
exits 0 so it never turns your build red. It is **idempotent** per pipeline: it
skips if a comment already references this pipeline id.

### Design note

Test parsing and CI wiring live here, in the pipeline, on purpose. The MCP server
stays a thin YouTrack API layer and is not coupled to any CI provider. The script
talks to the same REST endpoints the MCP uses (`/api/issues/{id}/comments`,
`/attachments`, `/commands`).

## 2. YouTrack-native commit commands (zero custom code)

YouTrack can read commands directly from commit messages once the repository is
connected (Project settings > VCS Integrations, or the GitLab/GitHub app). This
needs no script: the integration creates a comment on the issue for each linked
commit, and applies any command you write.

- **Link a commit to an issue:** put the id anywhere in the message.
  ```
  IS-87 fix token redaction in error bodies
  ```
- **Apply a command while linking:** prefix the command with `#`.
  ```
  IS-87 #In-Review redaction added, tests green
  IS-87 #Fixed #{ci-green}
  ```
  `#In-Review` sets the State; `#{ci-green}` adds a tag (braces for multi-word
  values). Anything after the command text becomes the comment.
- **Reference without acting:** just mention `IS-87` with no `#` command.

This is the strongest, lowest-maintenance trail for commit/MR linkage: the
evidence comes from YouTrack's own integration, not from an agent or a script.
Use the CI script above when you want richer evidence (attached reports, explicit
pass/fail summary, pipeline links) on top of it.

References: [YouTrack: Apply Commands in VCS Commits](https://www.jetbrains.com/help/youtrack/cloud/apply-commands-in-vcs-commits.html),
[Integration with GitLab/GitHub](https://www.jetbrains.com/help/youtrack/cloud/integration-with-version-control-systems.html).
