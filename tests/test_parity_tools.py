"""Wire-format tests for the JetBrains-parity tools (v0.3).

These mock `server._request` and assert the exact body/path sent to YouTrack,
since the request shape (custom-field $types, command phrasing, tag-by-id,
work-item duration) is the part most likely to silently break.
"""

from datetime import datetime, timezone

import pytest

from youtrack_mcp import server


class _Recorder:
    """Stub for server._request that records calls and returns canned responses.

    responses maps (method, path) -> a dict/list, or a callable (body, params) -> result.
    Unmatched calls return {}.
    """

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def __call__(self, method, path, body=None, params=None):
        self.calls.append({"method": method, "path": path, "body": body, "params": params})
        resp = self.responses.get((method, path))
        if callable(resp):
            return resp(body, params)
        return resp if resp is not None else {}

    def body_for(self, method, path):
        for c in self.calls:
            if c["method"] == method and c["path"] == path:
                return c["body"]
        raise AssertionError(f"no {method} {path} call recorded; got {self.calls}")

    def has_call(self, method, path):
        return any(c["method"] == method and c["path"] == path for c in self.calls)


def _schema_with(*fields):
    return lambda project_id: {"project_id": project_id, "fields": list(fields)}


# ─── update_issue ─────────────────────────────────────────────────────────────

def test_update_issue_sends_summary_and_custom_fields(monkeypatch):
    rec = _Recorder({
        ("GET", "/issues/IS-1"): {"idReadable": "IS-1", "project": {"id": "0-1"}},
        ("POST", "/issues/IS-1"): {"idReadable": "IS-1"},
    })
    monkeypatch.setattr(server, "_request", rec)
    monkeypatch.setattr(server, "_get_project_schema_cached", _schema_with(
        {"name": "Priority", "type": "enum[1]", "values": ["Critical", "Normal"]},
    ))

    result = server.update_issue("IS-1", summary="New title", custom_fields={"Priority": "critical"})

    body = rec.body_for("POST", "/issues/IS-1")
    assert body["summary"] == "New title"
    assert body["customFields"] == [
        {"name": "Priority", "$type": "SingleEnumIssueCustomField",
         "value": {"name": "Critical", "$type": "EnumBundleElement"}},
    ]
    # New shape: the get_issue-style projection plus an `applied` block.
    assert result["id_readable"] == "IS-1"
    assert result["url"] == f"{server.YOUTRACK_URL}/issue/IS-1"
    assert result["applied"]["custom_fields"] == ["Priority"]
    assert result["applied"]["warnings"] == []
    assert "error" not in result


def test_update_issue_bad_enum_value_returns_validation_error_with_suggestion(monkeypatch):
    rec = _Recorder({("GET", "/issues/IS-1"): {"idReadable": "IS-1", "project": {"id": "0-1"}}})
    monkeypatch.setattr(server, "_request", rec)
    monkeypatch.setattr(server, "_get_project_schema_cached", _schema_with(
        {"name": "Priority", "type": "enum[1]", "values": ["Critical", "Normal"]},
    ))

    result = server.update_issue("IS-1", custom_fields={"Priority": "Criticall"})

    assert result["error"]["code"] == "VALIDATION_FAILED"
    assert "Critical" in result["error"]["message"]


def test_update_issue_nothing_to_update_returns_error(monkeypatch):
    monkeypatch.setattr(server, "_request", _Recorder())
    result = server.update_issue("IS-1")
    assert result["error"]["code"] == "VALIDATION_FAILED"
    assert "nothing to update" in result["error"]["message"]


# ─── change_issue_assignee ────────────────────────────────────────────────────

def test_change_issue_assignee_sets_login(monkeypatch):
    rec = _Recorder({
        ("GET", "/issues/IS-1"): {"project": {"id": "0-1"}},
        ("POST", "/issues/IS-1"): {"idReadable": "IS-1", "summary": "S"},
    })
    monkeypatch.setattr(server, "_request", rec)
    monkeypatch.setattr(server, "_get_project_schema_cached", _schema_with(
        {"name": "Assignee", "type": "user[1]", "values": None},
    ))

    result = server.change_issue_assignee("IS-1", "jane.doe")

    assert rec.body_for("POST", "/issues/IS-1")["customFields"] == [
        {"name": "Assignee", "$type": "SingleUserIssueCustomField", "value": {"login": "jane.doe"}},
    ]
    assert result["assignee"] == "jane.doe"


