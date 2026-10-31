"""Acceptance tests for the v0.5 structured-output contract (spec §0, §4, §6).

Covers: normalized error classification (§0.3, §6.6), create_issue idempotency
(§0.4, §6.5), search_issues envelope + fields modes (§4.1), add_comment with
attachments (§4.3), and the core guarantee (§6.7) that every tool's
structuredContent validates against its DECLARED outputSchema, for both the
success and the error envelope.
"""

import asyncio

import jsonschema
import pytest

from youtrack_mcp import server

_TOOLS = {t.name: t for t in server.mcp._tool_manager.list_tools()}


def _run_tool(name, arguments):
    """Invoke a tool through MCPServer's conversion and return (structured, schema).

    Going through call_tool mirrors what the stdio server sends on the wire:
    the returned structuredContent is exactly what the orchestrator receives.
    """
    result = asyncio.run(server.mcp.call_tool(name, arguments))
    structured = result.structured_content
    assert structured is not None, f"{name} produced no structuredContent"
    return structured, _TOOLS[name].output_schema


# ─── §6.6 error classification ────────────────────────────────────────────────

def test_network_error_maps_to_youtrack_unavailable(monkeypatch):
    def boom(*a, **k):
        raise server.YouTrackNetworkError("connection refused")

    monkeypatch.setattr(server, "_request", boom)
    out = server.get_issue("PROJ-1")
    assert out["error"]["code"] == "YOUTRACK_UNAVAILABLE"
    assert out["error"]["retryable"] is True


def test_permission_denied_maps_from_403(monkeypatch):
    def boom(*a, **k):
        raise server.YouTrackHTTPError("HTTP 403 ...", status=403)

    monkeypatch.setattr(server, "_request", boom)
    out = server.get_issue("PROJ-1")
    assert out["error"]["code"] == "PERMISSION_DENIED"
    assert out["error"]["youtrack_status"] == 403
    assert out["error"]["retryable"] is False


def test_not_found_maps_from_404(monkeypatch):
    def boom(*a, **k):
        raise server.YouTrackHTTPError("HTTP 404 ...", status=404)

    monkeypatch.setattr(server, "_request", boom)
    out = server.get_issue("PROJ-999")
    assert out["error"]["code"] == "NOT_FOUND"


def test_rate_limited_maps_from_429_and_is_retryable(monkeypatch):
    def boom(*a, **k):
        raise server.YouTrackHTTPError("HTTP 429 ...", status=429)

    monkeypatch.setattr(server, "_request", boom)
    out = server.search_issues("project: IS")
    assert out["error"]["code"] == "RATE_LIMITED"
    assert out["error"]["retryable"] is True


def test_unexpected_python_error_never_leaks_raw(monkeypatch):
    def boom(*a, **k):
        raise KeyError("some internal bug")

    monkeypatch.setattr(server, "_request", boom)
    out = server.get_issue("PROJ-1")
    # A bare exception is wrapped, not raised, and is not presented as an outage
    # an orchestrator should retry: the same input hits the same bug.
    assert "error" in out and out["error"]["code"] == "INTERNAL_ERROR"
    assert out["error"]["retryable"] is False


def _urlopen_raising(exc):
    def _urlopen(req, timeout=None):
        raise exc
    return _urlopen


def test_post_timeout_after_send_is_not_retryable(monkeypatch):
    # A reply that never comes back: the comment may exist, so a retry could
    # post it twice.
    monkeypatch.setattr(server.urllib.request, "urlopen", _urlopen_raising(TimeoutError("timed out")))
    out = server.add_comment("PROJ-1", "hello")
    assert out["error"]["code"] == "YOUTRACK_UNAVAILABLE"
    assert out["error"]["retryable"] is False
    assert "may have been applied" in out["error"]["message"]


def test_get_timeout_after_send_is_retryable(monkeypatch):
    monkeypatch.setattr(server.urllib.request, "urlopen", _urlopen_raising(TimeoutError("timed out")))
    out = server.get_issue("PROJ-1")
    assert out["error"]["code"] == "YOUTRACK_UNAVAILABLE"
    assert out["error"]["retryable"] is True


def test_post_connect_failure_is_retryable(monkeypatch):
    # urlopen wraps connect/send failures in URLError: nothing reached YouTrack.
    monkeypatch.setattr(
        server.urllib.request, "urlopen",
        _urlopen_raising(server.urllib.error.URLError(ConnectionRefusedError("refused"))),
    )
    out = server.add_comment("PROJ-1", "hello")
    assert out["error"]["code"] == "YOUTRACK_UNAVAILABLE"
    assert out["error"]["retryable"] is True


