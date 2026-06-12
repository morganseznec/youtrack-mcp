"""Verify _build_custom_fields_payload validates, fuzzy-matches, and emits
the correct YouTrack payload shape (including the inner $type for bundle
elements introduced in v0.2.0).
"""

import pytest

from youtrack_mcp import server


def _stub_schema(project_id):
    return {
        "project_id": project_id,
        "fields": [
            {"name": "Priority", "type": "enum[1]", "values": ["Critical", "Normal"]},
            {"name": "State", "type": "state[1]", "values": ["To do", "Done"]},
            {"name": "Service", "type": "enum[*]", "values": ["AWS", "RDS", "EC2"]},
            {"name": "Component", "type": "text", "values": None},
            {"name": "Repository", "type": "string", "values": None},
            {"name": "CVSS score", "type": "float", "values": None},
            {"name": "Maintenance window", "type": "period", "values": None},
            {"name": "Due Date", "type": "date", "values": None},
            {"name": "Assignee", "type": "user[1]", "values": None},
        ],
    }


@pytest.fixture(autouse=True)
def patched_schema(monkeypatch):
    monkeypatch.setattr(server, "_get_project_schema_cached", _stub_schema)


def test_empty_input_returns_empty_payload():
    assert server._build_custom_fields_payload({}, "0-1") == []
    assert server._build_custom_fields_payload(None, "0-1") == []


def test_enum_field_emits_inner_type():
    payload = server._build_custom_fields_payload({"Priority": "Critical"}, "0-1")
    assert payload == [
        {
            "name": "Priority",
            "$type": "SingleEnumIssueCustomField",
            "value": {"name": "Critical", "$type": "EnumBundleElement"},
        }
    ]


def test_state_field_emits_state_bundle_inner_type():
    payload = server._build_custom_fields_payload({"State": "Done"}, "0-1")
    assert payload[0]["value"] == {"name": "Done", "$type": "StateBundleElement"}


def test_enum_fuzzy_match_canonicalizes():
    payload = server._build_custom_fields_payload({"Priority": "critical"}, "0-1")
    assert payload[0]["value"]["name"] == "Critical"


def test_multi_enum_accepts_list():
    payload = server._build_custom_fields_payload({"Service": ["AWS", "rds"]}, "0-1")
    assert payload[0]["$type"] == "MultiEnumIssueCustomField"
    assert payload[0]["value"] == [
        {"name": "AWS", "$type": "EnumBundleElement"},
        {"name": "RDS", "$type": "EnumBundleElement"},
    ]


def test_text_field_wraps_value_in_text_object():
    # YouTrack rejects a bare string for a text field with a TextFieldValue
    # type-mismatch 400; the value must be {"text": ...}.
    payload = server._build_custom_fields_payload({"Component": "auth"}, "0-1")
    assert payload[0] == {
        "name": "Component",
        "$type": "TextIssueCustomField",
        "value": {"text": "auth"},
    }


def test_text_field_coerces_non_string_to_text():
    payload = server._build_custom_fields_payload({"Component": 42}, "0-1")
    assert payload[0]["value"] == {"text": "42"}


def test_string_field_uses_bare_value():
    payload = server._build_custom_fields_payload({"Repository": "org/repo"}, "0-1")
    assert payload[0] == {
        "name": "Repository",
        "$type": "SimpleIssueCustomField",
        "value": "org/repo",
    }


def test_float_field_uses_bare_value():
    payload = server._build_custom_fields_payload({"CVSS score": 7.5}, "0-1")
    assert payload[0] == {
        "name": "CVSS score",
        "$type": "SimpleIssueCustomField",
        "value": 7.5,
    }


def test_period_field_from_int_minutes():
    payload = server._build_custom_fields_payload({"Maintenance window": 90}, "0-1")
    assert payload[0] == {
        "name": "Maintenance window",
        "$type": "PeriodIssueCustomField",
        "value": {"minutes": 90},
    }


def test_period_field_from_duration_string():
    payload = server._build_custom_fields_payload({"Maintenance window": "1h 30m"}, "0-1")
    assert payload[0]["value"] == {"minutes": 90}


def test_period_field_unparseable_raises():
    with pytest.raises(server.YouTrackError, match="could not parse period"):
        server._build_custom_fields_payload({"Maintenance window": "soon"}, "0-1")


def test_date_field_converts_iso_to_millis():
    payload = server._build_custom_fields_payload({"Due Date": "2026-06-20"}, "0-1")
    assert payload[0]["$type"] == "SimpleIssueCustomField"
    assert payload[0]["value"] == server._date_to_ms("2026-06-20")
    assert isinstance(payload[0]["value"], int)


def test_date_field_passes_epoch_millis_through():
    payload = server._build_custom_fields_payload({"Due Date": 1781000000000}, "0-1")
    assert payload[0]["value"] == 1781000000000


def test_date_field_bad_format_raises():
    with pytest.raises(server.YouTrackError, match="invalid date"):
        server._build_custom_fields_payload({"Due Date": "20/06/2026"}, "0-1")


def test_unknown_field_raises():
    with pytest.raises(server.YouTrackError, match="Unknown custom field"):
        server._build_custom_fields_payload({"Bogus": "x"}, "0-1")


def test_invalid_enum_value_raises():
    with pytest.raises(server.YouTrackError, match="invalid for field 'Priority'"):
        server._build_custom_fields_payload({"Priority": "ZZZ"}, "0-1")


def test_invalid_multi_enum_value_raises():
    with pytest.raises(server.YouTrackError, match="invalid for field 'Service'"):
        server._build_custom_fields_payload({"Service": ["AWS", "zzz"]}, "0-1")


def test_user_field_emits_login():
    payload = server._build_custom_fields_payload({"Assignee": "morgan.s"}, "0-1")
    assert payload[0] == {
        "name": "Assignee",
        "$type": "SingleUserIssueCustomField",
        "value": {"login": "morgan.s"},
    }


def test_none_value_clears_field():
    payload = server._build_custom_fields_payload({"Priority": None}, "0-1")
    assert payload[0]["value"] is None


def test_none_clears_text_field_without_wrapping():
    # None must short-circuit to a null value, not become {"text": "None"}.
    payload = server._build_custom_fields_payload({"Component": None}, "0-1")
    assert payload[0]["value"] is None
