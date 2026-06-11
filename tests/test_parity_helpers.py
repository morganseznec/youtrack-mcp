"""Unit tests for the pure helpers backing the JetBrains-parity tools (v0.3).

Covers duration parsing, link-type resolution, custom-field value flattening,
link flattening, timestamp/date conversion, and user projection. No network.
"""

from datetime import datetime, timezone

import pytest

from youtrack_mcp import server


# ─── _parse_duration_to_minutes ──────────────────────────────────────────────

@pytest.mark.parametrize("inp,expected", [
    ("90", 90),
    ("90m", 90),
    ("45 m", 45),
    ("2h", 120),
    ("1h 30m", 90),
    ("1h30m", 90),
    ("1d", 8 * 60),
    ("1w", 5 * 8 * 60),
    ("1w 1d 1h 1m", 5 * 8 * 60 + 8 * 60 + 60 + 1),
    ("", 0),
    ("garbage", 0),
])
def test_parse_duration_to_minutes(inp, expected):
    assert server._parse_duration_to_minutes(inp) == expected


# ─── _resolve_link_phrase ─────────────────────────────────────────────────────

@pytest.mark.parametrize("inp,expected", [
    (None, "relates to"),
    ("", "relates to"),
    ("relates", "relates to"),
    ("relates to", "relates to"),
    ("depends on", "depends on"),
    ("DependsOn", "depends on"),
    ("is required for", "is required for"),
    ("duplicates", "duplicates"),
    ("is duplicated by", "is duplicated by"),
    ("subtask", "subtask of"),
    ("subtask of", "subtask of"),
    ("parent", "parent for"),
])
def test_resolve_link_phrase(inp, expected):
    assert server._resolve_link_phrase(inp) == expected


def test_resolve_link_phrase_unknown_raises():
    with pytest.raises(server.YouTrackError, match="Unknown link type"):
        server._resolve_link_phrase("frobnicate")


# ─── _flatten_cf_value ────────────────────────────────────────────────────────

def test_flatten_cf_value_picks_name():
    assert server._flatten_cf_value({"name": "Done", "$type": "StateBundleElement"}) == "Done"


def test_flatten_cf_value_prefers_fullname_over_login():
    assert server._flatten_cf_value({"login": "jane", "fullName": "Jane Doe"}) == "Jane Doe"


def test_flatten_cf_value_login_only():
    assert server._flatten_cf_value({"login": "jane"}) == "jane"


def test_flatten_cf_value_period_prefers_presentation():
    assert server._flatten_cf_value({"presentation": "1h 30m", "minutes": 90}) == "1h 30m"


def test_flatten_cf_value_minutes_fallback():
    assert server._flatten_cf_value({"minutes": 90}) == 90


def test_flatten_cf_value_list():
    assert server._flatten_cf_value([{"name": "AWS"}, {"name": "RDS"}]) == ["AWS", "RDS"]


@pytest.mark.parametrize("inp", [None, 42, 3.5, "raw"])
def test_flatten_cf_value_scalars_passthrough(inp):
    assert server._flatten_cf_value(inp) == inp


# ─── _flatten_links ───────────────────────────────────────────────────────────

def test_flatten_links_outward_uses_source_to_target():
    links = [{
        "direction": "OUTWARD",
        "linkType": {"name": "Depend", "sourceToTarget": "is required for", "targetToSource": "depends on"},
        "issues": [{"idReadable": "IS-2"}],
    }]
    assert server._flatten_links(links) == [{"relation": "is required for", "issues": ["IS-2"]}]


def test_flatten_links_inward_uses_target_to_source():
    links = [{
        "direction": "INWARD",
        "linkType": {"name": "Depend", "sourceToTarget": "is required for", "targetToSource": "depends on"},
        "issues": [{"idReadable": "IS-9"}],
    }]
    assert server._flatten_links(links) == [{"relation": "depends on", "issues": ["IS-9"]}]


def test_flatten_links_drops_empty_and_handles_both():
    links = [
        {"direction": "BOTH", "linkType": {"name": "Relates"}, "issues": []},  # dropped
        {"direction": "BOTH", "linkType": {"name": "Relates"}, "issues": [{"idReadable": "IS-3"}]},
    ]
    assert server._flatten_links(links) == [{"relation": "Relates", "issues": ["IS-3"]}]


def test_flatten_links_empty():
    assert server._flatten_links(None) == []
    assert server._flatten_links([]) == []


# ─── _ms_to_iso ───────────────────────────────────────────────────────────────

def test_ms_to_iso_epoch_zero():
    assert server._ms_to_iso(0) == "1970-01-01T00:00:00+00:00"


def test_ms_to_iso_none_passthrough():
    assert server._ms_to_iso(None) is None


def test_ms_to_iso_non_numeric_passthrough():
    assert server._ms_to_iso("already-a-string") == "already-a-string"


# ─── _date_to_ms ──────────────────────────────────────────────────────────────

def test_date_to_ms_iso_string():
    expected = int(datetime(2026, 6, 11, tzinfo=timezone.utc).timestamp() * 1000)
    assert server._date_to_ms("2026-06-11") == expected


def test_date_to_ms_epoch_passthrough():
    assert server._date_to_ms(1749600000000) == 1749600000000
    assert server._date_to_ms("1749600000000") == 1749600000000


def test_date_to_ms_invalid_raises():
    with pytest.raises(server.YouTrackError, match="invalid date"):
        server._date_to_ms("11/06/2026")


def test_date_to_ms_bool_rejected():
    # bool is an int subclass; must not be silently treated as epoch millis.
    with pytest.raises(server.YouTrackError):
        server._date_to_ms(True)


# ─── _map_user ────────────────────────────────────────────────────────────────

def test_map_user():
    assert server._map_user(
        {"id": "1-1", "login": "jane", "fullName": "Jane Doe", "email": "j@x.io"}
    ) == {"id": "1-1", "login": "jane", "full_name": "Jane Doe", "email": "j@x.io"}


def test_map_user_non_dict():
    assert server._map_user(None) == {}
    assert server._map_user("nope") == {}
