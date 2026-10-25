"""Per-direction delay for dynamic_state: RuleConfig.effective_delay + parse.

Feature: dynamic_state may delay enforcement differently depending on whether the
target is being driven ON vs OFF. delay_on_seconds / delay_off_seconds override the
shared delay_seconds per direction; an unset override (None) falls back to
delay_seconds, and an explicit 0 is honoured as immediate. The override is gated to
dynamic_state only — the engine passes _follow_desired_on(), which also yields a real
value for dynamic_attribute, so the mode check keeps the overrides dynamic_state-only.
"""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.entity_guard.const import (
    CONF_DELAY_OFF_SECONDS,
    CONF_DELAY_ON_SECONDS,
    CONF_DELAY_SECONDS,
    CONF_MODE,
    CONF_RULE_NAME,
    CONF_TARGET_ENTITIES,
    DOMAIN,
    ENTRY_TYPE_RULE,
    MAX_DELAY_SECONDS,
    MODE_DYNAMIC_ATTRIBUTE,
    MODE_DYNAMIC_STATE,
    MODE_FIXED_STATE,
)
from custom_components.entity_guard.models import RuleConfig, parse_rule_config


def _cfg(mode: str, **overrides) -> RuleConfig:
    """Build a RuleConfig with the delay fields under test; everything else minimal."""
    defaults = {
        "unique_id": "uid",
        "name": "Rule",
        "target_entities": ["light.a"],
        "mode": mode,
        "trigger_states": ["on"],
        "target_state": "off",
        "delay_seconds": 30,
        "attribute": None,
        "operator": None,
        "threshold": None,
        "target_value": None,
        "flags": [],
        "debounce_enabled": False,
        "debounce_seconds": 60,
        "max_enforcements_per_minute": 10,
        "safety_acknowledged": False,
        "delay_on_seconds": None,
        "delay_off_seconds": None,
    }
    defaults.update(overrides)
    return RuleConfig(**defaults)


# --------------------------------------------------------------------------- helper


def test_effective_delay_dynamic_state_uses_direction():
    c = _cfg(MODE_DYNAMIC_STATE, delay_on_seconds=10, delay_off_seconds=90)
    assert c.effective_delay(True) == 10  # driving ON
    assert c.effective_delay(False) == 90  # driving OFF


def test_effective_delay_unset_direction_falls_back_to_shared():
    # delay_on unset → shared 30; delay_off set → 5.
    c = _cfg(MODE_DYNAMIC_STATE, delay_on_seconds=None, delay_off_seconds=5)
    assert c.effective_delay(True) == 30
    assert c.effective_delay(False) == 5


def test_effective_delay_explicit_zero_is_immediate_not_fallback():
    # 0 must be honoured as immediate, NOT treated as unset→fallback (is-not-None).
    c = _cfg(MODE_DYNAMIC_STATE, delay_on_seconds=0, delay_off_seconds=0)
    assert c.effective_delay(True) == 0
    assert c.effective_delay(False) == 0


def test_effective_delay_desired_none_returns_shared():
    # Source offline / direction unknown → shared delay, pre-feature behaviour.
    c = _cfg(MODE_DYNAMIC_STATE, delay_on_seconds=10, delay_off_seconds=90)
    assert c.effective_delay(None) == 30


def test_effective_delay_non_dynamic_state_ignores_overrides():
    # Mode gate: fixed_state (and any non-dynamic_state) never reads the overrides,
    # even if a desired direction is passed and overrides are set.
    c = _cfg(MODE_FIXED_STATE, delay_on_seconds=10, delay_off_seconds=90)
    assert c.effective_delay(True) == 30
    assert c.effective_delay(False) == 30
    assert c.effective_delay(None) == 30


def test_effective_delay_dynamic_attribute_ignores_overrides():
    # dynamic_attribute's private _follow_desired_on() DOES yield a bool, so the
    # mode check (not the None check) is what keeps the overrides dynamic_state-only.
    c = _cfg(MODE_DYNAMIC_ATTRIBUTE, delay_on_seconds=10, delay_off_seconds=90)
    assert c.effective_delay(True) == 30
    assert c.effective_delay(False) == 30


# --------------------------------------------------------------------------- parse


def _entry(hass, data) -> MockConfigEntry:
    base = {
        CONF_RULE_NAME: "R",
        "entry_type": ENTRY_TYPE_RULE,
        CONF_MODE: MODE_DYNAMIC_STATE,
        CONF_TARGET_ENTITIES: ["light.a"],
    }
    base.update(data)
    e = MockConfigEntry(domain=DOMAIN, data=base)
    e.add_to_hass(hass)
    return e


async def test_parse_directional_delays_present(hass):
    e = _entry(
        hass,
        {CONF_DELAY_ON_SECONDS: 12, CONF_DELAY_OFF_SECONDS: 88, CONF_DELAY_SECONDS: 12},
    )
    c = parse_rule_config(e)
    assert c.delay_on_seconds == 12
    assert c.delay_off_seconds == 88


async def test_parse_directional_delays_absent_are_none(hass):
    # No directional keys (e.g. a fixed rule, or an older dynamic rule) → None,
    # so effective_delay falls back to delay_seconds. No migration needed.
    e = _entry(hass, {CONF_DELAY_SECONDS: 30})
    c = parse_rule_config(e)
    assert c.delay_on_seconds is None
    assert c.delay_off_seconds is None


async def test_parse_directional_delay_zero_preserved(hass):
    # Explicit 0 must survive parse as 0 (not coerced to None/fallback).
    e = _entry(hass, {CONF_DELAY_ON_SECONDS: 0, CONF_DELAY_OFF_SECONDS: 0})
    c = parse_rule_config(e)
    assert c.delay_on_seconds == 0
    assert c.delay_off_seconds == 0


async def test_parse_directional_delay_clamped_to_max(hass):
    e = _entry(hass, {CONF_DELAY_ON_SECONDS: MAX_DELAY_SECONDS + 999})
    c = parse_rule_config(e)
    assert c.delay_on_seconds == MAX_DELAY_SECONDS
