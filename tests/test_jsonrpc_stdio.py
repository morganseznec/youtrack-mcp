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
import subprocess
import sys
from pathlib import Path

import jsonschema

SRC = str(Path(__file__).resolve().parent.parent / "src")


def _rpc_roundtrip():
    """Drive one initialize → tools/list exchange over stdio, return the tools."""
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
    )
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "acceptance", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    payload = "".join(json.dumps(m) + "\n" for m in messages)
    # Closing stdin lets the server drain the queued messages then exit cleanly.
    out, err = proc.communicate(input=payload, timeout=30)

    responses = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            responses.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    tools_resp = next((r for r in responses if r.get("id") == 2), None)
    assert tools_resp is not None, f"no tools/list response.\nSTDOUT:{out}\nSTDERR:{err}"
    assert "result" in tools_resp, tools_resp
    return tools_resp["result"]["tools"]


def test_stdio_tools_list_declares_valid_output_schemas():
    tools = _rpc_roundtrip()
    assert len(tools) >= 25, f"expected the full tool surface, got {len(tools)}"

    for tool in tools:
        name = tool["name"]
        schema = tool.get("outputSchema")
        assert schema is not None, f"{name} advertises no outputSchema over the wire"
        assert schema.get("type") == "object", f"{name} outputSchema is not an object"
        # The advertised schema must itself be a well-formed JSON Schema.
        jsonschema.Draft7Validator.check_schema(schema)


def test_stdio_core_tools_present():
    names = {t["name"] for t in _rpc_roundtrip()}
    for required in ("get_issue", "update_issue", "download_attachment",
                     "create_issue", "add_comment", "search_issues"):
        assert required in names, f"{required} missing from the stdio tool list"
