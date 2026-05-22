"""Unit tests for the pure helpers in `youtrack_mcp.server`.

Covers normalization, fuzzy matching, close-state resolution, and the token
redaction helper. No network, no FastMCP machinery touched.
"""

import pytest

from youtrack_mcp import server


# ─── _normalize ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("inp,expected", [
    ("Won't fix", "wontfix"),
    ("In Progress", "inprogress"),
    ("Show-stopper", "showstopper"),
    ("S0 / S1", "s0s1"),
    ("", ""),
    ("   ", ""),
    ("ALREADY_LOWERCASE_123", "already_lowercase_123".replace("_", "")),
])
def test_normalize(inp, expected):
    assert server._normalize(inp) == expected


# ─── _match_value ────────────────────────────────────────────────────────────

PRIORITY = ["Show-stopper", "Critical", "Major", "Normal", "Minor"]


def test_match_value_exact():
    assert server._match_value("Critical", PRIORITY) == "Critical"


def test_match_value_case_insensitive():
    assert server._match_value("critical", PRIORITY) == "Critical"
    assert server._match_value("CRITICAL", PRIORITY) == "Critical"


def test_match_value_punctuation_normalized():
    assert server._match_value("showstopper", PRIORITY) == "Show-stopper"
    assert server._match_value("Show stopper", PRIORITY) == "Show-stopper"


def test_match_value_unknown_returns_none():
    assert server._match_value("Urgent", PRIORITY) is None


def test_match_value_empty_input_returns_none():
    assert server._match_value("", PRIORITY) is None
    assert server._match_value("   ", PRIORITY) is None


def test_match_value_open_field_passes_through():
    # No allowed list means the field accepts any string.
    assert server._match_value("anything", []) == "anything"
    assert server._match_value("anything", None) == "anything"  # type: ignore[arg-type]


# ─── _resolve_state ──────────────────────────────────────────────────────────

IS_STATES = ["To do", "In Progress", "Done"]                       # state[1] project
CLASSIC_STATES = ["Open", "In Progress", "Fixed", "Verified", "Won't fix"]


def test_resolve_state_none_picks_done_on_is_project():
    assert server._resolve_state(None, IS_STATES) == "Done"


def test_resolve_state_fixed_synonym_resolves_to_done():
    assert server._resolve_state("Fixed", IS_STATES) == "Done"
    assert server._resolve_state("fixed", IS_STATES) == "Done"
    assert server._resolve_state("resolved", IS_STATES) == "Done"


def test_resolve_state_case_insensitive_match():
    assert server._resolve_state("in progress", IS_STATES) == "In Progress"


def test_resolve_state_unknown_returns_none():
    assert server._resolve_state("zzzz-unknown", IS_STATES) is None


def test_resolve_state_none_picks_fixed_on_classic_project():
    assert server._resolve_state(None, CLASSIC_STATES) == "Fixed"


def test_resolve_state_wontfix_synonym_with_punctuation():
    assert server._resolve_state("wontfix", CLASSIC_STATES) == "Won't fix"
    assert server._resolve_state("won't fix", CLASSIC_STATES) == "Won't fix"
    assert server._resolve_state("WONTFIX", CLASSIC_STATES) == "Won't fix"


def test_resolve_state_open_allowed_returns_unchanged_input():
    # No allowed list = no constraint, return what was requested.
    assert server._resolve_state("anything", []) == "anything"


# ─── _redact ─────────────────────────────────────────────────────────────────

def test_redact_strips_known_token(monkeypatch):
    monkeypatch.setattr(server, "YOUTRACK_TOKEN", "perm-SECRET.value.xyz")
    msg = "Authorization: Bearer perm-SECRET.value.xyz failed"
    redacted = server._redact(msg)
    assert "perm-SECRET" not in redacted
    assert "<redacted>" in redacted


def test_redact_handles_empty_token(monkeypatch):
    monkeypatch.setattr(server, "YOUTRACK_TOKEN", "")
    assert server._redact("hello") == "hello"


def test_redact_handles_empty_text(monkeypatch):
    monkeypatch.setattr(server, "YOUTRACK_TOKEN", "perm-X")
    assert server._redact("") == ""


# ─── inner $type wiring ──────────────────────────────────────────────────────

def test_inner_type_map_covers_state_and_enums():
    # Critical: state and enums need the inner $type discriminator.
    assert server._INNER_VALUE_TYPE_MAP["StateIssueCustomField"] == "StateBundleElement"
    assert server._INNER_VALUE_TYPE_MAP["SingleEnumIssueCustomField"] == "EnumBundleElement"
    assert server._INNER_VALUE_TYPE_MAP["MultiEnumIssueCustomField"] == "EnumBundleElement"


# ─── _resolve_token ──────────────────────────────────────────────────────────

def test_resolve_token_prefers_direct_env(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("from-file\n")
    monkeypatch.setenv("YOUTRACK_TOKEN", "from-env")
    monkeypatch.setenv("YOUTRACK_TOKEN_FILE", str(token_file))
    assert server._resolve_token() == "from-env"


def test_resolve_token_falls_back_to_file(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("from-file\n")
    monkeypatch.delenv("YOUTRACK_TOKEN", raising=False)
    monkeypatch.setenv("YOUTRACK_TOKEN_FILE", str(token_file))
    monkeypatch.delenv("YOUTRACK_TOKEN_CMD", raising=False)
    assert server._resolve_token() == "from-file"


def test_resolve_token_falls_back_to_cmd(monkeypatch):
    monkeypatch.delenv("YOUTRACK_TOKEN", raising=False)
    monkeypatch.delenv("YOUTRACK_TOKEN_FILE", raising=False)
    monkeypatch.setenv("YOUTRACK_TOKEN_CMD", "printf from-cmd")
    assert server._resolve_token() == "from-cmd"


def test_resolve_token_empty_when_no_source(monkeypatch):
    monkeypatch.delenv("YOUTRACK_TOKEN", raising=False)
    monkeypatch.delenv("YOUTRACK_TOKEN_FILE", raising=False)
    monkeypatch.delenv("YOUTRACK_TOKEN_CMD", raising=False)
    assert server._resolve_token() == ""


def test_resolve_token_cmd_failure_raises_systemexit(monkeypatch):
    monkeypatch.delenv("YOUTRACK_TOKEN", raising=False)
    monkeypatch.delenv("YOUTRACK_TOKEN_FILE", raising=False)
    monkeypatch.setenv("YOUTRACK_TOKEN_CMD", "false")  # exits 1
    with pytest.raises(SystemExit):
        server._resolve_token()
