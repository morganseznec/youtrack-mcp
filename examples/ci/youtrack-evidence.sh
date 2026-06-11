#!/usr/bin/env bash
#
# youtrack-evidence.sh: post CI evidence to the YouTrack issue referenced by the
# current branch / MR / commit. Provider-agnostic: auto-detects GitLab CI and
# GitHub Actions. Posts a factual comment, optionally uploads artifacts, and
# optionally applies a YouTrack command (e.g. a state transition or a tag).
#
# Best-effort by default: a YouTrack hiccup will NOT fail your pipeline. Set
# YT_STRICT=1 to make posting failures fatal.
#
# Required env:
#   YOUTRACK_URL     e.g. https://acme.youtrack.cloud
#   YOUTRACK_TOKEN   permanent token with Update Issue (Add Comment) on the project
#
# Optional env:
#   YT_ISSUE          explicit issue id, e.g. IS-87 (skips auto-detection)
#   YT_ISSUE_PATTERN  regex to extract the id (default: [A-Z][A-Z0-9_]*-[0-9]+)
#   YT_STATUS         success|failed (default: CI_JOB_STATUS / job status / success)
#   YT_SUMMARY        one-line test summary, e.g. "111 passed in 0.31s"
#   YT_ARTIFACTS      space- or comma-separated file paths to attach
#   YT_COMMAND        a YouTrack command to apply, e.g. "add tag ci-green"
#   YT_FORCE          1 = post even if this pipeline already left evidence
#   YT_STRICT         1 = exit non-zero if posting fails
#
set -u

warn() { printf '[youtrack-evidence] %s\n' "$*" >&2; }

bail() {
  warn "$1"
  [ "${YT_STRICT:-0}" = "1" ] && exit 1
  exit 0
}

[ -n "${YOUTRACK_URL:-}" ]   || bail "YOUTRACK_URL not set"
[ -n "${YOUTRACK_TOKEN:-}" ] || bail "YOUTRACK_TOKEN not set"
command -v curl >/dev/null || bail "curl not found"
command -v jq   >/dev/null || bail "jq not found (e.g. apt-get install -y jq / apk add jq)"

base="${YOUTRACK_URL%/}"
auth="Authorization: Bearer ${YOUTRACK_TOKEN}"
pattern="${YT_ISSUE_PATTERN:-[A-Z][A-Z0-9_]*-[0-9]+}"

# ── Detect provider and gather context ───────────────────────────────────────
provider="local"; pipeline_url=""; pipeline_id=""; mr_url=""; branch=""; sources=""
if [ -n "${GITLAB_CI:-}" ]; then
  provider="GitLab CI"
  pipeline_url="${CI_PIPELINE_URL:-}"
  pipeline_id="${CI_PIPELINE_ID:-}"
  [ -n "${CI_MERGE_REQUEST_IID:-}" ] && mr_url="${CI_PROJECT_URL:-}/-/merge_requests/${CI_MERGE_REQUEST_IID}"
  branch="${CI_MERGE_REQUEST_SOURCE_BRANCH_NAME:-${CI_COMMIT_REF_NAME:-}}"
  sources="${CI_MERGE_REQUEST_SOURCE_BRANCH_NAME:-} ${CI_COMMIT_REF_NAME:-} ${CI_MERGE_REQUEST_TITLE:-} ${CI_COMMIT_MESSAGE:-}"
elif [ -n "${GITHUB_ACTIONS:-}" ]; then
  provider="GitHub Actions"
  pipeline_url="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-}"
  pipeline_id="${GITHUB_RUN_ID:-}"
  branch="${GITHUB_HEAD_REF:-${GITHUB_REF_NAME:-}}"
  sources="${GITHUB_HEAD_REF:-} ${GITHUB_REF_NAME:-} $(git log -1 --pretty=%B 2>/dev/null || true)"
else
  branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  sources="${branch} $(git log -1 --pretty=%B 2>/dev/null || true)"
fi

