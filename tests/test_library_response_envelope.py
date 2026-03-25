"""Tests for shared library response envelope helpers."""

from __future__ import annotations

from app.modules.library.response_envelope import available_payload, unavailable_payload


def test_available_payload_sets_standard_fields() -> None:
    payload = available_payload(config_source="config.local.yaml", stats={"count": 3})
    assert payload["available"] is True
    assert payload["error"] is None
    assert payload["config_source"] == "config.local.yaml"
    assert payload["stats"] == {"count": 3}


def test_unavailable_payload_sets_standard_fields() -> None:
    payload = unavailable_payload(ValueError("boom"), config_source=None, items=[])
    assert payload["available"] is False
    assert payload["error"] == "boom"
    assert payload["config_source"] is None
    assert payload["items"] == []


def test_payload_coerces_config_source_to_string() -> None:
    payload = available_payload(config_source=123, summary={})
    assert payload["config_source"] == "123"
