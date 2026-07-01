"""Tests for pure helpers in config_flow.py."""

from __future__ import annotations


from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.entity_guard.config_flow import (
    _attributes_for_entities,
    _build_summary,
    _coerce_delay,
    _current_state_hint,
    _debounce_selector,
    _delay_selector,
    _has_safety_target,
    _number_selector,
    _rate_selector,
    _rule_name_taken,
    _states_for_entities,
    _target_state_selector,
    _trigger_states_selector,
)
from custom_components.entity_guard.const import (
    CONF_ATTRIBUTE,
    CONF_DEBOUNCE_ENABLED,
    CONF_DEBOUNCE_SECONDS,
    CONF_DELAY_SECONDS,
    CONF_ENTRY_TYPE,
    CONF_FLAG_ENTITY,
    CONF_FLAG_MATCH_STATE,
    CONF_FLAGS,
    CONF_MAX_ENFORCEMENTS_PER_MINUTE,
    CONF_MODE,
    CONF_OPERATOR,
    CONF_RULE_NAME,
    CONF_SAFETY_ACKNOWLEDGED,
    CONF_TARGET_ENTITIES,
    CONF_TARGET_STATE,
    CONF_TARGET_VALUE,
    CONF_THRESHOLD,
    CONF_TRIGGER_STATES,
    DOMAIN,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_RULE,
    FALLBACK_STATE_OPTIONS,
    MAX_DELAY_SECONDS,
    MIN_DELAY_SECONDS,
    MODE_ATTRIBUTE,
    MODE_STATE,
)


# ---------------------------------------------------------------------------
# _has_safety_target
# ---------------------------------------------------------------------------


def test_has_safety_target_lock():
    assert _has_safety_target(["lock.front"]) is True


def test_has_safety_target_cover():
    assert _has_safety_target(["cover.garage"]) is True


def test_has_safety_target_climate():
    assert _has_safety_target(["climate.thermostat"]) is True


def test_has_safety_target_light_only():
    assert _has_safety_target(["light.bedroom", "switch.kitchen"]) is False


def test_has_safety_target_empty():
    assert _has_safety_target([]) is False


def test_has_safety_target_mixed():
    assert _has_safety_target(["light.bedroom", "lock.front"]) is True


# ---------------------------------------------------------------------------
# _rule_name_taken
# ---------------------------------------------------------------------------


def test_rule_name_taken_match():
    e = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_RULE, CONF_RULE_NAME: "MyRule"},
        title="MyRule",
    )
    assert _rule_name_taken([e], "myrule") is True


def test_rule_name_taken_no_match():
    e = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_RULE, CONF_RULE_NAME: "Other"},
        title="Other",
    )
    assert _rule_name_taken([e], "myrule") is False


def test_rule_name_taken_skips_hub():
    e = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_HUB, CONF_RULE_NAME: "Hub"},
        title="Hub",
    )
    assert _rule_name_taken([e], "hub") is False


def test_rule_name_taken_ignore_id():
    e = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_RULE, CONF_RULE_NAME: "MyRule"},
        title="MyRule",
        entry_id="abc",
    )
    assert _rule_name_taken([e], "myrule", ignore_entry_id="abc") is False


def test_rule_name_taken_uses_title_when_name_missing():
    e = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_RULE},
        title="FromTitle",
    )
    assert _rule_name_taken([e], "fromtitle") is True


# ---------------------------------------------------------------------------
# _coerce_delay
# ---------------------------------------------------------------------------


def test_coerce_delay_int():
    assert _coerce_delay(5) == 5


def test_coerce_delay_float_string():
    assert _coerce_delay("3.7") == 3


def test_coerce_delay_invalid_string():
    assert _coerce_delay("nope") is None


def test_coerce_delay_none():
    assert _coerce_delay(None) is None


def test_coerce_delay_below_min():
    assert _coerce_delay(MIN_DELAY_SECONDS - 1) is None


def test_coerce_delay_above_max():
    assert _coerce_delay(MAX_DELAY_SECONDS + 1) is None