commit="$(git rev-parse --short HEAD 2>/dev/null || printf '%s' "${CI_COMMIT_SHORT_SHA:-${GITHUB_SHA:-}}")"

# ── Resolve the issue id ─────────────────────────────────────────────────────
issue="${YT_ISSUE:-}"
if [ -z "$issue" ]; then
  issue="$(printf '%s' "$sources" | grep -oE "$pattern" | head -n1 || true)"
fi
[ -n "$issue" ] || bail "no YouTrack issue id found in branch/MR/commit (set YT_ISSUE to override)"

# ── Normalize status ─────────────────────────────────────────────────────────
status="${YT_STATUS:-${CI_JOB_STATUS:-success}}"
case "$status" in
  success|passed|0) icon="✅"; label="passed" ;;
  *)                icon="❌"; label="$status" ;;
esac

# ── Idempotency: skip if this pipeline already left evidence on the issue ─────
marker="pipeline ${pipeline_id:-$commit}"
if [ "${YT_FORCE:-0}" != "1" ] && [ -n "$pipeline_id" ]; then
  existing="$(curl -sS -H "$auth" "$base/api/issues/$issue/comments?fields=text&\$top=100" 2>/dev/null \
              | jq -r '.[]?.text // empty' 2>/dev/null || true)"
  if printf '%s' "$existing" | grep -qF "$marker"; then
    warn "evidence for '$marker' already on $issue; skipping (set YT_FORCE=1 to override)"
    exit 0
  fi
fi

# ── Build the comment body ───────────────────────────────────────────────────
ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
nl=$'\n'
body="🤖 CI evidence: ${provider} ${icon} ${label}"
body="${body}${nl}- Commit : \`${commit:-?}\`"
[ -n "$branch" ]       && body="${body} · branch \`${branch}\`"
[ -n "${YT_SUMMARY:-}" ] && body="${body}${nl}- Tests : ${YT_SUMMARY}"
[ -n "$mr_url" ]       && body="${body}${nl}- MR : ${mr_url}"
[ -n "$pipeline_url" ] && body="${body}${nl}- Pipeline : ${pipeline_url} (${marker})"
body="${body}${nl}- ${ts}"

payload="$(jq -n --arg t "$body" '{text: $t}')"

# ── Post the comment ─────────────────────────────────────────────────────────
code="$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "$auth" \
        -H 'Content-Type: application/json' --data "$payload" \
        "$base/api/issues/$issue/comments?fields=id" 2>/dev/null)"
code="${code:-000}"
case "$code" in
  2*) warn "posted evidence to $issue ($label)" ;;
  *)  bail "comment POST to $issue returned HTTP $code" ;;
esac

# ── Optionally attach artifacts ──────────────────────────────────────────────
if [ -n "${YT_ARTIFACTS:-}" ]; then
  for f in $(printf '%s' "$YT_ARTIFACTS" | tr ',' ' '); do
    [ -f "$f" ] || { warn "artifact not found, skipping: $f"; continue; }
    acode="$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "$auth" \
             -F "upload=@${f}" "$base/api/issues/$issue/attachments?fields=id,name" 2>/dev/null)"
    acode="${acode:-000}"
    case "$acode" in
      2*) warn "attached $f" ;;
      *)  warn "attach failed for $f (HTTP $acode)" ;;
    esac
  done
fi

# ── Optionally apply a YouTrack command (state, tag, ...) ─────────────────────
if [ -n "${YT_COMMAND:-}" ]; then
  cmd="$(jq -n --arg q "$YT_COMMAND" --arg i "$issue" '{query: $q, issues: [{idReadable: $i}]}')"
  ccode="$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "$auth" \
           -H 'Content-Type: application/json' --data "$cmd" \
           "$base/api/commands" 2>/dev/null)"
  ccode="${ccode:-000}"
  case "$ccode" in
    2*) warn "applied command on $issue: $YT_COMMAND" ;;
    *)  warn "command failed on $issue (HTTP $ccode): $YT_COMMAND" ;;
  esac
fi

exit 0
