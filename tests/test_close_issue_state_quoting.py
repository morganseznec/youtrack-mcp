"""Verify close_issue wraps multi-word states in braces (fix for the regression
where `State In Progress` was parsed as two arguments by YouTrack).
"""

from unittest.mock import patch

import pytest

from youtrack_mcp import server


def _stub_request(method, path, body=None, params=None):
    """Mock _request to capture the call instead of hitting YouTrack."""
    if path == "/commands":
        _stub_request.last_command_body = body
        _stub_request.last_command_path = path
        return {}
    if path == "/issues/PROJ-1":
        return {"project": {"id": "0-1"}}
    raise AssertionError(f"unexpected path: {path}")


def _stub_schema(project_id):
    return {
        "project_id": "0-1",
        "fields": [
            {
                "name": "State",
                "type": "state[1]",
                "values": ["Open", "In Progress", "Won't fix", "Done"],
            }
        ],
    }


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(server, "_request", _stub_request)
    monkeypatch.setattr(server, "_get_project_schema_cached", _stub_schema)
    _stub_request.last_command_body = None


def test_close_issue_wraps_multi_word_state_in_braces(patched):
    server._close_issue_impl("PROJ-1", state="In Progress")
    body = _stub_request.last_command_body
    assert body is not None
    assert body["query"] == "State {In Progress}"
    assert body["issues"] == [{"idReadable": "PROJ-1"}]
    assert _stub_request.last_command_path == "/commands"


def test_close_issue_wraps_apostrophe_state_in_braces(patched):
    server._close_issue_impl("PROJ-1", state="wontfix")
    body = _stub_request.last_command_body
    # synonym resolution: "wontfix" → "Won't fix" in allowed list
    assert body["query"] == "State {Won't fix}"


def test_close_issue_picks_done_when_state_none(patched):
    server._close_issue_impl("PROJ-1", state=None)
    body = _stub_request.last_command_body
    assert body["query"] == "State {Done}"
    # Issue is specified in the body, not the URL.
    assert body["issues"] == [{"idReadable": "PROJ-1"}]


def test_close_issue_raises_when_no_state_field(monkeypatch):
    monkeypatch.setattr(
        server, "_request", lambda *a, **kw: {"project": {"id": "0-1"}}
    )
    monkeypatch.setattr(
        server,
        "_get_project_schema_cached",
        lambda pid: {"project_id": pid, "fields": []},  # no State field
    )
    with pytest.raises(server.YouTrackError, match="no 'State' field"):
        server._close_issue_impl("PROJ-1", state="Done")


def test_close_issue_raises_when_state_not_in_allowed(patched):
    with pytest.raises(server.YouTrackError, match="Cannot resolve state"):
        server._close_issue_impl("PROJ-1", state="xyz-unknown")
