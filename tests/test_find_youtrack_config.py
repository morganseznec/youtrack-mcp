"""Tests for `.youtrack.yml` parsing in `find_youtrack_config`.

Focus: the YAML 1.1 "Norway problem". An unquoted `on:` mapping key is parsed by
PyYAML's `safe_load` as the boolean True, not the string "on", so the evidence
checkpoint list would silently vanish. These tests pin both the defensive
key-restoration helper and the end-to-end config parse.
"""

from pathlib import Path

import yaml

from youtrack_mcp import server

EXAMPLE_FILE = Path(__file__).resolve().parent.parent / "youtrack.example.yml"


# ─── _restore_evidence_on_key (pure helper) ──────────────────────────────────

def test_restore_remaps_coerced_true_to_on():
    # `{True: [...]}` is exactly what safe_load yields for a bare `on:` key.
    restored = server._restore_evidence_on_key({True: ["tests", "build"]})
    assert restored == {"on": ["tests", "build"]}
    assert True not in restored


def test_restore_explicit_on_wins_over_coerced_true():
    # A quoted "on" key must not be clobbered by a stray coerced True.
    restored = server._restore_evidence_on_key({"on": ["a"], True: ["b"]})
    assert restored == {"on": ["a"]}
    assert True not in restored


def test_restore_passthrough_when_no_coercion():
    block = {"enabled": True, "on": ["tests"], "attach_artifacts": False}
    assert server._restore_evidence_on_key(block) == block


def test_restore_leaves_integer_key_one_untouched():
    # Only the *boolean* True is the coerced `on:`. A numeric key 1 compares
    # equal to True but is not it; the helper's `is True` identity check must
    # leave it alone rather than remapping it to "on" (a `== True` check would
    # wrongly catch it). Note: a dict cannot hold both 1 and True as separate
    # keys, so this is the realistic shape to guard.
    restored = server._restore_evidence_on_key({1: "int-key"})
    assert restored == {1: "int-key"}
    assert "on" not in restored


def test_restore_non_dict_passthrough():
    assert server._restore_evidence_on_key(None) is None
    assert server._restore_evidence_on_key(["not", "a", "dict"]) == ["not", "a", "dict"]


# ─── find_youtrack_config end-to-end ─────────────────────────────────────────

def _write_config(tmp_path: Path, body: str) -> dict:
    (tmp_path / ".youtrack.yml").write_text(body, encoding="utf-8")
    result = server.find_youtrack_config(str(tmp_path))
    assert result["found"] is True, result
    assert result["error"] is None, result
    return result["config"]


def test_unquoted_on_is_restored_to_string_list(tmp_path):
    # The core regression: a bare `on:` key must still surface as evidence["on"].
    config = _write_config(tmp_path, """
project_id: "0-1"
evidence:
  enabled: true
  on: [tests, build, commit, deploy]
  attach_artifacts: true
""")
    evidence = config["evidence"]
    assert evidence["on"] == ["tests", "build", "commit", "deploy"]
    assert all(isinstance(c, str) for c in evidence["on"])
    assert True not in evidence  # no stray coerced boolean key leaked through


def test_quoted_on_round_trips(tmp_path):
    config = _write_config(tmp_path, """
project_id: "0-1"
evidence:
  enabled: true
  "on": [tests, deploy]
""")
    assert config["evidence"]["on"] == ["tests", "deploy"]


def test_partial_evidence_block_keeps_defaults(tmp_path):
    # Deep-merge: omitting `on`/`attach_artifacts` falls back to the defaults
    # rather than dropping them (the old shallow merge replaced the whole block).
    config = _write_config(tmp_path, """
project_id: "0-1"
evidence:
  enabled: true
""")
    evidence = config["evidence"]
    assert evidence["enabled"] is True
    assert evidence["on"] == ["tests", "build", "commit", "deploy"]
    assert evidence["attach_artifacts"] is True


def test_no_evidence_block_uses_default_on_list(tmp_path):
    config = _write_config(tmp_path, 'project_id: "0-1"\n')
    assert config["evidence"]["on"] == ["tests", "build", "commit", "deploy"]


# ─── guard the shipped example file ──────────────────────────────────────────

def test_example_file_exposes_on_as_list(tmp_path):
    # Copy the real youtrack.example.yml through the parser. If someone unquotes
    # the `on:` key again, this fails.
    body = EXAMPLE_FILE.read_text(encoding="utf-8")
    config = _write_config(tmp_path, body)
    assert config["evidence"]["on"] == ["tests", "build", "commit", "deploy"]


def test_example_file_raw_parse_has_string_on_key():
    # Belt-and-suspenders: the example must parse with a string "on" key even
    # before any restoration logic runs (i.e. the key really is quoted on disk).
    raw = yaml.safe_load(EXAMPLE_FILE.read_text(encoding="utf-8"))
    assert "on" in raw["evidence"]
    assert True not in raw["evidence"]