def test_change_issue_assignee_unassign_clears_field(monkeypatch):
    rec = _Recorder({
        ("GET", "/issues/IS-1"): {"project": {"id": "0-1"}},
        ("POST", "/issues/IS-1"): {"idReadable": "IS-1", "summary": "S"},
    })
    monkeypatch.setattr(server, "_request", rec)
    monkeypatch.setattr(server, "_get_project_schema_cached", _schema_with(
        {"name": "Assignee", "type": "user[1]", "values": None},
    ))

    result = server.change_issue_assignee("IS-1", None)

    assert rec.body_for("POST", "/issues/IS-1")["customFields"] == [
        {"name": "Assignee", "$type": "SingleUserIssueCustomField", "value": None},
    ]
    assert result["assignee"] is None


# ─── link_issues ──────────────────────────────────────────────────────────────

def test_link_issues_default_relates(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(server, "_request", rec)

    result = server.link_issues("IS-1", "IS-2")

    assert rec.body_for("POST", "/commands") == {
        "query": "relates to IS-2", "issues": [{"idReadable": "IS-1"}],
    }
    assert result["link_type"] == "relates to"


def test_link_issues_depends_on(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(server, "_request", rec)

    server.link_issues("IS-10", "IS-3", "depends on")

    assert rec.body_for("POST", "/commands")["query"] == "depends on IS-3"


def test_link_issues_unknown_type_returns_error(monkeypatch):
    monkeypatch.setattr(server, "_request", _Recorder())
    result = server.link_issues("IS-1", "IS-2", "frobnicate")
    assert result["error"]["code"] == "VALIDATION_FAILED"
    assert "Unknown link type" in result["error"]["message"]


# ─── manage_issue_tags ────────────────────────────────────────────────────────

def test_manage_issue_tags_add_by_id_and_report_missing(monkeypatch):
    rec = _Recorder({
        ("GET", "/tags"): [{"id": "1-1", "name": "bug"}, {"id": "1-2", "name": "urgent"}],
        ("GET", "/issues/IS-1/tags"): [{"id": "1-2", "name": "urgent"}],
    })
    monkeypatch.setattr(server, "_request", rec)

    result = server.manage_issue_tags("IS-1", add=["bug", "ghost"], remove=["urgent"])

    # Tags are added by their resolved database id, never by name.
    assert rec.body_for("POST", "/issues/IS-1/tags") == {"id": "1-1"}
    assert rec.has_call("DELETE", "/issues/IS-1/tags/1-2")
    assert result["added"] == ["bug"]
    assert result["removed"] == ["urgent"]
    assert "ghost" in result["not_found"]


def test_manage_issue_tags_case_insensitive_add(monkeypatch):
    rec = _Recorder({("GET", "/tags"): [{"id": "1-1", "name": "Bug"}]})
    monkeypatch.setattr(server, "_request", rec)

    result = server.manage_issue_tags("IS-1", add=["bug"])

    assert rec.body_for("POST", "/issues/IS-1/tags") == {"id": "1-1"}
    assert result["added"] == ["bug"]


# ─── log_work ─────────────────────────────────────────────────────────────────

def test_log_work_duration_string_to_minutes_with_type(monkeypatch):
    rec = _Recorder({
        ("GET", "/issues/IS-1"): {"project": {"id": "0-1"}},
        ("GET", "/admin/projects/0-1/timeTrackingSettings/workItemTypes"):
            [{"id": "49-0", "name": "Development"}],
        ("POST", "/issues/IS-1/timeTracking/workItems"):
            {"id": "w1", "duration": {"minutes": 90, "presentation": "1h 30m"},
             "text": "fixed it", "type": {"name": "Development"}},
    })
    monkeypatch.setattr(server, "_request", rec)

    result = server.log_work("IS-1", duration="1h 30m", text="fixed it",
                             date="2026-06-11", work_type="development")

    body = rec.body_for("POST", "/issues/IS-1/timeTracking/workItems")
    assert body["duration"] == {"minutes": 90}
    assert body["text"] == "fixed it"
    assert body["type"] == {"id": "49-0"}
    expected_ms = int(datetime(2026, 6, 11, tzinfo=timezone.utc).timestamp() * 1000)
    assert body["date"] == expected_ms
    assert result["minutes"] == 90
    assert result["presentation"] == "1h 30m"
    assert result["work_type"] == "Development"


def test_log_work_minutes_int(monkeypatch):
    rec = _Recorder({
        ("POST", "/issues/IS-1/timeTracking/workItems"):
            {"duration": {"minutes": 30, "presentation": "30m"}},
    })
    monkeypatch.setattr(server, "_request", rec)

    server.log_work("IS-1", minutes=30)

    assert rec.body_for("POST", "/issues/IS-1/timeTracking/workItems")["duration"] == {"minutes": 30}


def test_log_work_requires_time_returns_error(monkeypatch):
    monkeypatch.setattr(server, "_request", _Recorder())
    result = server.log_work("IS-1")
    assert result["error"]["code"] == "VALIDATION_FAILED"
    assert "positive 'minutes'" in result["error"]["message"]


# ─── get_issue (response flattening) ──────────────────────────────────────────

def test_get_issue_flattens_fields_tags_links(monkeypatch):
    rec = _Recorder({
        ("GET", "/issues/IS-1"): {
            "idReadable": "IS-1",
            "summary": "Login bug",
            "description": "desc",
            "created": 0,
            "resolved": None,
            "reporter": {"login": "jane"},
            "project": {"shortName": "IS"},
            "customFields": [
                {"name": "State", "value": {"name": "In Progress"}},
                {"name": "Assignee", "value": {"login": "bob", "fullName": "Bob R"}},
                {"name": "Priority", "value": None},
            ],
            "commentsCount": 4,
            "tags": [{"name": "regression"}],
            "links": [{
                "direction": "OUTWARD",
                "linkType": {"sourceToTarget": "relates to"},
                "issues": [{"idReadable": "IS-9"}],
            }],
        },
    })
    monkeypatch.setattr(server, "_request", rec)

    result = server.get_issue("IS-1")

    assert result["custom_fields"] == {"State": "In Progress", "Assignee": "Bob R", "Priority": None}
    assert result["reporter"] == {"login": "jane", "name": None}
    assert result["project"] == {"id": None, "short_name": "IS"}
    assert result["created"] == "1970-01-01T00:00:00+00:00"
    assert result["is_resolved"] is False
    assert result["comments_count"] == 4
    assert result["tags"] == ["regression"]
    assert result["links"] == [{"relation": "relates to", "issues": ["IS-9"]}]
    assert result["url"] == f"{server.YOUTRACK_URL}/issue/IS-1"


# ─── find_user / search_articles client-side filtering ────────────────────────

def test_find_user_filters_client_side_even_if_server_ignores_query(monkeypatch):
    # Simulate an instance that ignores the undocumented ?query= param and
    # returns every user. We must still narrow to the requested substring.
    users = [
        {"id": "1", "login": "jane.doe", "fullName": "Jane Doe", "email": "jane@x.io"},
        {"id": "2", "login": "bob", "fullName": "Bob Roy", "email": "bob@x.io"},
    ]
    monkeypatch.setattr(server, "_request", _Recorder({("GET", "/users"): users}))

    result = server.find_user("jane")

    assert result == {
        "items": [{"id": "1", "login": "jane.doe", "full_name": "Jane Doe", "email": "jane@x.io"}],
        "count": 1,
    }


def test_search_articles_filters_by_title(monkeypatch):
    articles = [
        {"idReadable": "IS-A-1", "summary": "Deploy runbook", "project": {"name": "IS"}},
        {"idReadable": "IS-A-2", "summary": "Onboarding", "project": {"name": "IS"}},
    ]
    monkeypatch.setattr(server, "_request", _Recorder({("GET", "/articles"): articles}))

    result = server.search_articles("deploy")

    assert result == {
        "items": [{"id": "IS-A-1", "summary": "Deploy runbook", "project": "IS"}],
        "count": 1,
    }
