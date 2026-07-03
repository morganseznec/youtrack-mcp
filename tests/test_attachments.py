"""Tests for download_attachment (§3), get_issue enrichment (§1), and
update_issue tag/clear behavior (§2).

download_attachment mocks server._request (attachment metadata fetch) and
server._request_raw (the binary GET), so path handling, size caps, sha256,
text_preview, and traversal sanitisation are exercised without a live server.
"""

from youtrack_mcp import server


class _Recorder:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def __call__(self, method, path, body=None, params=None):
        self.calls.append({"method": method, "path": path, "body": body, "params": params})
        resp = self.responses.get((method, path))
        if callable(resp):
            return resp(body, params)
        return resp if resp is not None else {}

    def has_call(self, method, path):
        return any(c["method"] == method and c["path"] == path for c in self.calls)


# ─── download_attachment (§3) ─────────────────────────────────────────────────

def _meta(**over):
    base = {"id": "a-1", "name": "app.log", "url": "/api/files/a-1?sign=x",
            "mimeType": "text/plain", "size": 5}
    base.update(over)
    return {("GET", "/issues/PROJ-1/attachments/a-1"): base}


def test_download_writes_file_with_sha_and_preview(monkeypatch, tmp_path):
    rec = _Recorder(_meta())
    monkeypatch.setattr(server, "_request", rec)
    monkeypatch.setattr(server, "_request_raw", lambda url, timeout=60: (b"hello", "text/plain"))

    out = server.download_attachment("PROJ-1", "a-1", dest_path=str(tmp_path) + "/")

    assert out["attachment_id"] == "a-1"
    assert out["name"] == "app.log"
    assert out["size_bytes"] == 5
    assert out["mime_type"] == "text/plain"
    assert out["text_preview"] == "hello"
    import hashlib
    assert out["sha256"] == hashlib.sha256(b"hello").hexdigest()
    assert (tmp_path / "app.log").read_bytes() == b"hello"
    assert out["path"] == str(tmp_path / "app.log")
    # The download URL feeds _request_raw as-is (relative); it already has /api.
    assert "error" not in out


def test_download_binary_has_no_text_preview(monkeypatch, tmp_path):
    rec = _Recorder(_meta(name="shot.png", mimeType="image/png"))
    monkeypatch.setattr(server, "_request", rec)
    monkeypatch.setattr(server, "_request_raw", lambda url, timeout=60: (b"\x89PNG", "image/png"))

    out = server.download_attachment("PROJ-1", "a-1", dest_path=str(tmp_path) + "/")

    assert out["text_preview"] is None
    assert (tmp_path / "shot.png").read_bytes() == b"\x89PNG"


def test_download_unknown_id_returns_not_found(monkeypatch):
    rec = _Recorder({("GET", "/issues/PROJ-1/attachments/nope"): {}})
    monkeypatch.setattr(server, "_request", rec)
    out = server.download_attachment("PROJ-1", "nope")
    assert out["error"]["code"] == "NOT_FOUND"


def test_download_oversize_refused_before_fetch(monkeypatch):
    huge = 20 * 1024 * 1024
    rec = _Recorder(_meta(size=huge))
    monkeypatch.setattr(server, "_request", rec)

    def fake_raw(url, timeout=60):
        raise AssertionError("must not fetch an over-limit attachment")

    monkeypatch.setattr(server, "_request_raw", fake_raw)

    out = server.download_attachment("PROJ-1", "a-1")  # default 10 MB cap
    assert out["error"]["code"] == "VALIDATION_FAILED"
    assert str(huge) in out["error"]["message"]


def test_download_custom_max_size(monkeypatch):
    rec = _Recorder(_meta(size=100))
    monkeypatch.setattr(server, "_request", rec)
    monkeypatch.setattr(server, "_request_raw", lambda url, timeout=60: (b"x" * 100, "text/plain"))

    out = server.download_attachment("PROJ-1", "a-1", max_size_bytes=10)
    assert out["error"]["code"] == "VALIDATION_FAILED"


def test_download_sanitizes_traversal_name(monkeypatch, tmp_path):
    rec = _Recorder(_meta(name="../../etc/passwd"))
    monkeypatch.setattr(server, "_request", rec)
    monkeypatch.setattr(server, "_request_raw", lambda url, timeout=60: (b"hi", "text/plain"))

    out = server.download_attachment("PROJ-1", "a-1", dest_path=str(tmp_path) + "/")
    # Confined to tmp_path: basename only.
    assert out["path"] == str(tmp_path / "passwd")
    assert (tmp_path / "passwd").read_bytes() == b"hi"


