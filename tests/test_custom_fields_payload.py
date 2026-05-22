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


def test_text_field_no_inner_type():
    payload = server._build_custom_fields_payload({"Component": "auth"}, "0-1")
    assert payload[0] == {
        "name": "Component",
        "$type": "TextIssueCustomField",
        "value": "auth",
    }


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