def test_error_envelope_goes_out_with_is_error(monkeypatch):
    def boom(*a, **k):
        raise server.YouTrackHTTPError("HTTP 404 ...", status=404)

    monkeypatch.setattr(server, "_request", boom)
    result = asyncio.run(server.mcp.call_tool("get_issue", {"issue_id": "PROJ-999"}))
    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "NOT_FOUND"


def test_success_goes_out_without_is_error(monkeypatch):
    monkeypatch.setattr(server, "_request", lambda *a, **k: {
        "id": "2-1", "idReadable": "PROJ-1", "summary": "S",
        "customFields": [], "comments": [], "attachments": [], "tags": [],
    })
    result = asyncio.run(server.mcp.call_tool("get_issue", {"issue_id": "PROJ-1"}))
    assert result.is_error is False


def test_token_is_redacted_from_error_messages(monkeypatch):
    def boom(*a, **k):
        raise server.YouTrackHTTPError(f"HTTP 400 leaked {server.YOUTRACK_TOKEN}", status=400)

    monkeypatch.setattr(server, "_request", boom)
    out = server.get_issue("PROJ-1")
    assert server.YOUTRACK_TOKEN not in out["error"]["message"]
    assert "<redacted>" in out["error"]["message"]


# ─── §6.5 idempotency ─────────────────────────────────────────────────────────

class _StatefulRequest:
    """A _request stub whose GET /issues (the idem search) flips after the create."""

    def __init__(self):
        self.created = False
        self.calls = []

    def __call__(self, method, path, body=None, params=None):
        self.calls.append((method, path))
        if method == "GET" and path == "/issues":
            # Idempotency search: empty until we've created, then returns the issue.
            if self.created:
                return [{"id": "2-1", "idReadable": "PROJ-7", "summary": "Dup", "tags": [], "customFields": []}]
            return []
        if method == "POST" and path == "/issues":
            self.created = True
            return {"id": "2-1", "idReadable": "PROJ-7", "summary": "Dup", "tags": [], "customFields": []}
        return {}


def test_create_issue_idempotency_key_dedupes(monkeypatch):
    stub = _StatefulRequest()
    monkeypatch.setattr(server, "_request", stub)
    monkeypatch.setattr(server, "_add_tags_impl", lambda *a, **k: ([], []))

    first = server.create_issue("Dup", project_id="0-1", idempotency_key="abc123")
    second = server.create_issue("Dup", project_id="0-1", idempotency_key="abc123")

    assert first["idempotent_hit"] is False
    assert second["idempotent_hit"] is True
    assert second["id_readable"] == "PROJ-7"
    # Exactly one POST /issues across both calls.
    assert sum(1 for m, p in stub.calls if m == "POST" and p == "/issues") == 1


# ─── §4.1 search_issues envelope + fields ─────────────────────────────────────

def _search_recorder(monkeypatch, items):
    def _request(method, path, body=None, params=None):
        return items
    monkeypatch.setattr(server, "_request", _request)


def test_search_minimal_extracts_state_and_priority(monkeypatch):
    _search_recorder(monkeypatch, [{
        "id": "2-1", "idReadable": "IS-1", "summary": "Bug", "updated": 0,
        "tags": [{"name": "sev:critical"}],
        "customFields": [
            {"name": "State", "value": {"name": "Open"}},
            {"name": "Priority", "value": {"name": "Critical"}},
        ],
    }])

    out = server.search_issues("project: IS", fields="minimal")
    assert out["total"] == 1
    item = out["results"][0]
    assert item["state"] == "Open"
    assert item["priority"] == "Critical"
    assert item["tags"] == ["sev:critical"]
    assert "description" not in item  # minimal omits it


def test_search_standard_adds_description_and_assignee(monkeypatch):
    _search_recorder(monkeypatch, [{
        "idReadable": "IS-1", "summary": "Bug", "description": "detail",
        "reporter": {"login": "jane"},
        "customFields": [{"name": "Assignee", "value": {"login": "bob", "fullName": "Bob"}}],
    }])

    out = server.search_issues("project: IS", fields="standard")
    item = out["results"][0]
    assert item["description"] == "detail"
    assert item["assignee"] == "Bob"
    assert item["reporter"] == "jane"