def test_coerce_delay_at_bounds():
    assert _coerce_delay(MIN_DELAY_SECONDS) == MIN_DELAY_SECONDS
    assert _coerce_delay(MAX_DELAY_SECONDS) == MAX_DELAY_SECONDS


# ---------------------------------------------------------------------------
# _states_for_entities / _attributes_for_entities
# ---------------------------------------------------------------------------


def test_states_for_entities_known_domain():
    res = _states_for_entities(["light.bedroom"])
    assert "on" in res
    assert "off" in res


def test_states_for_entities_unknown_domain():
    res = _states_for_entities(["foo.bar"])
    assert res == list(FALLBACK_STATE_OPTIONS)


def test_states_for_entities_empty():
    assert _states_for_entities([]) == list(FALLBACK_STATE_OPTIONS)


def test_states_for_entities_dedup_across_domains():
    res = _states_for_entities(["light.a", "switch.b"])
    assert len(res) == len(set(res))


def test_attributes_for_entities_known_domain():
    res = _attributes_for_entities(["light.bedroom"])
    assert "brightness" in res


def test_attributes_for_entities_unknown_domain():
    from custom_components.entity_guard.const import SUPPORTED_ATTRIBUTES

    res = _attributes_for_entities(["foo.bar"])
    assert res == list(SUPPORTED_ATTRIBUTES)


def test_attributes_for_entities_empty():
    from custom_components.entity_guard.const import SUPPORTED_ATTRIBUTES

    assert _attributes_for_entities([]) == list(SUPPORTED_ATTRIBUTES)


# ---------------------------------------------------------------------------
# Selector factories
# ---------------------------------------------------------------------------


def test_delay_selector_returns_number():
    assert isinstance(_delay_selector(), selector.NumberSelector)


def test_debounce_selector_returns_number():
    assert isinstance(_debounce_selector(), selector.NumberSelector)


def test_rate_selector_returns_number():
    assert isinstance(_rate_selector(), selector.NumberSelector)


def test_number_selector_returns_number():
    assert isinstance(_number_selector(), selector.NumberSelector)


def test_trigger_states_selector_returns_select():
    s = _trigger_states_selector(["on", "off"])
    assert isinstance(s, selector.SelectSelector)


def test_trigger_states_selector_empty_falls_back():
    s = _trigger_states_selector([])
    assert isinstance(s, selector.SelectSelector)


def test_target_state_selector_returns_select():
    s = _target_state_selector(["on", "off"])
    assert isinstance(s, selector.SelectSelector)


def test_target_state_selector_empty_falls_back():
    s = _target_state_selector([])
    assert isinstance(s, selector.SelectSelector)


# ---------------------------------------------------------------------------
# _build_summary
# ---------------------------------------------------------------------------


def test_build_summary_state_mode_minimal():
    data = {
        CONF_RULE_NAME: "Test",
        CONF_TARGET_ENTITIES: ["light.bedroom"],
        CONF_MODE: MODE_STATE,
        CONF_TRIGGER_STATES: ["on"],
        CONF_TARGET_STATE: "off",
        CONF_DELAY_SECONDS: 0,
        CONF_FLAGS: [],
    }
    summary = _build_summary(data)
    assert "Test" in summary
    assert "light.bedroom" in summary
    assert "immediate" in summary
    assert "Conditions" in summary
    assert "Debounce" in summary


def test_build_summary_attribute_mode():
    data = {
        CONF_RULE_NAME: "Clamp",
        CONF_TARGET_ENTITIES: ["light.a", "light.b"],
        CONF_MODE: MODE_ATTRIBUTE,
        CONF_ATTRIBUTE: "brightness",
        CONF_OPERATOR: ">",
        CONF_THRESHOLD: 64,
        CONF_TARGET_VALUE: 64,
        CONF_DELAY_SECONDS: 5,
        CONF_FLAGS: [],
    }
    summary = _build_summary(data)
    assert "brightness" in summary
    assert "Targets:" in summary
    assert "5s" in summary