def test_download_full_dest_path_used_verbatim(monkeypatch, tmp_path):
    rec = _Recorder(_meta(name="orig.log"))
    monkeypatch.setattr(server, "_request", rec)
    monkeypatch.setattr(server, "_request_raw", lambda url, timeout=60: (b"hi", "text/plain"))

    dest = tmp_path / "renamed.txt"
    out = server.download_attachment("PROJ-1", "a-1", dest_path=str(dest))
    assert out["path"] == str(dest)
    assert dest.read_bytes() == b"hi"


def test_download_default_path_is_attachments_dir(monkeypatch, tmp_path):
    rec = _Recorder(_meta())
    monkeypatch.setattr(server, "_request", rec)
    monkeypatch.setattr(server, "_request_raw", lambda url, timeout=60: (b"hi", "text/plain"))
    monkeypatch.chdir(tmp_path)

    out = server.download_attachment("PROJ-1", "a-1")
    # Default is ./attachments/{issue_id}/{name}, relative to the cwd.
    assert out["path"] == "attachments/PROJ-1/app.log"
    assert (tmp_path / "attachments" / "PROJ-1" / "app.log").read_bytes() == b"hi"


# ─── get_issue enrichment (§1) ────────────────────────────────────────────────

def test_get_issue_includes_attachments_and_comments(monkeypatch):
    rec = _Recorder({
        ("GET", "/issues/PROJ-1"): {
            "id": "2-1",
            "idReadable": "PROJ-1",
            "summary": "Crash",
            "attachments": [
                {"id": "a-1", "name": "app.log", "size": 3, "mimeType": "text/plain",
                 "author": {"login": "jane", "name": "Jane"}, "created": 0,
                 "comment": {"id": "c-1"}},
            ],
            "comments": [
                {"id": "c-1", "text": "looking", "author": {"login": "bob"}, "created": 0,
                 "attachments": [{"id": "a-1"}]},
            ],
        },
    })
    monkeypatch.setattr(server, "_request", rec)

    result = server.get_issue("PROJ-1")

    assert result["id"] == "2-1"
    assert result["id_readable"] == "PROJ-1"
    assert result["attachments"] == [{
        "id": "a-1", "name": "app.log", "mime_type": "text/plain", "size_bytes": 3,
        "created": "1970-01-01T00:00:00+00:00",
        "author": {"login": "jane", "name": "Jane"}, "comment_id": "c-1",
    }]
    assert result["comments"][0]["text"] == "looking"
    assert result["comments"][0]["author"] == {"login": "bob", "name": None}
    assert result["comments"][0]["attachments"] == ["a-1"]


def test_get_issue_truncates_long_description(monkeypatch):
    long_desc = "x" * (server._MAX_TEXT_BYTES + 100)
    rec = _Recorder({("GET", "/issues/PROJ-1"): {"idReadable": "PROJ-1", "description": long_desc}})
    monkeypatch.setattr(server, "_request", rec)

    result = server.get_issue("PROJ-1")
    assert result["truncated"] is True
    assert result["description"].endswith("…[truncated]")
    assert len(result["description"].encode("utf-8")) <= server._MAX_TEXT_BYTES + len("…[truncated]".encode())


def test_get_issue_max_comments_keeps_newest(monkeypatch):
    comments = [{"id": f"c-{i}", "text": str(i)} for i in range(5)]
    rec = _Recorder({("GET", "/issues/PROJ-1"): {"idReadable": "PROJ-1", "comments": comments}})
    monkeypatch.setattr(server, "_request", rec)

    result = server.get_issue("PROJ-1", max_comments=2)
    assert [c["id"] for c in result["comments"]] == ["c-3", "c-4"]


def test_get_issue_include_flags_false(monkeypatch):
    rec = _Recorder({("GET", "/issues/PROJ-1"): {
        "idReadable": "PROJ-1",
        "comments": [{"id": "c-1", "text": "x"}],
        "attachments": [{"id": "a-1", "name": "f"}],
    }})
    monkeypatch.setattr(server, "_request", rec)

    result = server.get_issue("PROJ-1", include_comments=False, include_attachments=False)
    assert result["comments"] == []
    assert result["attachments"] == []