class _SearchStub:
    """_request stub: GET /issues returns `rows`, the count endpoint replies in turn."""

    def __init__(self, rows, counts):
        self.rows = rows
        self.counts = list(counts)
        self.calls = []

    def __call__(self, method, path, body=None, params=None):
        self.calls.append((method, path, params))
        if path == "/issues":
            return self.rows
        if path == "/issuesGetter/count":
            nxt = self.counts.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return {"count": nxt}
        return {}


def _rows(n):
    return [{"idReadable": f"IS-{i}", "summary": "s"} for i in range(n)]


def test_search_total_counts_all_pages_not_the_page(monkeypatch):
    # limit=2 and YouTrack returns the extra probe row: more pages exist, so the
    # total comes from the count endpoint (which answers -1 until it's ready).
    stub = _SearchStub(_rows(3), counts=[-1, 781])
    monkeypatch.setattr(server, "_request", stub)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)

    out = server.search_issues("project: IS #Unresolved", limit=2)

    assert [r["id_readable"] for r in out["results"]] == ["IS-0", "IS-1"]
    assert out["total"] == 781
    assert out["has_more"] is True
    assert out["next_offset"] == 2
    assert stub.calls[0][2]["$top"] == "3"


def test_search_last_page_total_needs_no_count_call(monkeypatch):
    stub = _SearchStub(_rows(2), counts=[])
    monkeypatch.setattr(server, "_request", stub)

    out = server.search_issues("project: IS", limit=20, offset=40)

    assert out["total"] == 42
    assert out["has_more"] is False
    assert out["next_offset"] is None
    assert [c[1] for c in stub.calls] == ["/issues"]


def test_search_total_is_null_when_count_fails(monkeypatch):
    stub = _SearchStub(_rows(3), counts=[server.YouTrackHTTPError("HTTP 500", status=500)])
    monkeypatch.setattr(server, "_request", stub)

    out = server.search_issues("project: IS", limit=2)

    assert "error" not in out
    assert out["total"] is None
    assert out["has_more"] is True


# ─── §4.3 add_comment with attachments ────────────────────────────────────────

def test_add_comment_uploads_attachments(monkeypatch):
    posted = {}

    def _request(method, path, body=None, params=None):
        posted["path"] = path
        return {"id": "c-1", "created": 0}

    monkeypatch.setattr(server, "_request", _request)
    monkeypatch.setattr(
        server, "_attach_file_impl",
        lambda issue_id, p, file_name=None: {"ok": True, "id": "8-1", "name": p},
    )

    out = server.add_comment("PROJ-1", "see logs", attachments=["/tmp/shot.png"])
    assert out["ok"] is True
    assert out["comment_id"] == "c-1"
    assert out["created"] == "1970-01-01T00:00:00+00:00"
    assert out["attachments"] == [{"ok": True, "id": "8-1", "name": "/tmp/shot.png"}]


# ─── §6.7 structuredContent validates against declared outputSchema ───────────

def test_get_issue_structured_content_validates(monkeypatch):
    monkeypatch.setattr(server, "_request", lambda *a, **k: {
        "id": "2-1", "idReadable": "PROJ-1", "summary": "S",
        "customFields": [], "comments": [], "attachments": [], "tags": [],
    })
    structured, schema = _run_tool("get_issue", {"issue_id": "PROJ-1"})
    jsonschema.validate(structured, schema)
    assert structured["id_readable"] == "PROJ-1"


def test_error_envelope_validates_against_success_schema(monkeypatch):
    # The key robustness property: an error payload must still satisfy the tool's
    # declared outputSchema, so schema validation never breaks on failures.
    def boom(*a, **k):
        raise server.YouTrackNetworkError("down")

    monkeypatch.setattr(server, "_request", boom)
    structured, schema = _run_tool("get_issue", {"issue_id": "PROJ-1"})
    jsonschema.validate(structured, schema)
    assert structured["error"]["code"] == "YOUTRACK_UNAVAILABLE"


def test_search_and_list_envelopes_validate(monkeypatch):
    monkeypatch.setattr(server, "_request", lambda *a, **k: [])
    for name, args in [
        ("search_issues", {"query": "project: IS"}),
        ("list_projects", {}),
    ]:
        structured, schema = _run_tool(name, args)
        jsonschema.validate(structured, schema)


@pytest.mark.parametrize("name", sorted(_TOOLS))
def test_every_tool_declares_object_output_schema(name):
    schema = _TOOLS[name].output_schema
    assert schema is not None, f"{name} has no outputSchema"
    assert schema.get("type") == "object", f"{name} outputSchema is not an object"
