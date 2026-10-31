"""Spec §6.7: a minimal JSON-RPC client that speaks to the server over stdio and
validates the declared tool contract.

This is the guarantee a programmatic client relies on: the server is a real
JSON-RPC-over-stdio MCP server, and every tool advertises an `outputSchema` that
is itself a valid JSON Schema. Structured-content-validates-schema for live calls
is covered in-process in test_structured_v05.py (a stdio call would need a live
YouTrack); here we pin the transport + schema declaration end to end.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import jsonschema

SRC = str(Path(__file__).resolve().parent.parent / "src")


def _rpc_roundtrip(timeout=30, request=None):
    """Drive initialize → one request over stdio, return that request's result.

    `request` is {"method", "params"?}; it defaults to tools/list. A background
    reader thread consumes stdout line by line so we can wait for the specific
    response (id 2) before shutting the server down. Relying on stdin-EOF to
    flush would race the in-flight response against cancellation.
    """
    env = {
        **os.environ,
        "YOUTRACK_URL": "https://test.invalid",
        "YOUTRACK_TOKEN": "perm-fake.test.token",
        "PYTHONPATH": SRC + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    proc = subprocess.Popen(
        [sys.executable, "-c", "from youtrack_mcp.server import main; main()"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        bufsize=1,
    )

    lines: "queue.Queue[str | None]" = queue.Queue()

    def _reader():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "acceptance", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, **(request or {"method": "tools/list"})},
    ]
    proc.stdin.write("".join(json.dumps(m) + "\n" for m in messages))
    proc.stdin.flush()

    result = None
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=max(0.05, deadline - time.monotonic()))
            except queue.Empty:
                break
            if line is None:
                break
            try:
                msg = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            if msg.get("id") == 2 and "result" in msg:
                result = msg["result"]
                break
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    assert result is not None, "no response to request id 2 received over stdio"
    return result


def test_stdio_tools_list_declares_valid_output_schemas():
    tools = _rpc_roundtrip()["tools"]
    assert len(tools) >= 25, f"expected the full tool surface, got {len(tools)}"

    for tool in tools:
        name = tool["name"]
        schema = tool.get("outputSchema")
        assert schema is not None, f"{name} advertises no outputSchema over the wire"
        assert schema.get("type") == "object", f"{name} outputSchema is not an object"
        # The advertised schema must itself be a well-formed JSON Schema.
        jsonschema.Draft7Validator.check_schema(schema)


def test_stdio_core_tools_present():
    names = {t["name"] for t in _rpc_roundtrip()["tools"]}
    for required in ("get_issue", "update_issue", "download_attachment",
                     "create_issue", "add_comment", "search_issues"):
        assert required in names, f"{required} missing from the stdio tool list"


def test_stdio_error_envelope_is_flagged_is_error():
    # test.invalid never resolves, so the call fails at the network layer. On
    # the wire the envelope must carry isError: true (MCP spec), alongside the
    # structuredContent an orchestrator routes on.
    result = _rpc_roundtrip(request={
        "method": "tools/call",
        "params": {"name": "get_issue", "arguments": {"issue_id": "PROJ-1"}},
    })
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "YOUTRACK_UNAVAILABLE"
