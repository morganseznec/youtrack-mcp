"""Canonical output shapes for every MCP tool (spec §0.2).

Each tool annotates its return type with one of these TypedDicts. MCPServer derives
a rich JSON `outputSchema` from the annotation and emits the returned dict as the
tool's `structuredContent`, which a programmatic client reads and validates.

Every field is optional (`total=False`) AND nullable (`| None`), for two reasons:
  1. MCPServer null-fills any field the tool omits before dumping structuredContent,
     so an omitted field must be allowed to be null by the schema.
  2. The normalized error envelope {"error": {...}} omits every success field, so
     on failure they are all null; the error object itself must validate too.
This makes success payloads AND error envelopes both validate against the one
declared schema (spec §0.3, §6.7).

The field names and types ARE the contract; see server.py docstrings for the
semantics.
"""

from __future__ import annotations

from typing import Any

from typing_extensions import TypedDict


class ErrorObj(TypedDict, total=False):
    code: str | None  # NOT_FOUND|PERMISSION_DENIED|VALIDATION_FAILED|RATE_LIMITED|YOUTRACK_UNAVAILABLE
    message: str | None
    youtrack_status: int | None
    retryable: bool | None


class UserRef(TypedDict, total=False):
    login: str | None
    name: str | None


class ProjectRef(TypedDict, total=False):
    id: str | None
    short_name: str | None


class AttachmentMeta(TypedDict, total=False):
    id: str | None
    name: str | None
    mime_type: str | None
    size_bytes: int | None
    created: str | None
    author: UserRef | None
    comment_id: str | None


class CommentObj(TypedDict, total=False):
    id: str | None
    author: UserRef | None
    created: str | None
    updated: str | None
    text: str | None
    truncated: bool | None
    attachments: list[str] | None
    deleted: bool | None


# ─── get_issue (§1) ───────────────────────────────────────────────────────────

class IssueDetail(TypedDict, total=False):
    id: str | None
    id_readable: str | None
    url: str | None
    project: ProjectRef | None
    summary: str | None
    description: str | None
    truncated: bool | None
    reporter: UserRef | None
    created: str | None
    updated: str | None
    resolved: str | None
    tags: list[str] | None
    custom_fields: dict[str, Any] | None
    comments: list[CommentObj] | None
    attachments: list[AttachmentMeta] | None
    comments_count: int | None
    is_resolved: bool | None
    links: list[dict[str, Any]] | None
    error: ErrorObj | None


# ─── update_issue (§2) ────────────────────────────────────────────────────────

class AppliedObj(TypedDict, total=False):
    custom_fields: list[str] | None
    tags_added: list[str] | None
    tags_removed: list[str] | None
    warnings: list[str] | None


class IssueMutation(TypedDict, total=False):
    id: str | None
    id_readable: str | None
    url: str | None
    project: ProjectRef | None
    summary: str | None
    description: str | None
    truncated: bool | None
    reporter: UserRef | None
    created: str | None
    updated: str | None
    resolved: str | None
    tags: list[str] | None
    custom_fields: dict[str, Any] | None
    comments_count: int | None
    is_resolved: bool | None
    links: list[dict[str, Any]] | None
    applied: AppliedObj | None
    error: ErrorObj | None


# ─── download_attachment (§3) ─────────────────────────────────────────────────

class DownloadResult(TypedDict, total=False):
    issue_id: str | None
    attachment_id: str | None
    name: str | None
    mime_type: str | None
    size_bytes: int | None
    path: str | None
    sha256: str | None
    text_preview: str | None
    error: ErrorObj | None


# ─── search_issues (§4.1) ─────────────────────────────────────────────────────

class SearchItem(TypedDict, total=False):
    id: str | None
    id_readable: str | None
    summary: str | None
    state: str | None
    priority: str | None
    tags: list[str] | None
    updated: str | None
    url: str | None
    description: str | None
    assignee: str | None
    reporter: str | None


class SearchResults(TypedDict, total=False):
    results: list[SearchItem] | None
    total: int | None
    error: ErrorObj | None


# ─── create_issue / create_and_close_issue (§4.2) ─────────────────────────────