def test_build_summary_with_flags():
    data = {
        CONF_RULE_NAME: "WithFlags",
        CONF_TARGET_ENTITIES: ["light.bedroom"],
        CONF_MODE: MODE_STATE,
        CONF_TRIGGER_STATES: ["on"],
        CONF_TARGET_STATE: "off",
        CONF_DELAY_SECONDS: 0,
        CONF_FLAGS: [
            {CONF_FLAG_ENTITY: "input_boolean.x", CONF_FLAG_MATCH_STATE: "on"}
        ],
    }
    summary = _build_summary(data)
    assert "input_boolean.x" in summary
    assert "all must match" in summary


def test_build_summary_with_debounce_and_rate():
    data = {
        CONF_RULE_NAME: "Adv",
        CONF_TARGET_ENTITIES: ["light.bedroom"],
        CONF_MODE: MODE_STATE,
        CONF_TRIGGER_STATES: ["on"],
        CONF_TARGET_STATE: "off",
        CONF_DELAY_SECONDS: 0,
        CONF_FLAGS: [],
        CONF_DEBOUNCE_ENABLED: True,
        CONF_DEBOUNCE_SECONDS: 30,
        CONF_MAX_ENFORCEMENTS_PER_MINUTE: 5,
        CONF_SAFETY_ACKNOWLEDGED: True,
    }
    summary = _build_summary(data)
    assert "30s" in summary
    assert "5/min" in summary
    assert "acknowledged" in summary


def test_build_summary_rate_disabled():
    data = {
        CONF_RULE_NAME: "NoRate",
        CONF_TARGET_ENTITIES: ["light.bedroom"],
        CONF_MODE: MODE_STATE,
        CONF_TRIGGER_STATES: ["on"],
        CONF_TARGET_STATE: "off",
        CONF_DELAY_SECONDS: 0,
        CONF_FLAGS: [],
        CONF_MAX_ENFORCEMENTS_PER_MINUTE: 0,
    }
    summary = _build_summary(data)
    assert "Loop protection:** disabled" in summary


def test_build_summary_with_hass_friendly_name(hass: HomeAssistant):
    hass.states.async_set("light.bedroom", "on", {"friendly_name": "Bedroom Light"})
    data = {
        CONF_RULE_NAME: "Friendly",
        CONF_TARGET_ENTITIES: ["light.bedroom"],
        CONF_MODE: MODE_STATE,
        CONF_TRIGGER_STATES: ["on"],
        CONF_TARGET_STATE: "off",
        CONF_DELAY_SECONDS: 0,
        CONF_FLAGS: [],
    }
    summary = _build_summary(data, hass)
    assert "Bedroom Light" in summary


def test_build_summary_hass_no_state_keeps_id(hass: HomeAssistant):
    data = {
        CONF_RULE_NAME: "NoState",
        CONF_TARGET_ENTITIES: ["light.missing"],
        CONF_MODE: MODE_STATE,
        CONF_TRIGGER_STATES: ["on"],
        CONF_TARGET_STATE: "off",
        CONF_DELAY_SECONDS: 0,
        CONF_FLAGS: [],
    }
    summary = _build_summary(data, hass)
    assert "light.missing" in summary


def test_build_summary_unknown_mode_skips_rule_line():
    """_build_summary with unknown mode must not emit a Rule line (132->139 branch)."""
    data = {
        CONF_RULE_NAME: "Test",
        CONF_TARGET_ENTITIES: ["light.x"],
        CONF_MODE: "unknown_mode",
        CONF_DELAY_SECONDS: 0,
        CONF_FLAGS: [],
    }
    summary = _build_summary(data)
    assert "**Rule:**" not in summary


# ---------------------------------------------------------------------------
# _current_state_hint
# ---------------------------------------------------------------------------


def test_current_state_hint_empty_entity():
    assert _current_state_hint(None, None) == ""


def test_current_state_hint_no_hass():
    assert _current_state_hint(None, "light.bedroom") == ""


def test_current_state_hint_resolves(hass: HomeAssistant):
    hass.states.async_set("input_boolean.guest", "off")
    out = _current_state_hint(hass, "input_boolean.guest")
    assert "input_boolean.guest" in out
    assert "off" in out


def test_current_state_hint_missing_entity(hass: HomeAssistant):
    out = _current_state_hint(hass, "input_boolean.ghost")
    assert "input_boolean.ghost" in out
    assert "not found" in out
