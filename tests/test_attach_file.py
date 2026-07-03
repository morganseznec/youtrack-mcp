"""Tests for attach_file and its multipart request construction.

Mocks server._send to capture the prepared urllib Request and assert the
multipart/form-data body shape, since that wire format is easy to get subtly
wrong and impossible to validate without a live server otherwise.
"""

import pytest

from youtrack_mcp import server


def _content_type(req):
    for k, v in req.headers.items():
        if k.lower() == "content-type":
            return v
    raise AssertionError("no Content-Type header set")


def test_attach_file_builds_multipart(monkeypatch, tmp_path):
    f = tmp_path / "junit.xml"
    f.write_bytes(b"<testsuite tests='3'/>")

    captured = {}

    def fake_send(req, method, path, timeout=15):
        captured["req"] = req
        captured["path"] = path
        return [{
            "id": "8-1", "name": "junit.xml", "size": 22,
            "mimeType": "application/xml", "url": "/api/files/8-1?sign=abc",
        }]

    monkeypatch.setattr(server, "_send", fake_send)

    result = server.attach_file("IS-1", str(f))

    req = captured["req"]
    assert captured["path"] == "/issues/IS-1/attachments"
    assert req.method == "POST"

    ct = _content_type(req)
    assert ct.startswith("multipart/form-data; boundary=")
    boundary = ct.split("boundary=", 1)[1]

    body = bytes(req.data)
    assert b'name="upload"; filename="junit.xml"' in body
    assert b"<testsuite tests='3'/>" in body
    assert ("--" + boundary).encode() in body
    assert body.rstrip().endswith(("--" + boundary + "--").encode())

    # Response mapping, including relative-URL absolutization.
    assert result["ok"] is True
    assert result["issue_id"] == "IS-1"
    assert result["id"] == "8-1"
    assert result["name"] == "junit.xml"
    assert result["size"] == 22
    assert result["mime_type"] == "application/xml"
    assert result["url"] == f"{server.YOUTRACK_URL}/api/files/8-1?sign=abc"


def test_attach_file_custom_name(monkeypatch, tmp_path):
    f = tmp_path / "report.txt"
    f.write_bytes(b"ok")
    captured = {}

    def fake_send(req, method, path, timeout=15):
        captured["req"] = req
        return [{"id": "8-2", "name": "evidence.txt", "url": "https://abs/url"}]

    monkeypatch.setattr(server, "_send", fake_send)

    result = server.attach_file("IS-1", str(f), file_name="evidence.txt")

    assert b'filename="evidence.txt"' in bytes(captured["req"].data)
    # Absolute URLs from the server are passed through unchanged.
    assert result["url"] == "https://abs/url"


def test_attach_file_missing_file_returns_error(monkeypatch):
    monkeypatch.setattr(server, "_send", lambda *a, **k: {})
    result = server.attach_file("IS-1", "/no/such/file.xyz")
    assert result["error"]["code"] == "VALIDATION_FAILED"
    assert "not a readable file" in result["error"]["message"]