class CreateResult(TypedDict, total=False):
    id: str | None
    id_readable: str | None
    summary: str | None
    url: str | None
    tags: list[str] | None
    custom_fields: dict[str, Any] | None
    idempotent_hit: bool | None
    state: str | None
    closed: bool | None
    error: ErrorObj | None


# ─── add_comment (§4.3) ───────────────────────────────────────────────────────

class CommentResult(TypedDict, total=False):
    ok: bool | None
    issue_id: str | None
    comment_id: str | None
    created: str | None
    attachments: list[dict[str, Any]] | None
    error: ErrorObj | None


# ─── find_youtrack_config (§4.4) ──────────────────────────────────────────────

class ConfigResult(TypedDict, total=False):
    found: bool | None
    path: str | None
    project_root: str | None
    config: dict[str, Any] | None
    parse_error: str | None  # soft: a found-but-unparseable file, not a tool failure
    error: ErrorObj | None


# ─── simple single-object mutations / reads ───────────────────────────────────

class OkResult(TypedDict, total=False):
    """Generic ack for close_issue / link_issues / manage_issue_tags / etc.

    A union of the optional keys the various ack-style tools add; each tool sets
    only the ones relevant to it (the rest null-fill).
    """
    ok: bool | None
    issue_id: str | None
    error: ErrorObj | None
    state: str | None
    project_id: str | None
    requested_state: str | None
    assignee: str | None
    target_issue_id: str | None
    link_type: str | None
    command: str | None
    added: list[str] | None
    removed: list[str] | None
    not_found: list[str] | None
    id: str | None
    name: str | None
    size: int | None
    mime_type: str | None
    url: str | None
    minutes: int | None
    presentation: str | None
    text: str | None
    date: str | None
    work_type: str | None
    draft: bool | None
    summary: str | None


class FieldSchema(TypedDict, total=False):
    name: str | None
    type: str | None
    can_be_empty: bool | None
    empty_text: str | None
    values: list[str] | None


class ProjectFields(TypedDict, total=False):
    project_id: str | None
    fields: list[FieldSchema] | None
    error: ErrorObj | None


class ProjectDetail(TypedDict, total=False):
    id: str | None
    name: str | None
    short_name: str | None
    description: str | None
    leader: str | None
    archived: bool | None
    created_by: str | None
    error: ErrorObj | None


class UserDetail(TypedDict, total=False):
    id: str | None
    login: str | None
    full_name: str | None
    email: str | None
    banned: bool | None
    online: bool | None
    error: ErrorObj | None


class ArticleDetail(TypedDict, total=False):
    id: str | None
    summary: str | None
    content: str | None
    project: str | None
    reporter: str | None
    created: str | None
    updated: str | None
    parent_article: str | None
    child_articles: list[dict[str, Any]] | None
    url: str | None
    error: ErrorObj | None


# ─── list-returning tools → object envelopes (rich everywhere) ────────────────

class ProjectItem(TypedDict, total=False):
    id: str | None
    name: str | None
    short_name: str | None


class UserItem(TypedDict, total=False):
    id: str | None
    login: str | None
    full_name: str | None
    email: str | None


class GroupItem(TypedDict, total=False):
    id: str | None
    name: str | None
    users_count: int | None


class SavedSearchItem(TypedDict, total=False):
    id: str | None
    name: str | None
    query: str | None
    owner: str | None


class ArticleItem(TypedDict, total=False):
    id: str | None
    summary: str | None
    project: str | None


class ProjectList(TypedDict, total=False):
    items: list[ProjectItem] | None
    count: int | None
    error: ErrorObj | None


class UserList(TypedDict, total=False):
    items: list[UserItem] | None
    count: int | None
    error: ErrorObj | None


class GroupList(TypedDict, total=False):
    items: list[GroupItem] | None
    count: int | None
    error: ErrorObj | None


class SavedSearchList(TypedDict, total=False):
    items: list[SavedSearchItem] | None
    count: int | None
    error: ErrorObj | None


class ArticleList(TypedDict, total=False):
    items: list[ArticleItem] | None
    count: int | None
    error: ErrorObj | None


class CommentList(TypedDict, total=False):
    items: list[CommentObj] | None
    count: int | None
    error: ErrorObj | None
