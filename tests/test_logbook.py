"""Tests for logbook describer."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant

from custom_components.entity_guard.const import (
    EVENT_ENFORCED,
    EVENT_LOOP_DETECTED,
    EVENT_SKIPPED,
    EVENT_SUPPRESSED,
    EVENT_VERIFIED,
)
from custom_components.entity_guard.logbook import async_describe_events


def _make_event(data: dict) -> MagicMock:
    e = MagicMock()
    e.data = data
    return e


def test_async_describe_events_registers(hass: HomeAssistant):
    registered = {}

    def _register(domain, event_type, describer):
        registered[event_type] = describer

    async_describe_events(hass, _register)
    assert EVENT_ENFORCED in registered
    assert EVENT_SKIPPED in registered
    assert EVENT_LOOP_DETECTED in registered
    assert EVENT_SUPPRESSED in registered
    assert EVENT_VERIFIED in registered


def test_describe_enforced():
    registered = {}

    def _register(domain, event_type, describer):
        registered[event_type] = describer

    async_describe_events(MagicMock(), _register)
    describer = registered[EVENT_ENFORCED]

    # target_entity_id present: used for display; entity_id in response is the rule sensor
    result = describer(
        _make_event(
            {
                "rule_name": "My Rule",
                "entity_id": "sensor.my_rule_status",
                "target_entity_id": "light.bedroom",
                "target": "off",
            }
        )
    )
    assert result["name"] == "My Rule"
    assert "off" in result["message"]
    assert "light.bedroom" in result["message"]
    assert result["entity_id"] == "sensor.my_rule_status"

    # fallback: no target_entity_id → message uses entity_id, response entity_id same
    result2 = describer(
        _make_event(
            {
                "rule_name": "My Rule",
                "entity_id": "light.bedroom",
                "target": "off",
            }
        )
    )
    assert result2["entity_id"] == "light.bedroom"


def test_describe_enforced_fallback_name():
    registered = {}
    async_describe_events(MagicMock(), lambda d, e, f: registered.update({e: f}))
    result = registered[EVENT_ENFORCED](
        _make_event({"entity_id": "light.x", "target": "on"})
    )
    assert result["name"] == "Entity Guard"


def test_describe_skipped():
    registered = {}
    async_describe_events(MagicMock(), lambda d, e, f: registered.update({e: f}))
    result = registered[EVENT_SKIPPED](
        _make_event(
            {"rule_name": "R", "entity_id": "switch.x", "reason": "no_service_mapping"}
        )
    )
    assert "no_service_mapping" in result["message"]


def test_describe_loop_detected():
    registered = {}
    async_describe_events(MagicMock(), lambda d, e, f: registered.update({e: f}))
    result = registered[EVENT_LOOP_DETECTED](
        _make_event({"rule_name": "R", "entity_id": "light.x"})
    )
    assert "auto-suppressed" in result["message"]


def test_describe_suppressed_with_until():
    registered = {}
    async_describe_events(MagicMock(), lambda d, e, f: registered.update({e: f}))
    result = registered[EVENT_SUPPRESSED](
        _make_event({"rule_name": "R", "suppressed_until": "2026-06-13T23:00:00"})
    )
    assert "2026-06-13T23:00:00" in result["message"]


def test_describe_suppressed_no_duration():
    registered = {}
    async_describe_events(MagicMock(), lambda d, e, f: registered.update({e: f}))
    result = registered[EVENT_SUPPRESSED](_make_event({"rule_name": "R"}))
    assert "suppressed" in result["message"]


def test_describe_verified_with_attempts():
    registered = {}
    async_describe_events(MagicMock(), lambda d, e, f: registered.update({e: f}))
    result = registered[EVENT_VERIFIED](
        _make_event(
            {
                "rule_name": "My Rule",
                "entity_id": "sensor.my_rule_status",
                "target_entity_id": "light.bedroom",
                "target": "off",
                "attempts": 3,
            }
        )
    )
    assert result["name"] == "My Rule"
    assert "verified" in result["message"]
    assert "light.bedroom" in result["message"]
    assert "off" in result["message"]
    assert "(3 attempts)" in result["message"]
    assert result["entity_id"] == "sensor.my_rule_status"


def test_describe_verified_no_attempts_suffix_and_fallbacks():
    registered = {}
    async_describe_events(MagicMock(), lambda d, e, f: registered.update({e: f}))
    # attempts absent → no suffix; entity_id fallback for target; name fallback.
    result = registered[EVENT_VERIFIED](_make_event({"entity_id": "light.x"}))
    assert result["name"] == "Entity Guard"
    assert "light.x" in result["message"]
    assert "attempts" not in result["message"]