# ─── update_issue tags + clear (§2) ───────────────────────────────────────────

def _core_reread(id_readable="PROJ-1", **over):
    base = {"id": "2-1", "idReadable": id_readable, "summary": "S", "tags": []}
    base.update(over)
    return base


def test_update_issue_add_and_remove_tags(monkeypatch):
    rec = _Recorder({
        ("GET", "/tags"): [{"id": "t-1", "name": "chain:FULL"}],
        ("GET", "/issues/PROJ-1/tags"): [{"id": "t-9", "name": "stale"}],
        ("GET", "/issues/PROJ-1"): _core_reread(tags=[{"name": "chain:FULL"}]),
    })
    monkeypatch.setattr(server, "_request", rec)

    result = server.update_issue("PROJ-1", add_tags=["chain:FULL"], remove_tags=["stale"])

    assert result["applied"]["tags_added"] == ["chain:FULL"]
    assert result["applied"]["tags_removed"] == ["stale"]
    assert result["applied"]["warnings"] == []
    # A tags-only update must not POST to the issue body endpoint.
    assert not rec.has_call("POST", "/issues/PROJ-1")
    assert rec.has_call("POST", "/issues/PROJ-1/tags")
    assert rec.has_call("DELETE", "/issues/PROJ-1/tags/t-9")


def test_update_issue_creates_missing_tag(monkeypatch):
    rec = _Recorder({
        ("GET", "/tags"): [],  # tag doesn't exist yet
        ("POST", "/tags"): {"id": "t-new", "name": "chain:FULL"},
        ("GET", "/issues/PROJ-1"): _core_reread(),
    })
    monkeypatch.setattr(server, "_request", rec)

    result = server.update_issue("PROJ-1", add_tags=["chain:FULL"], create_missing_tags=True)

    assert rec.has_call("POST", "/tags")  # created at instance level
    assert result["applied"]["tags_added"] == ["chain:FULL"]
    assert result["applied"]["warnings"] == []


def test_update_issue_missing_tag_not_created_warns(monkeypatch):
    rec = _Recorder({
        ("GET", "/tags"): [],
        ("GET", "/issues/PROJ-1"): _core_reread(),
    })
    monkeypatch.setattr(server, "_request", rec)

    result = server.update_issue("PROJ-1", add_tags=["ghost"], create_missing_tags=False)

    assert not rec.has_call("POST", "/tags")
    assert result["applied"]["tags_added"] == []
    assert any("ghost" in w for w in result["applied"]["warnings"])


def test_update_issue_clear_sentinel_sends_null(monkeypatch):
    rec = _Recorder({
        ("GET", "/issues/PROJ-1"): {"id": "2-1", "idReadable": "PROJ-1", "project": {"id": "0-1"}},
        ("POST", "/issues/PROJ-1"): {"idReadable": "PROJ-1"},
    })
    monkeypatch.setattr(server, "_request", rec)
    monkeypatch.setattr(server, "_get_project_schema_cached", lambda pid: {
        "project_id": pid, "fields": [{"name": "Assignee", "type": "user[1]", "values": None}],
    })

    server.update_issue("PROJ-1", custom_fields={"Assignee": "__CLEAR__"})

    body = next(c["body"] for c in rec.calls if c["method"] == "POST" and c["path"] == "/issues/PROJ-1")
    assert body["customFields"] == [
        {"name": "Assignee", "$type": "SingleUserIssueCustomField", "value": None},
    ]


def test_update_issue_state_synonym_resolves(monkeypatch):
    rec = _Recorder({
        ("GET", "/issues/PROJ-1"): {"id": "2-1", "idReadable": "PROJ-1", "project": {"id": "0-1"}},
        ("POST", "/issues/PROJ-1"): {"idReadable": "PROJ-1"},
    })
    monkeypatch.setattr(server, "_request", rec)
    monkeypatch.setattr(server, "_get_project_schema_cached", lambda pid: {
        "project_id": pid,
        "fields": [{"name": "State", "type": "state[1]", "values": ["To do", "In Progress", "Done"]}],
    })

    # "fixed" is a synonym that must resolve to the project's "Done".
    server.update_issue("PROJ-1", custom_fields={"State": "fixed"})

    body = next(c["body"] for c in rec.calls if c["method"] == "POST" and c["path"] == "/issues/PROJ-1")
    assert body["customFields"][0]["value"]["name"] == "Done"
