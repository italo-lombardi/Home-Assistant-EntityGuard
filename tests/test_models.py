"""Tests for Entity Guard data models."""

from __future__ import annotations

import asyncio

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.entity_guard.const import (
    DOMAIN,
    ENTRY_TYPE_RULE,
    MODE_ATTRIBUTE,
    MODE_STATE,
)
from custom_components.entity_guard.models import (
    Flag,
    RuleRuntimeState,
    _to_float_or_none,
    _to_int_or_default,
    parse_rule_config,
)


# ---------------------------------------------------------------------------
# Flag
# ---------------------------------------------------------------------------


def test_flag_to_dict():
    f = Flag(entity="light.test", match_state="on")
    d = f.to_dict()
    assert d["entity"] == "light.test"
    assert d["match_state"] == "on"


def test_flag_from_dict():
    f = Flag.from_dict({"entity": "switch.x", "match_state": "off"})
    assert f.entity == "switch.x"
    assert f.match_state == "off"


def test_flag_roundtrip():
    orig = Flag(entity="binary_sensor.door", match_state="on")
    assert Flag.from_dict(orig.to_dict()) == orig


# ---------------------------------------------------------------------------
# RuleRuntimeState
# ---------------------------------------------------------------------------


def test_runtime_state_defaults():
    s = RuleRuntimeState()
    assert s.enabled is True
    assert s.enforcement_count_today == 0
    assert s.enforcement_count_total == 0
    assert s.last_enforced is None
    assert s.suppressed_until is None
    assert s.suppression_reason is None
    assert s.cooldowns == {}
    assert s.rate_limit_window == []
    assert isinstance(s.reentrance_lock, asyncio.Lock)


# ---------------------------------------------------------------------------
# parse_rule_config
# ---------------------------------------------------------------------------


def _make_entry(**overrides):
    data = {
        "entry_type": ENTRY_TYPE_RULE,
        "rule_id": "rule-uuid",
        "rule_name": "My Rule",
        "target_entities": ["light.bedroom"],
        "mode": MODE_STATE,
        "trigger_states": ["on"],
        "target_state": "off",
        "delay_seconds": 0,
        "flags": [],
        "debounce_enabled": False,
        "debounce_seconds": 60,
        "max_enforcements_per_minute": 10,
        "safety_acknowledged": False,
    }
    data.update(overrides)
    return MockConfigEntry(domain=DOMAIN, data=data, title="My Rule")


def test_parse_rule_config_basic():
    entry = _make_entry()
    config = parse_rule_config(entry)
    assert config.name == "My Rule"
    assert config.unique_id == "rule-uuid"
    assert config.target_entities == ["light.bedroom"]
    assert config.mode == MODE_STATE
    assert config.trigger_states == ["on"]
    assert config.target_state == "off"
    assert config.delay_seconds == 0
    assert config.flags == []
    assert config.debounce_enabled is False
    assert config.debounce_seconds == 60
    assert config.max_enforcements_per_minute == 10
    assert config.safety_acknowledged is False


def test_parse_rule_config_with_flags():
    entry = _make_entry(flags=[{"entity": "input_boolean.night", "match_state": "on"}])
    config = parse_rule_config(entry)
    assert len(config.flags) == 1
    assert config.flags[0].entity == "input_boolean.night"
    assert config.flags[0].match_state == "on"


def test_parse_rule_config_attribute_mode():
    entry = _make_entry(
        mode=MODE_ATTRIBUTE,
        attribute="brightness",
        operator="le",
        threshold=50.0,
        target_value=0.0,
    )
    config = parse_rule_config(entry)
    assert config.mode == MODE_ATTRIBUTE
    assert config.attribute == "brightness"
    assert config.operator == "le"
    assert config.threshold == 50.0
    assert config.target_value == 0.0


def test_parse_rule_config_options_override_data():
    data = {
        "entry_type": ENTRY_TYPE_RULE,
        "rule_id": "uid",
        "rule_name": "Old Name",
        "target_entities": ["light.x"],
        "mode": MODE_STATE,
        "trigger_states": ["on"],
        "target_state": "off",
        "delay_seconds": 0,
        "flags": [],
        "debounce_enabled": False,
        "debounce_seconds": 60,
        "max_enforcements_per_minute": 10,
        "safety_acknowledged": False,
    }
    options = {"rule_name": "New Name", "delay_seconds": 30}
    entry = MockConfigEntry(domain=DOMAIN, data=data, options=options, title="Old Name")
    config = parse_rule_config(entry)
    assert config.name == "New Name"
    assert config.delay_seconds == 30


def test_parse_rule_config_missing_rule_id_falls_back_to_entry_id():
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "entry_type": ENTRY_TYPE_RULE,
            "rule_name": "No ID",
            "target_entities": [],
            "mode": MODE_STATE,
            "trigger_states": [],
            "target_state": "off",
            "delay_seconds": 0,
            "flags": [],
            "debounce_enabled": False,
            "debounce_seconds": 60,
            "max_enforcements_per_minute": 5,
            "safety_acknowledged": False,
        },
        title="No ID",
    )
    config = parse_rule_config(entry)
    assert config.unique_id == entry.entry_id


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_to_float_or_none_valid():
    assert _to_float_or_none(3.14) == pytest.approx(3.14)
    assert _to_float_or_none("2.5") == pytest.approx(2.5)


def test_to_float_or_none_invalid():
    assert _to_float_or_none(None) is None
    assert _to_float_or_none("bad") is None
    assert _to_float_or_none([]) is None


def test_to_int_or_default():
    assert _to_int_or_default(5, 99) == 5
    assert _to_int_or_default("7", 99) == 7
    assert _to_int_or_default(None, 99) == 99
    assert _to_int_or_default("bad", 99) == 99


def test_flag_from_dict_missing_key_raises_value_error():
    with pytest.raises(ValueError, match="missing required key"):
        Flag.from_dict({"entity": "light.x"})  # match_state missing


def test_parse_rule_config_non_list_flags_treated_as_empty():
    entry = MockConfigEntry(
        domain="entity_guard",
        data={
            "entry_type": "rule",
            "rule_id": "uid",
            "rule_name": "R",
            "target_entities": ["light.x"],
            "mode": "state",
            "trigger_states": ["on"],
            "target_state": "off",
            "flags": "not-a-list",
        },
        title="R",
    )
    config = parse_rule_config(entry)
    assert config.flags == []


def test_parse_rule_config_skips_corrupt_flag():
    entry = MockConfigEntry(
        domain="entity_guard",
        data={
            "entry_type": "rule",
            "rule_id": "uid",
            "rule_name": "R",
            "target_entities": ["light.x"],
            "mode": "state",
            "trigger_states": ["on"],
            "target_state": "off",
            "flags": [{"entity": "light.x"}],  # missing match_state
        },
        title="R",
    )
    with pytest.raises(ValueError, match="Corrupt flag entry"):
        parse_rule_config(entry)
