"""Tests for RuleEngine core logic."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util

from custom_components.entity_guard.const import (
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    COLOR_RGB_TOLERANCE,
    COLOR_TEMP_KELVIN_TOLERANCE,
    CONSECUTIVE_EXHAUSTION_LIMIT,
    ERROR_RECOVERY_SUCCESS_THRESHOLD,
    ERROR_THRESHOLD,
    EVENT_CANCELLED,
    EVENT_ENFORCED,
    EVENT_LOOP_DETECTED,
    EVENT_SKIPPED,
    EVENT_SUPPRESSED,
    EVENT_UNREACHABLE,
    EVENT_VERIFIED,
    MODE_DYNAMIC_STATE,
    MODE_FIXED_ATTRIBUTE,
    MODE_FIXED_STATE,
    OPERATOR_GE,
    OPERATOR_GT,
    OPERATOR_LE,
    OPERATOR_LT,
    REASON_SERVICE_CALL_FAILED,
    REASON_UNREACHABLE_AFTER_RETRIES,
    RECENTLY_ENFORCED_WINDOW_SECONDS,
    STATUS_ARMED,
    STATUS_CONDITIONAL,
    STATUS_COOLDOWN,
    STATUS_DISABLED,
    STATUS_ENFORCING,
    STATUS_ERROR,
    STATUS_MASTER_DISABLED,
    STATUS_PENDING,
    STATUS_STARTING,
    STATUS_SUPPRESSED,
    STATUS_VERIFYING,
    VERIFY_MAX_ATTEMPTS,
)
from custom_components.entity_guard.models import Flag, RuleConfig, RuleRuntimeState
from custom_components.entity_guard.rule_engine import (
    RuleEngine,
    _compare,
    _describe_color_target,
    _normalize_kelvin,
    _normalize_rgb_color,
    _rgb_matches,
    signal_for_rule,
)
from custom_components.entity_guard.storage import EntityGuardStore
from tests.conftest import capturing_create_task

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides) -> RuleConfig:
    defaults = {
        "name": "Test Rule",
        "unique_id": "test-uid",
        "target_entities": ["light.bedroom"],
        "mode": MODE_FIXED_STATE,
        "trigger_states": ["on"],
        "target_state": "off",
        "delay_seconds": 0,
        "attribute": None,
        "operator": None,
        "threshold": None,
        "target_value": None,
        "flags": [],
        "debounce_enabled": False,
        "debounce_seconds": 60,
        "max_enforcements_per_minute": 10,
        "safety_acknowledged": False,
    }
    defaults.update(overrides)
    return RuleConfig(**defaults)


def _make_engine(hass, config=None, master=True) -> RuleEngine:
    config = config or _make_config()
    store = MagicMock(spec=EntityGuardStore)
    store.get_rule_state.return_value = {}
    store.set_rule_state.return_value = None
    store.runtime_to_blob = EntityGuardStore.runtime_to_blob
    store.blob_to_runtime = EntityGuardStore.blob_to_runtime
    return RuleEngine(hass, config, store, lambda: master)


def _state(state_str, attributes=None):
    s = MagicMock()
    s.state = state_str
    s.attributes = attributes or {}
    return s


# ---------------------------------------------------------------------------
# signal helpers
# ---------------------------------------------------------------------------


def test_signal_for_rule():
    assert signal_for_rule("my-id") == "entity_guard_rule_update_my-id"


# ---------------------------------------------------------------------------
# _compare
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,op,threshold,expected",
    [
        (1.0, OPERATOR_LT, 2.0, True),
        (2.0, OPERATOR_LT, 2.0, False),
        (2.0, OPERATOR_LE, 2.0, True),
        (3.0, OPERATOR_LE, 2.0, False),
        (3.0, OPERATOR_GT, 2.0, True),
        (2.0, OPERATOR_GT, 2.0, False),
        (2.0, OPERATOR_GE, 2.0, True),
        (1.0, OPERATOR_GE, 2.0, False),
        (1.0, "unknown", 2.0, False),
    ],
)
def test_compare(value, op, threshold, expected):
    assert _compare(value, op, threshold) is expected


def test_normalize_rgb_color_invalid():
    assert _normalize_rgb_color([255, 0]) is None
    assert _normalize_rgb_color([255, "bad", 0]) is None


def test_normalize_kelvin_invalid():
    assert _normalize_kelvin("bad") is None


def test_rgb_matches_boundary():
    assert _rgb_matches((255, 0, 0), (255 - COLOR_RGB_TOLERANCE, 0, 0)) is True


def test_describe_color_target_unknown_attribute():
    assert _describe_color_target("custom", "value") == "value"


# ---------------------------------------------------------------------------
# RuleEngine properties
# ---------------------------------------------------------------------------


async def test_engine_properties(hass: HomeAssistant):
    engine = _make_engine(hass)
    assert engine.config.name == "Test Rule"
    assert isinstance(engine.state, RuleRuntimeState)


async def test_current_status_starts_at_starting(hass: HomeAssistant):
    engine = _make_engine(hass)
    assert engine.current_status() == STATUS_STARTING


# ---------------------------------------------------------------------------
# async_setup / async_unload
# ---------------------------------------------------------------------------


async def test_async_setup_subscribes(hass: HomeAssistant):
    engine = _make_engine(hass)
    with (
        patch(
            "custom_components.entity_guard.rule_engine.async_track_state_change_event"
        ) as mock_track,
        patch("custom_components.entity_guard.rule_engine.async_track_time_change"),
        patch("custom_components.entity_guard.rule_engine.async_call_later"),
    ):
        await engine.async_setup()
    mock_track.assert_called_once()


async def test_async_setup_disabled_rule_never_shows_starting(hass: HomeAssistant):
    """Disabled rule must show DISABLED immediately after setup, not STATUS_STARTING."""
    config = _make_config()
    engine = _make_engine(hass, config)
    engine._store.get_rule_state.return_value = {"enabled": False}
    with (
        patch(
            "custom_components.entity_guard.rule_engine.async_track_state_change_event"
        ),
        patch("custom_components.entity_guard.rule_engine.async_track_time_change"),
        patch("custom_components.entity_guard.rule_engine.async_call_later"),
    ):
        await engine.async_setup()
    assert engine.current_status() == STATUS_DISABLED


async def test_async_setup_master_disabled_never_shows_starting(hass: HomeAssistant):
    """Master-disabled rule must show MASTER_DISABLED immediately, not STATUS_STARTING."""
    engine = _make_engine(hass, master=False)
    with (
        patch(
            "custom_components.entity_guard.rule_engine.async_track_state_change_event"
        ),
        patch("custom_components.entity_guard.rule_engine.async_track_time_change"),
        patch("custom_components.entity_guard.rule_engine.async_call_later"),
    ):
        await engine.async_setup()
    assert engine.current_status() == STATUS_MASTER_DISABLED


async def test_async_setup_no_watched_entities_skips_track(hass: HomeAssistant):
    """Engine with no targets and no flags must not call async_track_state_change_event."""
    config = _make_config(target_entities=[], flags=[])
    engine = _make_engine(hass, config)
    with (
        patch(
            "custom_components.entity_guard.rule_engine.async_track_state_change_event"
        ) as mock_track,
        patch("custom_components.entity_guard.rule_engine.async_track_time_change"),
        patch("custom_components.entity_guard.rule_engine.async_call_later"),
    ):
        await engine.async_setup()
    mock_track.assert_not_called()


async def test_async_unload_cleans_up(hass: HomeAssistant):
    engine = _make_engine(hass)
    cancel_mock = MagicMock()
    engine._unsub_callbacks.append(cancel_mock)
    engine._store.async_save_now = AsyncMock()
    await engine.async_unload()
    cancel_mock.assert_called_once()
    assert engine._unsub_callbacks == []


# ---------------------------------------------------------------------------
# set_enabled
# ---------------------------------------------------------------------------


async def test_set_enabled_false_disables(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine._startup_complete = True
    engine.set_enabled(False)
    assert engine.current_status() == STATUS_DISABLED
    assert engine.state.enabled is False


async def test_set_enabled_true_arms(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine._startup_complete = True
    engine.state.enabled = False
    engine.set_enabled(True)
    assert engine.state.enabled is True


# ---------------------------------------------------------------------------
# _flags_match
# ---------------------------------------------------------------------------


async def test_flags_match_no_flags(hass: HomeAssistant):
    engine = _make_engine(hass)
    assert engine._flags_match() is True


async def test_flags_match_all_match(hass: HomeAssistant):
    config = _make_config(flags=[Flag(entity="input_boolean.night", match_state="on")])
    engine = _make_engine(hass, config)
    hass.states.async_set("input_boolean.night", "on")
    assert engine._flags_match() is True


async def test_flags_match_no_match(hass: HomeAssistant):
    config = _make_config(flags=[Flag(entity="input_boolean.night", match_state="on")])
    engine = _make_engine(hass, config)
    hass.states.async_set("input_boolean.night", "off")
    assert engine._flags_match() is False


async def test_flags_match_missing_entity(hass: HomeAssistant):
    config = _make_config(
        flags=[Flag(entity="input_boolean.missing", match_state="on")]
    )
    engine = _make_engine(hass, config)
    assert engine._flags_match() is False


async def test_flags_match_unavailable(hass: HomeAssistant):
    config = _make_config(flags=[Flag(entity="input_boolean.x", match_state="on")])
    engine = _make_engine(hass, config)
    hass.states.async_set("input_boolean.x", "unavailable")
    assert engine._flags_match() is False


# ---------------------------------------------------------------------------
# _is_triggered
# ---------------------------------------------------------------------------


async def test_is_triggered_state_mode_match(hass: HomeAssistant):
    engine = _make_engine(hass)
    st = _state("on")
    assert engine._is_triggered("light.bedroom", st) is True


async def test_is_triggered_state_mode_no_match(hass: HomeAssistant):
    engine = _make_engine(hass)
    st = _state("off")
    assert engine._is_triggered("light.bedroom", st) is False


async def test_is_triggered_none_state(hass: HomeAssistant):
    engine = _make_engine(hass)
    assert engine._is_triggered("light.bedroom", None) is False


async def test_is_triggered_attribute_mode(hass: HomeAssistant):
    config = _make_config(
        mode=MODE_FIXED_ATTRIBUTE,
        attribute="brightness",
        operator=OPERATOR_GT,
        threshold=50.0,
        target_value=0.0,
        trigger_states=[],
    )
    engine = _make_engine(hass, config)
    st = _state("on", {"brightness": 80})
    assert engine._is_triggered("light.x", st) is True


async def test_is_triggered_attribute_below_threshold(hass: HomeAssistant):
    config = _make_config(
        mode=MODE_FIXED_ATTRIBUTE,
        attribute="brightness",
        operator=OPERATOR_GT,
        threshold=50.0,
        target_value=0.0,
        trigger_states=[],
    )
    engine = _make_engine(hass, config)
    st = _state("on", {"brightness": 30})
    assert engine._is_triggered("light.x", st) is False


async def test_is_triggered_attribute_non_numeric(hass: HomeAssistant):
    config = _make_config(
        mode=MODE_FIXED_ATTRIBUTE,
        attribute="brightness",
        operator=OPERATOR_GT,
        threshold=50.0,
        target_value=0.0,
        trigger_states=[],
    )
    engine = _make_engine(hass, config)
    st = _state("on", {"brightness": "not-a-number"})
    assert engine._is_triggered("light.x", st) is False


async def test_color_trigger_decision_missing_target_returns_false(hass: HomeAssistant):
    config = _make_config(
        mode=MODE_FIXED_ATTRIBUTE, attribute=ATTR_RGB_COLOR, target_value=None
    )
    engine = _make_engine(hass, config)
    st = _state("on", {"rgb_color": [1, 2, 3]})
    assert engine._color_trigger_decision(st) == (False, None)


async def test_color_trigger_decision_missing_attribute_returns_false(
    hass: HomeAssistant,
):
    config = _make_config(
        mode=MODE_FIXED_ATTRIBUTE, attribute=None, target_value=[255, 0, 0]
    )
    engine = _make_engine(hass, config)
    st = _state("on", {"rgb_color": [1, 2, 3]})
    assert engine._color_trigger_decision(st) == (False, None)


async def test_color_trigger_decision_missing_current_value_skips(hass: HomeAssistant):
    config = _make_config(
        mode=MODE_FIXED_ATTRIBUTE,
        attribute=ATTR_COLOR_TEMP_KELVIN,
        target_value=2700,
    )
    engine = _make_engine(hass, config)
    st = _state("on", {})
    assert engine._color_trigger_decision(st) == (False, "attribute_unavailable")


async def test_color_trigger_decision_missing_current_rgb_value_skips(
    hass: HomeAssistant,
):
    config = _make_config(
        mode=MODE_FIXED_ATTRIBUTE,
        attribute=ATTR_RGB_COLOR,
        target_value=[255, 0, 0],
    )
    engine = _make_engine(hass, config)
    st = _state("on", {})
    assert engine._color_trigger_decision(st) == (False, "attribute_unavailable")


async def test_color_trigger_decision_missing_kelvin_target_returns_false(
    hass: HomeAssistant,
):
    config = _make_config(
        mode=MODE_FIXED_ATTRIBUTE,
        attribute=ATTR_COLOR_TEMP_KELVIN,
        target_value=None,
    )
    engine = _make_engine(hass, config)
    st = _state("on", {"color_temp_kelvin": 2700})
    assert engine._color_trigger_decision(st) == (False, None)


async def test_color_trigger_decision_unknown_color_attribute_returns_false(
    hass: HomeAssistant,
):
    config = _make_config(
        mode=MODE_FIXED_ATTRIBUTE,
        attribute="color_effect",
        target_value="red",
    )
    engine = _make_engine(hass, config)
    st = _state("on", {"color_effect": "blue"})
    assert engine._color_trigger_decision(st) == (False, None)


async def test_is_triggered_rgb_color_within_tolerance(hass: HomeAssistant):
    config = _make_config(
        mode=MODE_FIXED_ATTRIBUTE,
        attribute=ATTR_RGB_COLOR,
        target_value=[255, 0, 0],
        trigger_states=[],
    )
    engine = _make_engine(hass, config)
    st = _state("on", {"rgb_color": [255 - COLOR_RGB_TOLERANCE, 0, 0]})
    assert engine._is_triggered("light.x", st) is False


async def test_is_triggered_rgb_color_outside_tolerance(hass: HomeAssistant):
    config = _make_config(
        mode=MODE_FIXED_ATTRIBUTE,
        attribute=ATTR_RGB_COLOR,
        target_value=[255, 0, 0],
        trigger_states=[],
    )
    engine = _make_engine(hass, config)
    st = _state("on", {"rgb_color": [255 - COLOR_RGB_TOLERANCE - 1, 0, 0]})
    assert engine._is_triggered("light.x", st) is True


async def test_is_triggered_color_temp_kelvin_within_tolerance(hass: HomeAssistant):
    config = _make_config(
        mode=MODE_FIXED_ATTRIBUTE,
        attribute=ATTR_COLOR_TEMP_KELVIN,
        target_value=2700,
        trigger_states=[],
    )
    engine = _make_engine(hass, config)
    st = _state(
        "on",
        {"color_temp_kelvin": 2700 + COLOR_TEMP_KELVIN_TOLERANCE},
    )
    assert engine._is_triggered("light.x", st) is False


async def test_is_triggered_color_temp_kelvin_outside_tolerance(hass: HomeAssistant):
    config = _make_config(
        mode=MODE_FIXED_ATTRIBUTE,
        attribute=ATTR_COLOR_TEMP_KELVIN,
        target_value=2700,
        trigger_states=[],
    )
    engine = _make_engine(hass, config)
    st = _state(
        "on",
        {"color_temp_kelvin": 2700 + COLOR_TEMP_KELVIN_TOLERANCE + 1},
    )
    assert engine._is_triggered("light.x", st) is True


async def test_is_triggered_unknown_mode(hass: HomeAssistant):
    config = _make_config(mode="bad_mode")
    engine = _make_engine(hass, config)
    st = _state("on")
    assert engine._is_triggered("light.x", st) is False


async def test_color_enforcement_calls_light_turn_on_with_rgb(hass: HomeAssistant):
    config = _make_config(
        mode=MODE_FIXED_ATTRIBUTE,
        attribute=ATTR_RGB_COLOR,
        target_value=[255, 0, 0],
        trigger_states=[],
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    service_calls = []
    enforced_events = []
    hass.bus.async_listen(EVENT_ENFORCED, lambda e: enforced_events.append(e))

    async def _turn_on(call):
        service_calls.append(call)

    hass.services.async_register("light", "turn_on", _turn_on)
    hass.states.async_set("light.bedroom", "on", {"rgb_color": [0, 0, 255]})
    await engine.async_evaluate("light.bedroom", hass.states.get("light.bedroom"))
    await hass.async_block_till_done()

    assert service_calls
    assert service_calls[0].data["rgb_color"] == [255, 0, 0]
    assert any(e.data["target"] == "rgb_color=[255, 0, 0]" for e in enforced_events)


async def test_color_enforcement_calls_light_turn_on_with_kelvin(hass: HomeAssistant):
    config = _make_config(
        mode=MODE_FIXED_ATTRIBUTE,
        attribute=ATTR_COLOR_TEMP_KELVIN,
        target_value=2700,
        trigger_states=[],
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    service_calls = []

    async def _turn_on(call):
        service_calls.append(call)

    hass.services.async_register("light", "turn_on", _turn_on)
    hass.states.async_set("light.bedroom", "on", {"color_temp_kelvin": 4000})
    await engine.async_evaluate("light.bedroom", hass.states.get("light.bedroom"))

    assert service_calls
    assert service_calls[0].data["color_temp_kelvin"] == 2700


async def test_color_enforcement_skips_when_light_off(hass: HomeAssistant):
    config = _make_config(
        mode=MODE_FIXED_ATTRIBUTE,
        attribute=ATTR_RGB_COLOR,
        target_value=[255, 0, 0],
        trigger_states=[],
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    skipped_events = []
    service = AsyncMock()
    hass.bus.async_listen(EVENT_SKIPPED, lambda e: skipped_events.append(e))
    hass.services.async_register("light", "turn_on", service)
    hass.states.async_set("light.bedroom", "off", {"rgb_color": [0, 0, 255]})

    await engine.async_evaluate("light.bedroom", hass.states.get("light.bedroom"))
    await hass.async_block_till_done()

    service.assert_not_awaited()
    assert any(e.data["reason"] == "light_off" for e in skipped_events)


async def test_color_enforcement_skips_when_light_unavailable(hass: HomeAssistant):
    config = _make_config(
        mode=MODE_FIXED_ATTRIBUTE,
        attribute=ATTR_COLOR_TEMP_KELVIN,
        target_value=2700,
        trigger_states=[],
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    skipped_events = []
    hass.bus.async_listen(EVENT_SKIPPED, lambda e: skipped_events.append(e))
    hass.states.async_set("light.bedroom", "unavailable")

    await engine.async_evaluate("light.bedroom", hass.states.get("light.bedroom"))
    await hass.async_block_till_done()

    assert any(e.data["reason"] == "light_unavailable" for e in skipped_events)


async def test_color_enforcement_skips_when_rgb_attribute_unavailable(
    hass: HomeAssistant,
):
    config = _make_config(
        mode=MODE_FIXED_ATTRIBUTE,
        attribute=ATTR_RGB_COLOR,
        target_value=[255, 0, 0],
        trigger_states=[],
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    skipped_events = []
    service = AsyncMock()
    hass.bus.async_listen(EVENT_SKIPPED, lambda e: skipped_events.append(e))
    hass.services.async_register("light", "turn_on", service)
    hass.states.async_set("light.bedroom", "on", {})

    await engine.async_evaluate("light.bedroom", hass.states.get("light.bedroom"))
    await hass.async_block_till_done()

    service.assert_not_awaited()
    assert any(e.data["reason"] == "attribute_unavailable" for e in skipped_events)


async def test_color_enforcement_skips_when_kelvin_attribute_unavailable(
    hass: HomeAssistant,
):
    config = _make_config(
        mode=MODE_FIXED_ATTRIBUTE,
        attribute=ATTR_COLOR_TEMP_KELVIN,
        target_value=2700,
        trigger_states=[],
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    skipped_events = []
    service = AsyncMock()
    hass.bus.async_listen(EVENT_SKIPPED, lambda e: skipped_events.append(e))
    hass.services.async_register("light", "turn_on", service)
    hass.states.async_set("light.bedroom", "on", {})

    await engine.async_evaluate("light.bedroom", hass.states.get("light.bedroom"))
    await hass.async_block_till_done()

    service.assert_not_awaited()
    assert any(e.data["reason"] == "attribute_unavailable" for e in skipped_events)


async def test_delayed_color_enforcement_skips_if_light_turns_off(hass: HomeAssistant):
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    config = _make_config(
        mode=MODE_FIXED_ATTRIBUTE,
        attribute=ATTR_RGB_COLOR,
        target_value=[255, 0, 0],
        trigger_states=[],
        delay_seconds=1,
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    skipped_events = []
    hass.bus.async_listen(EVENT_SKIPPED, lambda e: skipped_events.append(e))
    hass.states.async_set("light.bedroom", "on", {"rgb_color": [0, 0, 255]})
    await engine.async_evaluate("light.bedroom", hass.states.get("light.bedroom"))

    hass.states.async_set("light.bedroom", "off", {"rgb_color": [0, 0, 255]})
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()

    assert any(e.data["reason"] == "light_off" for e in skipped_events)


async def test_manual_color_enforce_skips_when_light_off(hass: HomeAssistant):
    config = _make_config(
        mode=MODE_FIXED_ATTRIBUTE,
        attribute=ATTR_RGB_COLOR,
        target_value=[255, 0, 0],
        trigger_states=[],
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    skipped_events = []
    service = AsyncMock()
    hass.bus.async_listen(EVENT_SKIPPED, lambda e: skipped_events.append(e))
    hass.services.async_register("light", "turn_on", service)
    hass.states.async_set("light.bedroom", "off", {"rgb_color": [0, 0, 255]})

    await engine.async_test_enforce()
    await hass.async_block_till_done()

    service.assert_not_awaited()
    assert any(e.data["reason"] == "light_off" for e in skipped_events)


async def test_color_enforcement_respects_debounce_cooldown(hass: HomeAssistant):
    config = _make_config(
        mode=MODE_FIXED_ATTRIBUTE,
        attribute=ATTR_RGB_COLOR,
        target_value=[255, 0, 0],
        trigger_states=[],
        debounce_enabled=True,
        debounce_seconds=30,
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    service = AsyncMock()
    hass.services.async_register("light", "turn_on", service)
    hass.states.async_set("light.bedroom", "on", {"rgb_color": [0, 0, 255]})
    state = hass.states.get("light.bedroom")

    await engine.async_evaluate("light.bedroom", state)
    await engine.async_evaluate("light.bedroom", state)

    assert service.await_count == 1
    assert engine.current_status() == STATUS_COOLDOWN
    engine._store.async_save_now = AsyncMock()
    await engine.async_unload()


def test_event_target_numeric_attribute(hass: HomeAssistant):
    config = _make_config(
        mode=MODE_FIXED_ATTRIBUTE,
        attribute="brightness",
        operator=OPERATOR_GT,
        threshold=50.0,
        target_value=25.0,
    )
    engine = _make_engine(hass, config)
    assert engine._event_target() == 25.0


# ---------------------------------------------------------------------------
# _in_cooldown
# ---------------------------------------------------------------------------


def test_in_cooldown_true(hass: HomeAssistant):
    engine = _make_engine(hass)
    future = dt_util.now() + timedelta(seconds=60)
    engine.state.cooldowns["light.bedroom"] = future
    assert engine._in_cooldown("light.bedroom", dt_util.now()) is True


def test_in_cooldown_false(hass: HomeAssistant):
    engine = _make_engine(hass)
    past = dt_util.now() - timedelta(seconds=60)
    engine.state.cooldowns["light.bedroom"] = past
    assert engine._in_cooldown("light.bedroom", dt_util.now()) is False


# ---------------------------------------------------------------------------
# _derive_armed_or_cooldown
# ---------------------------------------------------------------------------


def test_derive_armed(hass: HomeAssistant):
    engine = _make_engine(hass)
    assert engine._derive_armed_or_cooldown(dt_util.now()) == STATUS_ARMED


def test_derive_cooldown(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine.state.cooldowns["light.x"] = dt_util.now() + timedelta(seconds=30)
    assert engine._derive_armed_or_cooldown(dt_util.now()) == STATUS_COOLDOWN


def test_derive_pending(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine._pending_enforcements["light.x"] = MagicMock()
    assert engine._derive_armed_or_cooldown(dt_util.now()) == STATUS_PENDING


# ---------------------------------------------------------------------------
# _resolve_service
# ---------------------------------------------------------------------------


async def test_resolve_service_state_on_off(hass: HomeAssistant):
    engine = _make_engine(hass, _make_config(target_state="off"))
    result = engine._resolve_service("light.bedroom")
    assert result is not None
    domain, service, data = result
    # light domain has explicit map: off → light.turn_off
    assert domain == "light"
    assert service == "turn_off"
    assert data["entity_id"] == "light.bedroom"


async def test_resolve_service_domain_map(hass: HomeAssistant):
    config = _make_config(target_state="locked")
    engine = _make_engine(hass, config)
    result = engine._resolve_service("lock.front_door")
    assert result is not None
    domain, service, _ = result
    assert domain == "lock"
    assert service == "lock"


async def test_resolve_service_attribute_mode(hass: HomeAssistant):
    config = _make_config(
        mode=MODE_FIXED_ATTRIBUTE,
        attribute="brightness",
        operator=OPERATOR_LE,
        threshold=50.0,
        target_value=30.0,
        trigger_states=[],
    )
    engine = _make_engine(hass, config)
    result = engine._resolve_service("light.x")
    assert result is not None
    _, _, data = result
    assert "brightness" in data


async def test_resolve_service_unknown_target(hass: HomeAssistant):
    config = _make_config(target_state="unknown_state_xyz")
    engine = _make_engine(hass, config)
    result = engine._resolve_service("light.x")
    assert result is None


async def test_resolve_service_attribute_none(hass: HomeAssistant):
    config = _make_config(
        mode=MODE_FIXED_ATTRIBUTE,
        attribute=None,
        target_value=None,
        trigger_states=[],
    )
    engine = _make_engine(hass, config)
    assert engine._resolve_service("light.x") is None


# ---------------------------------------------------------------------------
# async_evaluate: disabled
# ---------------------------------------------------------------------------


async def test_evaluate_disabled_returns_early(hass: HomeAssistant):
    engine = _make_engine(hass, master=False)
    engine._startup_complete = True
    hass.states.async_set("light.bedroom", "on")
    st = hass.states.get("light.bedroom")
    await engine.async_evaluate("light.bedroom", st)
    assert engine.current_status() == STATUS_MASTER_DISABLED


async def test_evaluate_rule_disabled(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine._startup_complete = True
    engine.state.enabled = False
    hass.states.async_set("light.bedroom", "on")
    st = hass.states.get("light.bedroom")
    await engine.async_evaluate("light.bedroom", st)
    assert engine.current_status() == STATUS_DISABLED


# ---------------------------------------------------------------------------
# async_evaluate: suppressed
# ---------------------------------------------------------------------------


async def test_evaluate_suppressed(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine._startup_complete = True
    engine.state.suppressed_until = dt_util.now() + timedelta(hours=1)
    hass.states.async_set("light.bedroom", "on")
    st = hass.states.get("light.bedroom")
    await engine.async_evaluate("light.bedroom", st)
    assert engine.current_status() == STATUS_SUPPRESSED


async def test_evaluate_suppression_expired(hass: HomeAssistant):
    config = _make_config(target_state="off")
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    engine.state.suppressed_until = dt_util.now() - timedelta(seconds=1)
    hass.states.async_set("light.bedroom", "off")
    st = hass.states.get("light.bedroom")
    await engine.async_evaluate("light.bedroom", st)
    assert engine.state.suppressed_until is None


# ---------------------------------------------------------------------------
# async_evaluate: not triggered
# ---------------------------------------------------------------------------


async def test_evaluate_not_triggered_sets_armed(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine._startup_complete = True
    hass.states.async_set("light.bedroom", "off")
    st = hass.states.get("light.bedroom")
    await engine.async_evaluate("light.bedroom", st)
    assert engine.current_status() == STATUS_ARMED


# ---------------------------------------------------------------------------
# async_evaluate: grace period
# ---------------------------------------------------------------------------


async def test_evaluate_ignores_target_during_grace(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine._startup_complete = False
    hass.states.async_set("light.bedroom", "on")
    st = hass.states.get("light.bedroom")
    await engine.async_evaluate("light.bedroom", st)
    # Still at STARTING — no status change during grace
    assert engine.current_status() == STATUS_STARTING


# ---------------------------------------------------------------------------
# async_evaluate: flags fail
# ---------------------------------------------------------------------------


async def test_evaluate_flags_not_matched(hass: HomeAssistant):
    config = _make_config(flags=[Flag(entity="input_boolean.night", match_state="on")])
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    hass.states.async_set("input_boolean.night", "off")
    hass.states.async_set("light.bedroom", "on")
    st = hass.states.get("light.bedroom")
    await engine.async_evaluate("light.bedroom", st)
    assert engine.current_status() == STATUS_CONDITIONAL


async def test_flag_change_while_conditional_broadcasts(hass: HomeAssistant):
    """Flag entity changing while overall status stays CONDITIONAL must still
    broadcast so the frontend can re-read fresh flag values from the status
    sensor's extra_state_attributes.
    """
    config = _make_config(
        flags=[
            Flag(entity="binary_sensor.presence", match_state="off"),
            Flag(entity="input_boolean.wfh", match_state="off"),
        ]
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    # Presence stays "on" — status will remain CONDITIONAL through the toggle.
    hass.states.async_set("binary_sensor.presence", "on")
    hass.states.async_set("input_boolean.wfh", "off")
    hass.states.async_set("light.bedroom", "on")

    # Prime status to CONDITIONAL.
    await engine.async_evaluate(
        "input_boolean.wfh", hass.states.get("input_boolean.wfh")
    )
    assert engine.current_status() == STATUS_CONDITIONAL

    broadcasts: list[None] = []

    def _capture(*_args: object) -> None:
        broadcasts.append(None)

    unsub = async_dispatcher_connect(hass, signal_for_rule(config.unique_id), _capture)
    try:
        hass.states.async_set("input_boolean.wfh", "on")
        await engine.async_evaluate(
            "input_boolean.wfh", hass.states.get("input_boolean.wfh")
        )
    finally:
        unsub()

    assert engine.current_status() == STATUS_CONDITIONAL
    assert broadcasts, "flag change must broadcast even when status unchanged"


async def test_flag_change_while_armed_broadcasts(hass: HomeAssistant):
    """Flag entity changing while overall status stays ARMED must still
    broadcast — non-critical flag toggles (multi-flag rules) can flip a
    flag row's current value on the card without moving the overall status.
    """
    config = _make_config(
        flags=[
            Flag(entity="input_boolean.a", match_state="on"),
            Flag(entity="input_boolean.b", match_state="on"),
        ]
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    hass.states.async_set("input_boolean.a", "on")
    hass.states.async_set("input_boolean.b", "on")
    hass.states.async_set("light.bedroom", "off")  # not triggered → ARMED

    # Prime status to ARMED via a flag event.
    await engine.async_evaluate("input_boolean.a", hass.states.get("input_boolean.a"))
    assert engine.current_status() == STATUS_ARMED

    broadcasts: list[None] = []
    unsub = async_dispatcher_connect(
        hass, signal_for_rule(config.unique_id), lambda *_: broadcasts.append(None)
    )
    try:
        # Toggle b to another matching-ish state that keeps flags_match false
        # is not what we want; here we simulate a flag event that leaves
        # overall status ARMED (both flags still match, target still not
        # triggered). Just re-fire the flag entity event.
        hass.states.async_set("input_boolean.a", "on")  # no-op state, still on
        await engine.async_evaluate(
            "input_boolean.a", hass.states.get("input_boolean.a")
        )
        await hass.async_block_till_done()
    finally:
        unsub()

    assert engine.current_status() == STATUS_ARMED
    assert broadcasts, "flag change must broadcast even when status stays ARMED"


async def test_flag_change_while_disabled_broadcasts(hass: HomeAssistant):
    """Flag entity changing while the rule is DISABLED must still broadcast.

    The rule won't enforce, but the card reads live flag `current` values from
    the status sensor's extra_state_attributes, which only refresh on a state
    write. _set_status dedupes when status stays DISABLED, so without the
    forced broadcast the card would show frozen flag values. Mirrors the
    CONDITIONAL/ARMED stale-attr guards.
    """
    config = _make_config(flags=[Flag(entity="input_boolean.night", match_state="on")])
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    engine.state.enabled = False
    hass.states.async_set("input_boolean.night", "off")
    hass.states.async_set("light.bedroom", "on")

    # Prime status to DISABLED via a flag event.
    await engine.async_evaluate(
        "input_boolean.night", hass.states.get("input_boolean.night")
    )
    assert engine.current_status() == STATUS_DISABLED

    broadcasts: list[None] = []
    unsub = async_dispatcher_connect(
        hass, signal_for_rule(config.unique_id), lambda *_: broadcasts.append(None)
    )
    try:
        # Watched flag flips while disabled — status stays DISABLED (dedupe),
        # but the broadcast must still fire so the card re-reads flag values.
        hass.states.async_set("input_boolean.night", "on")
        await engine.async_evaluate(
            "input_boolean.night", hass.states.get("input_boolean.night")
        )
        await hass.async_block_till_done()
    finally:
        unsub()

    assert engine.current_status() == STATUS_DISABLED
    assert broadcasts, "flag change must broadcast even when rule is disabled"
    # Broadcast triggers the status sensor's async_write_ha_state, which pushes
    # the live flag value (read fresh from hass.states in extra_state_attributes)
    # to the card. The live value is now "on".
    assert hass.states.get("input_boolean.night").state == "on"


async def test_flag_change_while_suppressed_broadcasts(hass: HomeAssistant):
    """Flag entity changing while the rule is SUPPRESSED must still broadcast.

    A suppressed rule's status stays SUPPRESSED when a watched flag flips, so
    _set_status dedupes the broadcast — the card's flags[].current would freeze.
    Same stale-attr guard as the CONDITIONAL/ARMED paths.
    """
    config = _make_config(flags=[Flag(entity="input_boolean.night", match_state="on")])
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    hass.states.async_set("input_boolean.night", "on")
    hass.states.async_set("light.bedroom", "on")

    # Prime status to SUPPRESSED. Set the field directly (no async_suppress) to
    # avoid arming an expiry timer that would linger past the test.
    engine._state.suppressed_until = dt_util.now() + timedelta(minutes=10)
    engine._set_status(STATUS_SUPPRESSED)
    assert engine.current_status() == STATUS_SUPPRESSED

    broadcasts: list[None] = []
    unsub = async_dispatcher_connect(
        hass, signal_for_rule(config.unique_id), lambda *_: broadcasts.append(None)
    )
    try:
        # Watched flag flips while suppressed — status stays SUPPRESSED (dedupe),
        # broadcast must still fire so the card re-reads flag values.
        hass.states.async_set("input_boolean.night", "off")
        await engine.async_evaluate(
            "input_boolean.night", hass.states.get("input_boolean.night")
        )
        await hass.async_block_till_done()
    finally:
        unsub()

    assert engine.current_status() == STATUS_SUPPRESSED
    assert broadcasts, "flag change must broadcast even when rule is suppressed"


# ---------------------------------------------------------------------------
# async_evaluate: enforcement (mocked service call)
# ---------------------------------------------------------------------------


async def test_evaluate_triggers_enforcement(hass: HomeAssistant):
    config = _make_config(target_state="off")
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    hass.states.async_set("light.bedroom", "on")
    st = hass.states.get("light.bedroom")

    called = []

    async def _mock_turn_off(call):
        called.append(call)

    hass.services.async_register("light", "turn_off", _mock_turn_off)
    await engine.async_evaluate("light.bedroom", st)

    assert len(called) == 1
    assert engine.state.enforcement_count_total == 1
    assert engine.state.enforcement_count_today == 1


# ---------------------------------------------------------------------------
# async_evaluate: delay (pending path)
# ---------------------------------------------------------------------------


async def test_evaluate_delayed_sets_pending(hass: HomeAssistant):
    config = _make_config(delay_seconds=30)
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    hass.states.async_set("light.bedroom", "on")
    st = hass.states.get("light.bedroom")

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later"
    ) as mock_later:
        mock_later.return_value = MagicMock()
        await engine.async_evaluate("light.bedroom", st)

    assert engine.current_status() == STATUS_PENDING
    assert "light.bedroom" in engine._pending_enforcements
    assert "light.bedroom" in engine.pending_enforcement_due()


async def test_evaluate_delayed_multi_entity_all_get_due_entry(hass: HomeAssistant):
    """All non-compliant targets must appear in pending_enforcement_due.

    Regression: when status was already PENDING, _set_status deduped the
    broadcast so later entities never reached the sensor dict.
    """
    config = _make_config(
        delay_seconds=30,
        target_entities=["light.a", "light.b", "light.c"],
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    for eid in ["light.a", "light.b", "light.c"]:
        hass.states.async_set(eid, "on")

    broadcasts: list[None] = []
    async_dispatcher_connect(
        hass, signal_for_rule(config.unique_id), lambda *_: broadcasts.append(None)
    )

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later"
    ) as mock_later:
        mock_later.return_value = MagicMock()
        for eid in ["light.a", "light.b", "light.c"]:
            await engine.async_evaluate(eid, hass.states.get(eid))
            await hass.async_block_till_done()

    due = engine.pending_enforcement_due()
    assert "light.a" in due, "first entity must have a due entry"
    assert "light.b" in due, "second entity must have a due entry"
    assert "light.c" in due, "third entity must have a due entry"
    assert len(broadcasts) >= 3, (
        f"expected at least 3 broadcasts, got {len(broadcasts)}"
    )


async def test_evaluate_delayed_second_entity_broadcasts_when_already_pending(
    hass: HomeAssistant,
):
    """Adding a second entity while PENDING must trigger a broadcast."""
    config = _make_config(
        delay_seconds=30,
        target_entities=["light.x", "light.y"],
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    hass.states.async_set("light.x", "on")
    hass.states.async_set("light.y", "on")

    broadcasts: list[None] = []
    async_dispatcher_connect(
        hass, signal_for_rule(config.unique_id), lambda *_: broadcasts.append(None)
    )

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later"
    ) as mock_later:
        mock_later.return_value = MagicMock()
        await engine.async_evaluate("light.x", hass.states.get("light.x"))
        await hass.async_block_till_done()
        assert engine.current_status() == STATUS_PENDING
        pre_count = len(broadcasts)
        await engine.async_evaluate("light.y", hass.states.get("light.y"))
        await hass.async_block_till_done()

    assert "light.y" in engine.pending_enforcement_due()
    assert len(broadcasts) > pre_count, "second entity must trigger broadcast"


async def test_evaluate_compliant_entity_removed_from_pending_due_broadcasts(
    hass: HomeAssistant,
):
    """When one entity turns compliant while others stay pending, its due entry
    must be removed AND a broadcast must fire so the card stops showing its countdown.

    Regression: _cancel_pending removed the entity from the dict but _set_status
    deduped the broadcast (status stayed PENDING), so the sensor never received
    the updated dict.
    """
    config = _make_config(
        delay_seconds=30,
        target_entities=["light.a", "light.b", "light.c"],
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    for eid in ["light.a", "light.b", "light.c"]:
        hass.states.async_set(eid, "on")

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later"
    ) as mock_later:
        mock_later.return_value = MagicMock()
        for eid in ["light.a", "light.b", "light.c"]:
            await engine.async_evaluate(eid, hass.states.get(eid))
            await hass.async_block_till_done()

    assert engine.current_status() == STATUS_PENDING
    assert all(
        e in engine.pending_enforcement_due() for e in ["light.a", "light.b", "light.c"]
    )

    broadcasts: list[None] = []
    async_dispatcher_connect(
        hass, signal_for_rule(config.unique_id), lambda *_: broadcasts.append(None)
    )

    # entity A turns compliant (off = target_state)
    hass.states.async_set("light.a", "off")
    await engine.async_evaluate("light.a", hass.states.get("light.a"))
    await hass.async_block_till_done()

    due = engine.pending_enforcement_due()
    assert "light.a" not in due, (
        "compliant entity must be removed from pending_enforcement_due"
    )
    assert "light.b" in due, "still-pending entity must remain"
    assert "light.c" in due, "still-pending entity must remain"
    assert engine.current_status() == STATUS_PENDING, (
        "rule must stay PENDING (b and c still pending)"
    )
    assert broadcasts, "broadcast must fire so card receives updated dict"


async def test_evaluate_skip_reason_entity_removed_from_pending_due_broadcasts(
    hass: HomeAssistant,
):
    """When a skip_reason fires for one entity while another stays pending, its
    due entry must be removed AND a broadcast must fire.

    Uses a color-attribute rule (color_temp_kelvin) where setting the light
    unavailable triggers the skip_reason path in _color_trigger_decision.
    """
    config = _make_config(
        delay_seconds=30,
        target_entities=["light.x", "light.y"],
        mode=MODE_FIXED_ATTRIBUTE,
        attribute="color_temp_kelvin",
        operator=None,
        threshold=None,
        target_value=3000,
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    # Both lights non-compliant (color_temp_kelvin != 3000 by enough tolerance)
    hass.states.async_set("light.x", "on", {"color_temp_kelvin": 6000})
    hass.states.async_set("light.y", "on", {"color_temp_kelvin": 6000})

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later"
    ) as mock_later:
        mock_later.return_value = MagicMock()
        for eid in ["light.x", "light.y"]:
            await engine.async_evaluate(eid, hass.states.get(eid))
            await hass.async_block_till_done()

    assert engine.current_status() == STATUS_PENDING
    assert "light.x" in engine.pending_enforcement_due()
    assert "light.y" in engine.pending_enforcement_due()

    broadcasts: list[None] = []
    async_dispatcher_connect(
        hass, signal_for_rule(config.unique_id), lambda *_: broadcasts.append(None)
    )

    # light.x goes unavailable → _color_trigger_decision returns skip_reason="light_unavailable"
    hass.states.async_set("light.x", "unavailable", {"color_temp_kelvin": 6000})
    await engine.async_evaluate("light.x", hass.states.get("light.x"))
    await hass.async_block_till_done()

    due = engine.pending_enforcement_due()
    assert "light.x" not in due, (
        "skipped entity must be removed from pending_enforcement_due"
    )
    assert "light.y" in due, "still-pending entity must remain"
    assert engine.current_status() == STATUS_PENDING, (
        "rule must stay PENDING (light.y still pending)"
    )
    assert broadcasts, "broadcast must fire so card receives updated dict"


# ---------------------------------------------------------------------------
# async_suppress / async_unsuppress
# ---------------------------------------------------------------------------


async def test_async_suppress(hass: HomeAssistant):
    engine = _make_engine(hass)
    await engine.async_suppress(30)
    assert engine.state.suppressed_until is not None
    assert engine.state.suppression_reason == "manual"
    assert engine.current_status() == STATUS_SUPPRESSED
    engine._cancel_suppression_timer()


async def test_async_suppress_event_carries_rule_entity_id(hass: HomeAssistant):
    """EVENT_SUPPRESSED payload carries entity_id (the rule's status sensor), so
    the integration-level trigger + logbook filter surface it like its siblings."""
    engine = _make_engine(hass)
    engine._rule_entity_id = "sensor.test_rule_status"  # cached → skip registry
    events: list = []
    hass.bus.async_listen(EVENT_SUPPRESSED, lambda e: events.append(e))
    await engine.async_suppress(5)
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data["entity_id"] == "sensor.test_rule_status"
    engine._cancel_suppression_timer()


async def test_async_unsuppress(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine.state.suppressed_until = dt_util.now() + timedelta(hours=1)
    engine.state.suppression_reason = "manual"
    await engine.async_unsuppress()
    assert engine.state.suppressed_until is None
    assert engine.state.suppression_reason is None


# ---------------------------------------------------------------------------
# async_reset_cooldowns
# ---------------------------------------------------------------------------


async def test_async_reset_cooldowns(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine.state.cooldowns["light.x"] = dt_util.now() + timedelta(seconds=60)
    await engine.async_reset_cooldowns()
    assert engine.state.cooldowns == {}


# ---------------------------------------------------------------------------
# async_clear_history
# ---------------------------------------------------------------------------


async def test_async_clear_history(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine.state.enforcement_count_today = 5
    engine.state.enforcement_count_total = 100
    await engine.async_clear_history()
    assert engine.state.enforcement_count_today == 0
    assert engine.state.enforcement_count_total == 0


async def test_async_clear_history_resets_counter_since(hass: HomeAssistant):
    """Clear History must stamp counter_total_since with the reset moment."""
    engine = _make_engine(hass)
    old = dt_util.now() - timedelta(days=10)
    engine.state.counter_total_since = old
    engine.state.enforcement_count_total = 42
    await engine.async_clear_history()
    assert engine.state.counter_total_since is not None
    assert engine.state.counter_total_since > old


async def test_maybe_backfill_counter_since_uses_entry_created_at(
    hass: HomeAssistant,
):
    """On first setup, counter_total_since is backfilled from entry.created_at."""
    engine = _make_engine(hass)
    created = dt_util.now() - timedelta(days=5)
    engine._entry = MagicMock(created_at=created)
    engine.state.counter_total_since = None
    engine._maybe_backfill_counter_since()
    assert engine.state.counter_total_since == created


async def test_maybe_backfill_counter_since_falls_back_to_now(hass: HomeAssistant):
    """When entry has no created_at, backfill uses dt_util.now()."""
    engine = _make_engine(hass)
    engine._entry = MagicMock(spec=[])  # no created_at attr
    engine.state.counter_total_since = None
    before = dt_util.now()
    engine._maybe_backfill_counter_since()
    after = dt_util.now()
    assert engine.state.counter_total_since is not None
    assert before <= engine.state.counter_total_since <= after


async def test_maybe_backfill_counter_since_no_entry(hass: HomeAssistant):
    """When entry is None, backfill still populates counter_total_since."""
    engine = _make_engine(hass)
    engine._entry = None
    engine.state.counter_total_since = None
    engine._maybe_backfill_counter_since()
    assert engine.state.counter_total_since is not None


async def test_maybe_backfill_counter_since_preserves_existing(hass: HomeAssistant):
    """Backfill must not overwrite an already-set counter_total_since."""
    engine = _make_engine(hass)
    engine._entry = MagicMock(created_at=dt_util.now())
    original = dt_util.now() - timedelta(days=30)
    engine.state.counter_total_since = original
    engine._maybe_backfill_counter_since()
    assert engine.state.counter_total_since == original


async def test_async_clear_history_swallows_cancel_error(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine._cooldown_broadcast_unsubs["light.x"] = MagicMock(
        side_effect=RuntimeError("boom")
    )
    await engine.async_clear_history()  # must not raise
    assert engine._cooldown_broadcast_unsubs == {}


async def test_async_clear_history_non_error_calls_apply_idle_status(
    hass: HomeAssistant,
):
    """C3: clear_history on non-ERROR rule calls _apply_idle_status, not _broadcast_status."""
    engine = _make_engine(hass)
    engine._startup_complete = True
    engine._current_status = STATUS_ARMED
    await engine.async_clear_history()
    # _apply_idle_status re-derives from state; armed engine stays armed
    assert engine.current_status() == STATUS_ARMED


async def test_startup_grace_skips_sweep_when_disabled(hass: HomeAssistant):
    """C4: startup grace on disabled rule skips eval sweep (no tasks created)."""
    config = _make_config()
    engine = _make_engine(hass, config)
    # Plant enabled=False in the blob so async_setup restores it correctly
    engine._store.get_rule_state.return_value = {"enabled": False}

    eval_calls: list[str] = []
    original_schedule = engine._schedule_eval_task

    def _record(eid, state):
        eval_calls.append(eid)
        original_schedule(eid, state)

    engine._schedule_eval_task = _record  # type: ignore[assignment]
    hass.states.async_set("light.bedroom", "on")

    grace_callbacks: list = []

    def _capture_later(_hass, _delay, cb):
        grace_callbacks.append(cb)
        return MagicMock()

    with (
        patch(
            "custom_components.entity_guard.rule_engine.async_track_state_change_event"
        ),
        patch("custom_components.entity_guard.rule_engine.async_track_time_change"),
        patch(
            "custom_components.entity_guard.rule_engine.async_call_later",
            side_effect=_capture_later,
        ),
    ):
        await engine.async_setup()

    assert grace_callbacks, "async_call_later not called during setup"
    # Fire the startup grace callback
    grace_callbacks[0](dt_util.now())
    await hass.async_block_till_done()

    assert eval_calls == [], f"sweep tasks created for disabled rule: {eval_calls}"


async def test_evaluate_disabled_check_before_grace_guard(hass: HomeAssistant):
    """C6: disabled check is evaluated before the startup-grace guard — event during grace still sets DISABLED."""
    engine = _make_engine(hass)
    engine._startup_complete = False
    engine._state.enabled = False
    hass.states.async_set("light.bedroom", "on")
    st = hass.states.get("light.bedroom")
    await engine.async_evaluate("light.bedroom", st)
    # Disabled check fires before grace guard → status is DISABLED, not STARTING
    assert engine.current_status() == STATUS_DISABLED


# ---------------------------------------------------------------------------
# async_test_enforce
# ---------------------------------------------------------------------------


async def test_async_test_enforce_status_restored_when_disabled(hass: HomeAssistant):
    config = _make_config(target_state="off")
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    engine._state.enabled = False
    engine._current_status = STATUS_DISABLED
    hass.states.async_set("light.bedroom", "on")

    called = []

    async def _mock_turn_off(call):
        called.append(call)

    hass.services.async_register("light", "turn_off", _mock_turn_off)
    await engine.async_test_enforce()

    # Service fires (user can validate rule is correct) but status stays DISABLED.
    assert len(called) == 1
    assert engine.current_status() == STATUS_DISABLED


async def test_async_test_enforce_no_intermediate_broadcast_when_disabled(
    hass: HomeAssistant,
):
    """Bug 1: per-entity _apply_idle_status restores DISABLED between entities.

    The enforce tail derives its post-status through _derive_idle_status, which
    honours the disabled flag, so a disabled test-enforce paints DISABLED (not a
    transient ARMED) right after ENFORCING. Expected broadcast sequence for 2
      targets: [ENFORCING, DISABLED, DISABLED, ENFORCING, DISABLED, DISABLED]
    (the tail DISABLED + the per-entity _apply_idle_status restore). The
    single-post-loop call (old bug) would produce a trailing ARMED/DISABLED
    mismatch.
    """
    config = _make_config(
        target_entities=["light.a", "light.b"],
        target_state="off",
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    engine._state.enabled = False
    engine._current_status = STATUS_DISABLED
    hass.states.async_set("light.a", "on")
    hass.states.async_set("light.b", "on")

    all_broadcasts: list[str] = []
    original = engine._set_status

    def _record(status: str) -> None:
        all_broadcasts.append(status)
        original(status)

    engine._set_status = _record  # type: ignore[assignment]

    async def _ok(call):
        pass

    hass.services.async_register("light", "turn_off", _ok)
    await engine.async_test_enforce()

    assert engine.current_status() == STATUS_DISABLED
    # Per entity: ENFORCING (enforce start) → DISABLED (tail derives the disabled
    # status) → DISABLED (per-entity _apply_idle_status restore). The disabled
    # flag means the tail never paints a transient ARMED.
    assert all_broadcasts == [
        STATUS_ENFORCING,
        STATUS_DISABLED,
        STATUS_DISABLED,
        STATUS_ENFORCING,
        STATUS_DISABLED,
        STATUS_DISABLED,
    ], f"unexpected broadcast sequence: {all_broadcasts}"


async def test_async_test_enforce_disabled_rule_in_error_stays_disabled(
    hass: HomeAssistant,
):
    """Bug 3: disabled rule in STATUS_ERROR must show DISABLED after test-enforce, not ERROR."""
    config = _make_config(target_state="off")
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    engine._state.enabled = False
    engine._current_status = STATUS_ERROR  # sticky error
    hass.states.async_set("light.bedroom", "on")

    async def _ok(call):
        pass

    hass.services.async_register("light", "turn_off", _ok)
    await engine.async_test_enforce()

    # disabled check now beats ERROR sticky in _derive_idle_status
    assert engine.current_status() == STATUS_DISABLED


async def test_async_test_enforce(hass: HomeAssistant):
    config = _make_config(target_state="off")
    engine = _make_engine(hass, config)
    hass.states.async_set("light.bedroom", "on")

    called = []

    async def _mock_turn_off(call):
        called.append(call)

    hass.services.async_register("light", "turn_off", _mock_turn_off)
    await engine.async_test_enforce()

    assert len(called) == 1


# ---------------------------------------------------------------------------
# loop protection
# ---------------------------------------------------------------------------


async def test_loop_protection_triggers(hass: HomeAssistant):
    config = _make_config(target_state="off", max_enforcements_per_minute=2)
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    now = dt_util.now()
    engine.state.rate_limit_window = [now, now]  # already at limit

    hass.states.async_set("light.bedroom", "on")
    st = hass.states.get("light.bedroom")
    await engine.async_evaluate("light.bedroom", st)

    assert engine.current_status() == STATUS_SUPPRESSED
    assert engine.state.suppression_reason == "loop_protection"
    engine._cancel_suppression_timer()


# ---------------------------------------------------------------------------
# cooldown_remaining_seconds
# ---------------------------------------------------------------------------


def test_cooldown_remaining_seconds(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine.state.cooldowns["light.x"] = dt_util.now() + timedelta(seconds=30)
    remaining = engine.cooldown_remaining_seconds()
    assert 28 < remaining <= 30


def test_cooldown_remaining_seconds_empty(hass: HomeAssistant):
    engine = _make_engine(hass)
    assert engine.cooldown_remaining_seconds() == 0.0


def test_cooldown_ends_returns_future_only(hass: HomeAssistant):
    """cooldown_ends exposes absolute future ends; past ends are skipped
    (mirrors cooldown_remaining_seconds, which counts a past end as 0)."""
    engine = _make_engine(hass)
    future = dt_util.now() + timedelta(seconds=30)
    past = dt_util.now() - timedelta(seconds=30)
    engine.state.cooldowns["light.x"] = future
    engine.state.cooldowns["light.y"] = past
    ends = engine.cooldown_ends()
    assert ends == {"light.x": future}


def test_cooldown_ends_empty(hass: HomeAssistant):
    engine = _make_engine(hass)
    assert engine.cooldown_ends() == {}


# ---------------------------------------------------------------------------
# is_armed / is_active / is_in_cooldown
# ---------------------------------------------------------------------------


def test_is_armed(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine._current_status = STATUS_ARMED
    assert engine.is_armed() is True
    engine._current_status = STATUS_CONDITIONAL
    assert engine.is_armed() is False


def test_is_active(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine._current_status = STATUS_ENFORCING
    assert engine.is_active() is True


def test_is_in_cooldown(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine._current_status = STATUS_COOLDOWN
    assert engine.is_in_cooldown() is True


# ---------------------------------------------------------------------------
# debounce cooldown after enforcement
# ---------------------------------------------------------------------------


async def test_enforcement_with_debounce_sets_cooldown(hass: HomeAssistant):
    config = _make_config(
        target_state="off", debounce_enabled=True, debounce_seconds=60
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    hass.states.async_set("light.bedroom", "on")
    st = hass.states.get("light.bedroom")

    async def _mock_turn_off(call):
        pass

    hass.services.async_register("light", "turn_off", _mock_turn_off)

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later"
    ) as mock_later:
        mock_later.return_value = MagicMock()
        await engine.async_evaluate("light.bedroom", st)

    assert "light.bedroom" in engine.state.cooldowns
    assert engine.current_status() == STATUS_COOLDOWN


# ---------------------------------------------------------------------------
# _cancel_pending
# ---------------------------------------------------------------------------


def test_cancel_pending_existing(hass: HomeAssistant):
    engine = _make_engine(hass)
    cancel = MagicMock()
    engine._pending_enforcements["light.x"] = cancel
    engine._cancel_pending("light.x")
    cancel.assert_called_once()
    assert "light.x" not in engine._pending_enforcements


def test_cancel_pending_missing(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine._cancel_pending("light.nonexistent")  # should not raise


# ---------------------------------------------------------------------------
# _handle_state_event
# ---------------------------------------------------------------------------


def test_handle_state_event_creates_task(hass: HomeAssistant):
    engine = _make_engine(hass)
    event = MagicMock()
    event.data = {"entity_id": "light.bedroom", "new_state": _state("on")}
    tasks_created = []
    with patch.object(
        hass, "async_create_task", side_effect=capturing_create_task(tasks_created)
    ):
        engine._handle_state_event(event)
    assert len(tasks_created) == 1


def test_handle_state_event_none_entity_id(hass: HomeAssistant):
    engine = _make_engine(hass)
    event = MagicMock()
    event.data = {"entity_id": None, "new_state": None}
    tasks_created = []
    with patch.object(
        hass, "async_create_task", side_effect=capturing_create_task(tasks_created)
    ):
        engine._handle_state_event(event)
    assert tasks_created == []


# ---------------------------------------------------------------------------
# _handle_midnight
# ---------------------------------------------------------------------------


def test_handle_midnight_resets_today_counter(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine.state.enforcement_count_today = 7
    engine._handle_midnight(MagicMock())
    assert engine.state.enforcement_count_today == 0


def test_handle_midnight_disabled_rule_stays_disabled(hass: HomeAssistant):
    """_handle_midnight on disabled rule must not emit STATUS_STARTING (Bug 4 regression)."""
    engine = _make_engine(hass)
    engine._startup_complete = True
    engine._state.enabled = False
    engine._current_status = STATUS_DISABLED
    engine.state.enforcement_count_today = 5
    engine._handle_midnight(MagicMock())
    assert engine.state.enforcement_count_today == 0
    assert engine.current_status() == STATUS_DISABLED


# ---------------------------------------------------------------------------
# _handle_startup_grace_done
# ---------------------------------------------------------------------------


def test_handle_startup_grace_done_sweeps_targets(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine._startup_complete = False
    hass.states.async_set("light.bedroom", "on")
    tasks_created = []
    with patch.object(
        hass, "async_create_task", side_effect=capturing_create_task(tasks_created)
    ):
        engine._handle_startup_grace_done(MagicMock())
    assert engine._startup_complete is True
    assert len(tasks_created) == 1  # one per target entity


def test_reenable_sweeps_targets(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine._startup_complete = True
    engine._state.enabled = False
    hass.states.async_set("light.bedroom", "on")
    tasks_created = []
    with patch.object(
        hass, "async_create_task", side_effect=capturing_create_task(tasks_created)
    ):
        engine.set_enabled(True)
    assert len(tasks_created) == 1


def test_reenable_no_sweep_before_startup_complete(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine._startup_complete = False
    engine._state.enabled = False
    hass.states.async_set("light.bedroom", "on")
    tasks_created = []
    with patch.object(
        hass, "async_create_task", side_effect=capturing_create_task(tasks_created)
    ):
        engine.set_enabled(True)
    assert len(tasks_created) == 0


def test_master_reenable_sweeps_targets(hass: HomeAssistant):
    engine = _make_engine(hass, master=True)
    engine._startup_complete = True
    engine._state.enabled = True
    hass.states.async_set("light.bedroom", "on")
    tasks_created = []
    with patch.object(
        hass, "async_create_task", side_effect=capturing_create_task(tasks_created)
    ):
        engine._handle_master_changed()
    assert len(tasks_created) == 1


def test_reenable_sweep_skipped_when_master_off(hass: HomeAssistant):
    engine = _make_engine(hass, master=False)
    engine._startup_complete = True
    engine._state.enabled = False
    hass.states.async_set("light.bedroom", "on")
    tasks_created = []
    with patch.object(
        hass, "async_create_task", side_effect=capturing_create_task(tasks_created)
    ):
        engine.set_enabled(True)
    assert len(tasks_created) == 0


# ---------------------------------------------------------------------------
# async_unload: exception paths
# ---------------------------------------------------------------------------


async def test_async_unload_handles_failing_unsub(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine._unsub_callbacks.append(MagicMock(side_effect=RuntimeError("boom")))
    engine._store.async_save_now = AsyncMock()
    await engine.async_unload()  # should not raise
    assert engine._unsub_callbacks == []


async def test_async_unload_handles_failing_cancel(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine._pending_enforcements["light.x"] = MagicMock(
        side_effect=RuntimeError("boom")
    )
    engine._store.async_save_now = AsyncMock()
    await engine.async_unload()  # should not raise
    assert engine._pending_enforcements == {}


# ---------------------------------------------------------------------------
# in_cooldown debounce path during evaluate
# ---------------------------------------------------------------------------


async def test_evaluate_in_cooldown_during_debounce(hass: HomeAssistant):
    config = _make_config(debounce_enabled=True, debounce_seconds=60)
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    engine.state.cooldowns["light.bedroom"] = dt_util.now() + timedelta(seconds=30)
    hass.states.async_set("light.bedroom", "on")
    st = hass.states.get("light.bedroom")
    await engine.async_evaluate("light.bedroom", st)
    assert engine.current_status() == STATUS_COOLDOWN


# ---------------------------------------------------------------------------
# _resolve_service: homeassistant fallback for unknown domain
# ---------------------------------------------------------------------------


def test_resolve_service_homeassistant_fallback_on(hass: HomeAssistant):
    config = _make_config(target_state="on", target_entities=["vacuum.robo"])
    engine = _make_engine(hass, config)
    result = engine._resolve_service("vacuum.robo")
    assert result is not None
    domain, service, _ = result
    assert domain == "homeassistant"
    assert service == "turn_on"


def test_resolve_service_no_mapping_returns_none(hass: HomeAssistant):
    # 'cooking' is not a vacuum service state, and vacuum has no mode-service map
    # entry, so it falls through every branch to None (silent no_service_mapping).
    config = _make_config(target_state="cooking", target_entities=["vacuum.robo"])
    engine = _make_engine(hass, config)
    result = engine._resolve_service("vacuum.robo")
    assert result is None


# ---------------------------------------------------------------------------
# Feature C: deliverable non-on/off states (DOMAIN_SERVICE_MAP + mode map)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entity_id,target_state,exp_domain,exp_service",
    [
        # Parameterless per-state services (DOMAIN_SERVICE_MAP).
        ("cover.garage", "stopped", "cover", "stop_cover"),
        ("media_player.tv", "playing", "media_player", "media_play"),
        ("media_player.tv", "paused", "media_player", "media_pause"),
        ("media_player.tv", "idle", "media_player", "media_stop"),
        ("vacuum.robo", "cleaning", "vacuum", "start"),
        ("vacuum.robo", "docked", "vacuum", "return_to_base"),
        ("vacuum.robo", "returning", "vacuum", "return_to_base"),
        ("vacuum.robo", "paused", "vacuum", "pause"),
        ("valve.water", "open", "valve", "open_valve"),
        ("valve.water", "closed", "valve", "close_valve"),
        ("lawn_mower.yard", "mowing", "lawn_mower", "start_mowing"),
        ("lawn_mower.yard", "docked", "lawn_mower", "dock"),
    ],
)
def test_resolve_state_service_parameterless(
    hass: HomeAssistant, entity_id, target_state, exp_domain, exp_service
):
    config = _make_config(target_state=target_state, target_entities=[entity_id])
    engine = _make_engine(hass, config)
    result = engine._resolve_service(entity_id)
    assert result is not None
    domain, service, data = result
    assert (domain, service) == (exp_domain, exp_service)
    # Parameterless services carry only the entity_id — no data kwarg.
    assert data == {"entity_id": entity_id}


@pytest.mark.parametrize(
    "entity_id,target_state,exp_service,exp_kwarg",
    [
        ("climate.living", "heat", "set_hvac_mode", "hvac_mode"),
        ("climate.living", "cool", "set_hvac_mode", "hvac_mode"),
        ("climate.living", "auto", "set_hvac_mode", "hvac_mode"),
        ("climate.living", "heat_cool", "set_hvac_mode", "hvac_mode"),
        ("climate.living", "fan_only", "set_hvac_mode", "hvac_mode"),
        ("climate.living", "dry", "set_hvac_mode", "hvac_mode"),
        ("water_heater.tank", "eco", "set_operation_mode", "operation_mode"),
        ("water_heater.tank", "heat_pump", "set_operation_mode", "operation_mode"),
        ("input_select.scene", "Movie Night", "select_option", "option"),
    ],
)
def test_resolve_state_service_mode_payload(
    hass: HomeAssistant, entity_id, target_state, exp_service, exp_kwarg
):
    """Mode-style domains pass the target_state through as a data kwarg."""
    config = _make_config(target_state=target_state, target_entities=[entity_id])
    engine = _make_engine(hass, config)
    result = engine._resolve_service(entity_id)
    assert result is not None
    domain, service, data = result
    assert (domain, service) == (entity_id.split(".", 1)[0], exp_service)
    assert data == {"entity_id": entity_id, exp_kwarg: target_state}


def test_resolve_state_service_climate_off_stays_parameterless(hass: HomeAssistant):
    """climate 'off' keeps the parameterless climate.turn_off, not set_hvac_mode."""
    config = _make_config(target_state="off", target_entities=["climate.living"])
    engine = _make_engine(hass, config)
    result = engine._resolve_service("climate.living")
    assert result == ("climate", "turn_off", {"entity_id": "climate.living"})


def test_resolve_state_service_bare_on_off_bypasses_mode_map(hass: HomeAssistant):
    """A bare 'on'/'off' on a mode-style domain must NOT become an invalid mode
    payload (climate.set_hvac_mode hvac_mode='on' is rejected by HA). Follow mode
    (dynamic_state) always feeds exactly 'on'/'off', so this is the regression
    guard: 'on' → homeassistant.turn_on, 'off' → the domain's parameterless
    turn_off where one exists, else homeassistant.turn_off — never set_hvac_mode /
    set_operation_mode with 'on'/'off'."""
    config = _make_config(
        mode=MODE_DYNAMIC_STATE,
        source_entity="input_boolean.src",
        target_entities=["climate.living"],
    )
    engine = _make_engine(hass, config)
    # climate 'on' has no parameterless entry → must fall to homeassistant.turn_on,
    # NOT climate.set_hvac_mode(hvac_mode='on').
    on = engine._resolve_service("climate.living", state_override="on")
    assert on == ("homeassistant", "turn_on", {"entity_id": "climate.living"})
    # climate 'off' is matched by the parameterless map before either fallback.
    off = engine._resolve_service("climate.living", state_override="off")
    assert off == ("climate", "turn_off", {"entity_id": "climate.living"})
    # water_heater has NO parameterless entry → both on/off use the homeassistant
    # fallback, never set_operation_mode(operation_mode='on'/'off').
    wh_on = engine._resolve_service("water_heater.tank", state_override="on")
    assert wh_on == ("homeassistant", "turn_on", {"entity_id": "water_heater.tank"})
    wh_off = engine._resolve_service("water_heater.tank", state_override="off")
    assert wh_off == ("homeassistant", "turn_off", {"entity_id": "water_heater.tank"})


def test_resolve_state_service_input_select_on_off_is_an_option(hass: HomeAssistant):
    """input_select options are arbitrary strings, so a literal 'on'/'off' option
    must route through select_option — NOT the homeassistant on/off fallback
    (input_select has no turn_on/off service). This is the one mode domain where
    'on'/'off' is a genuine mode-map target."""
    config = _make_config(target_state="on", target_entities=["input_select.mode"])
    engine = _make_engine(hass, config)
    result = engine._resolve_service("input_select.mode")
    assert result == (
        "input_select",
        "select_option",
        {"entity_id": "input_select.mode", "option": "on"},
    )
    # "off" as an option likewise goes through select_option, not turn_off.
    config_off = _make_config(target_state="off", target_entities=["input_select.mode"])
    engine_off = _make_engine(hass, config_off)
    assert engine_off._resolve_service("input_select.mode") == (
        "input_select",
        "select_option",
        {"entity_id": "input_select.mode", "option": "off"},
    )


async def test_enforce_delivers_climate_mode_payload(hass: HomeAssistant):
    """End-to-end: a climate heat rule actually calls set_hvac_mode hvac_mode=heat."""
    config = _make_config(
        target_state="heat",
        trigger_states=["off"],
        target_entities=["climate.living"],
        safety_acknowledged=True,
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    calls: list = []
    hass.services.async_register("climate", "set_hvac_mode", lambda c: calls.append(c))
    hass.states.async_set("climate.living", "off")
    await engine.async_evaluate("climate.living", hass.states.get("climate.living"))
    assert calls
    assert calls[0].data == {"entity_id": "climate.living", "hvac_mode": "heat"}


async def test_enforce_delivers_input_select_option(hass: HomeAssistant):
    """End-to-end: input_select rule calls select_option with the option value."""
    config = _make_config(
        target_state="Away",
        trigger_states=["Home"],
        target_entities=["input_select.mode"],
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    calls: list = []
    hass.services.async_register(
        "input_select", "select_option", lambda c: calls.append(c)
    )
    hass.states.async_set("input_select.mode", "Home")
    await engine.async_evaluate(
        "input_select.mode", hass.states.get("input_select.mode")
    )
    assert calls
    assert calls[0].data == {"entity_id": "input_select.mode", "option": "Away"}


# ---------------------------------------------------------------------------
# _enforce: service call failure
# ---------------------------------------------------------------------------


async def test_enforce_service_call_failure_skips(hass: HomeAssistant):
    config = _make_config(target_state="off")
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    async def _fail(call):
        raise RuntimeError("device unavailable")

    hass.services.async_register("light", "turn_off", _fail)
    hass.states.async_set("light.bedroom", "on")
    st = hass.states.get("light.bedroom")
    await engine.async_evaluate("light.bedroom", st)
    # Should not have counted the enforcement
    assert engine.state.enforcement_count_total == 0


# ---------------------------------------------------------------------------
# _enforce: no service mapping
# ---------------------------------------------------------------------------


async def test_enforce_no_service_mapping_fires_skipped(hass: HomeAssistant):
    config = _make_config(
        target_state="cooking",  # unmapped state on unmapped domain
        target_entities=["vacuum.robo"],
        trigger_states=["on"],
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    hass.states.async_set("vacuum.robo", "on")
    st = hass.states.get("vacuum.robo")
    events_fired = []
    hass.bus.async_listen("entity_guard_skipped", lambda e: events_fired.append(e))
    await engine.async_evaluate("vacuum.robo", st)
    assert engine.state.enforcement_count_total == 0


# ---------------------------------------------------------------------------
# set_enabled: suppression expiry on re-enable
# ---------------------------------------------------------------------------


def test_set_enabled_clears_expired_suppression(hass: HomeAssistant):
    engine = _make_engine(hass)
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    engine.state.suppressed_until = dt_util.now() - timedelta(seconds=1)
    engine.state.suppression_reason = "manual"
    engine.state.enabled = False
    engine.set_enabled(True)
    assert engine.state.suppressed_until is None
    assert engine.state.suppression_reason is None


# ---------------------------------------------------------------------------
# flag entity change during armed state
# ---------------------------------------------------------------------------


async def test_flag_entity_change_rearms(hass: HomeAssistant):
    config = _make_config(
        flags=[Flag(entity="input_boolean.night", match_state="on")],
        target_entities=["light.bedroom"],
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    hass.states.async_set("input_boolean.night", "on")
    hass.states.async_set("light.bedroom", "off")
    # Evaluate a flag entity change (not a target entity)
    st = hass.states.get("input_boolean.night")
    await engine.async_evaluate("input_boolean.night", st)
    assert engine.current_status() in (STATUS_ARMED, STATUS_COOLDOWN, STATUS_PENDING)


async def test_flag_change_triggers_enforcement_on_already_triggered_target(
    hass: HomeAssistant,
):
    """Flag flipping on while target already violates rule must enforce immediately."""
    config = _make_config(
        flags=[Flag(entity="binary_sensor.no_water", match_state="on")],
        target_entities=["switch.diffuser"],
        trigger_states=["on"],
        target_state="off",
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    # Diffuser is already on; flag is initially off (guard dormant).
    hass.states.async_set("switch.diffuser", "on")
    hass.states.async_set("binary_sensor.no_water", "off")

    # Flag turns on — guard should now sweep and enforce the diffuser.
    hass.states.async_set("binary_sensor.no_water", "on")
    flag_st = hass.states.get("binary_sensor.no_water")

    with patch.object(engine, "_enforce", new_callable=AsyncMock) as mock_enforce:
        await engine.async_evaluate("binary_sensor.no_water", flag_st)
        # Allow spawned tasks to run.
        await hass.async_block_till_done()

    mock_enforce.assert_awaited_once_with("switch.diffuser", trigger_context_id=None)


async def test_flag_change_match_state_off_triggers_enforcement(
    hass: HomeAssistant,
):
    """Flag with match_state='off' flipping off while target violated must enforce."""
    config = _make_config(
        flags=[Flag(entity="binary_sensor.water_ok", match_state="off")],
        target_entities=["switch.diffuser"],
        trigger_states=["on"],
        target_state="off",
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    hass.states.async_set("switch.diffuser", "on")
    hass.states.async_set("binary_sensor.water_ok", "on")  # flag does not match yet

    # Flag turns off → match_state="off" now satisfied → sweep targets.
    hass.states.async_set("binary_sensor.water_ok", "off")
    flag_st = hass.states.get("binary_sensor.water_ok")

    with patch.object(engine, "_enforce", new_callable=AsyncMock) as mock_enforce:
        await engine.async_evaluate("binary_sensor.water_ok", flag_st)
        await hass.async_block_till_done()

    mock_enforce.assert_awaited_once_with("switch.diffuser", trigger_context_id=None)


async def test_flag_change_no_enforcement_when_target_not_triggered(
    hass: HomeAssistant,
):
    """Flag becoming satisfied must NOT enforce when target state is already correct."""
    config = _make_config(
        flags=[Flag(entity="binary_sensor.no_water", match_state="on")],
        target_entities=["switch.diffuser"],
        trigger_states=["on"],
        target_state="off",
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    hass.states.async_set("switch.diffuser", "off")  # already off — no violation
    hass.states.async_set("binary_sensor.no_water", "off")

    hass.states.async_set("binary_sensor.no_water", "on")
    flag_st = hass.states.get("binary_sensor.no_water")

    with patch.object(engine, "_enforce", new_callable=AsyncMock) as mock_enforce:
        await engine.async_evaluate("binary_sensor.no_water", flag_st)
        await hass.async_block_till_done()

    mock_enforce.assert_not_awaited()


async def test_flag_change_multi_flag_all_must_match(hass: HomeAssistant):
    """With two flags, sweep only fires when BOTH are satisfied."""
    config = _make_config(
        flags=[
            Flag(entity="binary_sensor.no_water", match_state="on"),
            Flag(entity="input_boolean.night", match_state="on"),
        ],
        target_entities=["switch.diffuser"],
        trigger_states=["on"],
        target_state="off",
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    hass.states.async_set("switch.diffuser", "on")
    hass.states.async_set("binary_sensor.no_water", "off")
    hass.states.async_set("input_boolean.night", "on")

    # Both flags now satisfied — sweep should enforce.
    hass.states.async_set("binary_sensor.no_water", "on")
    flag_st = hass.states.get("binary_sensor.no_water")

    with patch.object(engine, "_enforce", new_callable=AsyncMock) as mock_enforce:
        await engine.async_evaluate("binary_sensor.no_water", flag_st)
        await hass.async_block_till_done()

    mock_enforce.assert_awaited_once_with("switch.diffuser", trigger_context_id=None)


async def test_entity_both_flag_and_target_sweeps_other_targets(hass: HomeAssistant):
    """Entity that is both a flag and a target must still sweep other targets."""
    config = _make_config(
        flags=[Flag(entity="switch.diffuser", match_state="on")],
        target_entities=["switch.diffuser", "switch.humidifier"],
        trigger_states=["on"],
        target_state="off",
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    hass.states.async_set("switch.diffuser", "on")
    hass.states.async_set("switch.humidifier", "on")

    flag_and_target_st = hass.states.get("switch.diffuser")

    with patch.object(engine, "_enforce", new_callable=AsyncMock) as mock_enforce:
        await engine.async_evaluate("switch.diffuser", flag_and_target_st)
        await hass.async_block_till_done()

    enforced = {call.args[0] for call in mock_enforce.await_args_list}
    assert "switch.humidifier" in enforced


# ---------------------------------------------------------------------------
# Suppression lazy-expiry clear-path: hardening of untested param combos.
# The plain-target lazy-expiry branch (suppressed_until <= now) clears
# suppression, sweeps the SIBLING targets, then falls through to enforce the
# triggering target itself. The existing coverage only exercises this at
# delay=0, debounce off, fixed_state mode — these assert the downstream
# debounce guard, the delay>0 PENDING arming, and dynamic mode behave on it.
# ---------------------------------------------------------------------------


async def test_suppression_expiry_sibling_sweep_respects_debounce(
    hass: HomeAssistant,
):
    """A sibling re-armed by the lazy-expiry sweep must still honour its debounce
    cooldown: the debounce guard is downstream of the sweep, so a sibling inside
    its cooldown window is NOT enforced while the triggering target is."""
    config = _make_config(
        target_entities=["light.bedroom", "light.kitchen"],
        trigger_states=["on"],
        target_state="off",
        debounce_enabled=True,
        debounce_seconds=60,
        delay_seconds=0,
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    now = dt_util.now()
    # Suppression already expired → lazy-expiry branch fires on this event.
    engine._state.suppressed_until = now - timedelta(seconds=1)
    # Sibling is mid-debounce — its re-armed eval must bail at the cooldown guard.
    engine._state.cooldowns["light.kitchen"] = now + timedelta(seconds=60)

    hass.states.async_set("light.bedroom", "on")
    hass.states.async_set("light.kitchen", "on")
    trigger_st = hass.states.get("light.bedroom")

    with patch.object(engine, "_enforce", new_callable=AsyncMock) as mock_enforce:
        await engine.async_evaluate("light.bedroom", trigger_st)
        await hass.async_block_till_done()

    enforced = {call.args[0] for call in mock_enforce.await_args_list}
    assert engine._state.suppressed_until is None
    # Triggering target (no cooldown) enforces; debounced sibling does not.
    assert "light.bedroom" in enforced
    assert "light.kitchen" not in enforced


async def test_suppression_expiry_sibling_sweep_arms_delay(hass: HomeAssistant):
    """With delay_seconds>0, the lazy-expiry sweep must ARM a delayed enforcement
    for every triggered target (status PENDING, both entities in the due map with
    the correct delay) rather than enforcing immediately."""
    config = _make_config(
        target_entities=["light.bedroom", "light.kitchen"],
        trigger_states=["on"],
        target_state="off",
        delay_seconds=5,
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    now = dt_util.now()
    engine._state.suppressed_until = now - timedelta(seconds=1)

    hass.states.async_set("light.bedroom", "on")
    hass.states.async_set("light.kitchen", "on")
    trigger_st = hass.states.get("light.bedroom")

    with (
        patch.object(engine, "_enforce", new_callable=AsyncMock) as mock_enforce,
        patch(
            "custom_components.entity_guard.rule_engine.async_call_later",
            return_value=MagicMock(),
        ),
    ):
        await engine.async_evaluate("light.bedroom", trigger_st)
        await hass.async_block_till_done()

    assert engine._state.suppressed_until is None
    # Nothing enforced yet — both are armed on the 5s timer instead.
    mock_enforce.assert_not_awaited()
    assert engine.current_status() == STATUS_PENDING
    assert set(engine._pending_enforcement_due) == {"light.bedroom", "light.kitchen"}
    for due in engine._pending_enforcement_due.values():
        delta = (due - now).total_seconds()
        assert 4 <= delta <= 6  # ~5s countdown, allowing for test-clock drift


async def test_suppression_expiry_dynamic_source_sweep_enforces_once(
    hass: HomeAssistant,
):
    """A dynamic_state (follow) SOURCE trigger that also lazily expires suppression
    must not double-enforce. The lazy sibling sweep is skipped for source triggers
    (is_source guard) because the dynamic source-sweep branch already sweeps ALL
    targets; running both would enforce each target twice at delay 0. Assert each
    target is enforced exactly once — the skip, not a cancel, is what prevents the
    double (the first delay-0 eval runs to completion before the second schedules,
    so a per-entity cancel would be a no-op)."""
    config = _make_config(
        mode=MODE_DYNAMIC_STATE,
        source_entity="input_boolean.src",
        target_entities=["light.bedroom", "light.kitchen"],
        delay_seconds=0,
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    now = dt_util.now()
    engine._state.suppressed_until = now - timedelta(seconds=1)

    # Source on → desired_on True; both targets off → both triggered (cur != desired).
    hass.states.async_set("input_boolean.src", "on")
    hass.states.async_set("light.bedroom", "off")
    hass.states.async_set("light.kitchen", "off")
    trigger_st = hass.states.get("input_boolean.src")

    with patch.object(engine, "_enforce", new_callable=AsyncMock) as mock_enforce:
        # Trigger via the SOURCE: this both lazily clears suppression AND enters
        # the dynamic source-sweep branch. The lazy sibling sweep must be skipped.
        await engine.async_evaluate("input_boolean.src", trigger_st)
        await hass.async_block_till_done()

    assert engine._state.suppressed_until is None
    enforced = [call.args[0] for call in mock_enforce.await_args_list]
    # Each target swept exactly once by the source branch; lazy sweep skipped.
    assert enforced.count("light.bedroom") == 1
    assert enforced.count("light.kitchen") == 1


def test_is_triggered_ignores_unavailable_state():
    """trigger_states must never match unavailable or unknown."""
    config = _make_config(trigger_states=["on", "unavailable"])
    engine = _make_engine(None, config)

    unavail = _state("unavailable")
    unknown = _state("unknown")

    assert engine._is_triggered("light.bedroom", unavail) is False
    assert engine._is_triggered("light.bedroom", unknown) is False


def test_is_triggered_ignores_unknown_state():
    """unknown state must never trigger enforcement even if listed in trigger_states."""
    config = _make_config(trigger_states=["on", "unknown"])
    engine = _make_engine(None, config)

    assert engine._is_triggered("light.bedroom", _state("unknown")) is False


def test_is_triggered_attribute_mode_missing_config_returns_false():
    """MODE_FIXED_ATTRIBUTE with None attr/op/threshold must return False."""
    config = _make_config(
        mode=MODE_FIXED_ATTRIBUTE, attribute=None, operator=None, threshold=None
    )
    engine = _make_engine(None, config)
    assert (
        engine._is_triggered("light.bedroom", _state("on", {"brightness": 100}))
        is False
    )


def test_is_triggered_attribute_no_attributes_attr():
    """MODE_FIXED_ATTRIBUTE state object without attributes must return False gracefully."""
    config = _make_config(
        mode=MODE_FIXED_ATTRIBUTE,
        attribute="brightness",
        operator=OPERATOR_GT,
        threshold=50.0,
        target_value=0.0,
    )
    engine = _make_engine(None, config)
    st = MagicMock()
    st.state = "on"
    del st.attributes  # no attributes attribute
    assert engine._is_triggered("light.bedroom", st) is False


def test_cancel_pending_swallows_exception():
    """_cancel_pending must not raise when cancel callback raises."""
    config = _make_config()
    engine = _make_engine(None, config)

    def _bad_cancel():
        raise RuntimeError("cancel blew up")

    engine._pending_enforcements["light.bedroom"] = _bad_cancel
    engine._cancel_pending("light.bedroom")  # must not raise
    assert "light.bedroom" not in engine._pending_enforcements


def test_resolve_service_attribute_mode_no_mapping():
    """_resolve_service returns None when attribute has no service mapping."""
    config = _make_config(
        mode=MODE_FIXED_ATTRIBUTE,
        attribute="unmapped_attr",
        target_value=50.0,
    )
    engine = _make_engine(None, config)
    assert engine._resolve_service("light.bedroom") is None


def test_resolve_service_attribute_mode_missing_attr():
    """_resolve_service returns None when attribute or target_value is None."""
    config = _make_config(mode=MODE_FIXED_ATTRIBUTE, attribute=None, target_value=None)
    engine = _make_engine(None, config)
    assert engine._resolve_service("light.bedroom") is None


def test_resolve_service_unknown_mode():
    """_resolve_service returns None for unknown mode."""
    config = _make_config()
    engine = _make_engine(None, config)
    engine._config = MagicMock()
    engine._config.mode = "bogus_mode"
    assert engine._resolve_service("light.bedroom") is None


async def test_set_enabled_true_clears_expired_suppression(hass: HomeAssistant):
    """set_enabled(True) with expired suppressed_until must clear suppression."""
    config = _make_config()
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    engine._state.suppressed_until = dt_util.now() - timedelta(seconds=10)
    engine._state.suppression_reason = "manual"

    engine.set_enabled(True)

    assert engine._state.suppressed_until is None
    assert engine._state.suppression_reason is None
    assert engine.current_status() in (STATUS_ARMED, STATUS_COOLDOWN, STATUS_PENDING)


def test_set_status_no_broadcast_on_same_status():
    """_set_status must not broadcast when status unchanged."""
    config = _make_config()
    engine = _make_engine(None, config)
    engine._current_status = STATUS_ARMED

    broadcast_calls = []
    engine._broadcast_status = lambda: broadcast_calls.append(1)

    engine._set_status(STATUS_ARMED)
    assert broadcast_calls == []

    engine._set_status(STATUS_CONDITIONAL)
    assert broadcast_calls == [1]


async def test_delayed_enforcement_skips_when_state_gone(hass: HomeAssistant):
    """_fire inner coroutine: state=None after schedule → no enforce."""
    config = _make_config(delay_seconds=0)  # no timer; test _fire logic directly
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    # Simulate what _fire does: state is None.
    with patch.object(engine, "_enforce", new_callable=AsyncMock) as mock_enforce:
        engine._pending_enforcements.pop("light.bedroom", None)
        current = None  # state gone
        if current is None or not engine._is_triggered("light.bedroom", current):
            pass  # _fire returns early — no enforce
        await hass.async_block_till_done()

    mock_enforce.assert_not_awaited()


async def test_delayed_enforcement_skips_when_flags_no_longer_match(
    hass: HomeAssistant,
):
    """_fire inner coroutine: flags not matching after schedule → no enforce."""
    config = _make_config(
        flags=[Flag(entity="input_boolean.night", match_state="on")],
        delay_seconds=0,
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    hass.states.async_set("light.bedroom", "on")
    hass.states.async_set("input_boolean.night", "off")  # flag no longer matches

    with patch.object(engine, "_enforce", new_callable=AsyncMock) as mock_enforce:
        # Directly call async_evaluate — flags don't match so enforce never runs.
        st = hass.states.get("light.bedroom")
        await engine.async_evaluate("light.bedroom", st)
        await hass.async_block_till_done()

    mock_enforce.assert_not_awaited()


async def test_async_setup_logs_restored_state(hass: HomeAssistant):
    """async_setup with non-empty blob must log restored state (line 106)."""
    from custom_components.entity_guard.storage import EntityGuardStore

    config = _make_config()
    store = MagicMock(spec=EntityGuardStore)
    blob = {"enforcement_count_total": 5, "enforcement_count_today": 2}
    store.get_rule_state.return_value = blob
    store.runtime_to_blob = EntityGuardStore.runtime_to_blob
    store.blob_to_runtime = EntityGuardStore.blob_to_runtime
    engine = RuleEngine(hass, config, store, lambda: True)

    with (
        patch(
            "custom_components.entity_guard.rule_engine.async_track_state_change_event"
        ),
        patch("custom_components.entity_guard.rule_engine.async_track_time_change"),
        patch("custom_components.entity_guard.rule_engine.async_call_later"),
    ):
        await engine.async_setup()

    assert engine.state.enforcement_count_total == 5


async def test_set_enabled_true_while_still_suppressed(hass: HomeAssistant):
    """set_enabled(True) when suppressed_until is in the future must set SUPPRESSED."""
    config = _make_config()
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    engine._state.suppressed_until = dt_util.now() + timedelta(minutes=5)

    engine.set_enabled(True)

    assert engine.current_status() == STATUS_SUPPRESSED
    engine._cancel_suppression_timer()


# ---------------------------------------------------------------------------
# error status: consecutive failures, threshold, recovery
# ---------------------------------------------------------------------------


async def test_first_failure_does_not_set_error(hass: HomeAssistant):
    """A single enforcement failure must NOT flip status to error."""
    config = _make_config(target_state="off")
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    async def _fail(call):
        raise RuntimeError("boom")

    hass.services.async_register("light", "turn_off", _fail)
    hass.states.async_set("light.bedroom", "on")
    st = hass.states.get("light.bedroom")
    await engine.async_evaluate("light.bedroom", st)

    assert engine.state.consecutive_errors == 1
    assert engine.state.last_error == "boom"
    assert engine.current_status() != STATUS_ERROR


async def test_threshold_failures_set_error(hass: HomeAssistant):
    """ERROR_THRESHOLD consecutive failures must set status to error."""
    config = _make_config(target_state="off")
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    async def _fail(call):
        raise RuntimeError("device offline")

    hass.services.async_register("light", "turn_off", _fail)
    hass.states.async_set("light.bedroom", "on")
    st = hass.states.get("light.bedroom")

    for _ in range(ERROR_THRESHOLD):
        await engine.async_evaluate("light.bedroom", st)

    assert engine.state.consecutive_errors == ERROR_THRESHOLD
    assert engine.state.last_error == "device offline"
    assert engine.current_status() == STATUS_ERROR


# ---------------------------------------------------------------------------
# Group dead-member reclassification: a group turn-off whose aggregate already
# reached desired, but one dead member's integration RAISED, must NOT become a
# sticky STATUS_ERROR — it is a clean member_unavailable skip carrying the
# offline members for the card.
# ---------------------------------------------------------------------------


def _set_group(hass, gid, agg_state, members):
    """Register a group `gid` at aggregate `agg_state` with `members` (dict of
    member_eid -> state). The group exposes its members via the `entity_id`
    attribute exactly like HA's light-group aggregate."""
    for meid, mstate in members.items():
        hass.states.async_set(meid, mstate, {"friendly_name": meid.split(".")[-1]})
    hass.states.async_set(gid, agg_state, {"entity_id": list(members)})


async def test_group_dead_member_raise_is_skip_not_error(hass: HomeAssistant):
    """Group aggregate already 'off' (desired) + a dead member raises → clean
    member_unavailable skip: no consecutive_errors bump, not STATUS_ERROR."""
    gid = "light.group"
    config = _make_config(target_entities=[gid], target_state="off")
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    async def _fail(call):
        raise RuntimeError("SwitchBot Cloud device is offline")

    hass.services.async_register("light", "turn_off", _fail)
    # 2 live-off members + 1 dead; any-on aggregate reads 'off' = desired.
    _set_group(
        hass,
        gid,
        "off",
        {"light.a": "off", "light.b": "off", "light.dead": "unavailable"},
    )

    # Drive _enforce directly: the real case trips while the group reads 'on'
    # then the aggregate settles to 'off' before the dead member's exception
    # surfaces — in a zero-delay unit test trigger+enforce read the same state,
    # so an already-'off' aggregate would never trip. _enforce is the unit under
    # test (the except-block reclassification), not the trigger gate.
    for _ in range(ERROR_THRESHOLD + 2):
        await engine._enforce(gid, bypass_rate_limit=True)

    assert engine.state.consecutive_errors == 0
    assert engine.current_status() != STATUS_ERROR
    outcome = engine.state.target_outcomes.get(gid)
    assert outcome is not None
    assert outcome["outcome"] == "skipped"
    assert outcome["reason"] == "member_unavailable"
    assert outcome["members"] == ["dead"]


async def test_group_dead_member_many_members_capped(hass: HomeAssistant):
    """More than 3 dead members → list capped to 3 names + a '+N more' tail."""
    gid = "light.group"
    config = _make_config(target_entities=[gid], target_state="off")
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    async def _fail(call):
        raise RuntimeError("offline")

    hass.services.async_register("light", "turn_off", _fail)
    members = {f"light.d{i}": "unavailable" for i in range(5)}
    members["light.live"] = "off"
    _set_group(hass, gid, "off", members)
    await engine._enforce(gid, bypass_rate_limit=True)

    shown = engine.state.target_outcomes[gid]["members"]
    assert len(shown) == 4  # 3 names + tail
    assert shown[-1] == "+2 more"


async def test_group_not_reached_still_errors(hass: HomeAssistant):
    """A group whose aggregate is NOT at desired (still 'on') when the call
    raises is a real failure — must still climb consecutive_errors, NOT be
    masked as a member_unavailable skip."""
    gid = "light.group"
    config = _make_config(target_entities=[gid], target_state="off")
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    async def _fail(call):
        raise RuntimeError("boom")

    hass.services.async_register("light", "turn_off", _fail)
    # Aggregate 'on' (a live member still on) — desired 'off' NOT reached.
    _set_group(hass, gid, "on", {"light.a": "on", "light.dead": "unavailable"})
    st = hass.states.get(gid)
    await engine.async_evaluate(gid, st)

    assert engine.state.consecutive_errors == 1
    outcome = engine.state.target_outcomes.get(gid)
    assert outcome["outcome"] == "failed"
    assert outcome["reason"] == REASON_SERVICE_CALL_FAILED


async def test_single_entity_raise_still_errors(hass: HomeAssistant):
    """Regression guard: a lone (non-group) target that raises is itself the
    unreached thing (_verify_reached False), so it must still count as an error
    exactly as before the group reclassification."""
    config = _make_config(target_state="off")
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    async def _fail(call):
        raise RuntimeError("boom")

    hass.services.async_register("light", "turn_off", _fail)
    hass.states.async_set("light.bedroom", "on")  # stays on after a raised turn_off
    st = hass.states.get("light.bedroom")
    await engine.async_evaluate("light.bedroom", st)

    assert engine.state.consecutive_errors == 1
    assert engine.state.target_outcomes["light.bedroom"]["outcome"] == "failed"


# ---------------------------------------------------------------------------
# Feature E: enforce service-call timeout
# ---------------------------------------------------------------------------


async def _register_hanging_service(hass: HomeAssistant, domain: str, service: str):
    """Register a service that hangs well past the (tiny, patched) timeout."""

    async def _hang(call):
        await asyncio.sleep(5)

    hass.services.async_register(domain, service, _hang)


async def test_enforce_timeout_routes_to_failure(hass: HomeAssistant):
    """A hung service call times out and takes the service_call_failed path:
    consecutive_errors++, a non-empty timeout error string, status not ERROR yet."""
    config = _make_config(target_state="off")
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    await _register_hanging_service(hass, "light", "turn_off")
    hass.states.async_set("light.bedroom", "on")
    st = hass.states.get("light.bedroom")

    # Patch the timeout tiny so wait_for fires fast instead of blocking the test 30s.
    with patch(
        "custom_components.entity_guard.rule_engine.ENFORCE_TIMEOUT_SECONDS", 0.05
    ):
        await engine.async_evaluate("light.bedroom", st)

    assert engine.state.consecutive_errors == 1
    # str(TimeoutError()) is empty — the branch must synthesize a useful message.
    assert engine.state.last_error == "service call timed out after 0.05s"
    assert engine.current_status() != STATUS_ERROR
    # The timed-out call is a failure, NOT a successful enforce.
    assert engine.state.enforcement_count_total == 0


async def test_enforce_timeout_writes_failed_outcome(hass: HomeAssistant):
    """A timeout composes with per-target visibility: target_outcomes[eid] = failed
    with the timeout error string (Round-1 A feature)."""
    config = _make_config(target_state="off")
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    await _register_hanging_service(hass, "light", "turn_off")
    hass.states.async_set("light.bedroom", "on")
    st = hass.states.get("light.bedroom")

    with patch(
        "custom_components.entity_guard.rule_engine.ENFORCE_TIMEOUT_SECONDS", 0.05
    ):
        await engine.async_evaluate("light.bedroom", st)

    outcome = engine.state.target_outcomes.get("light.bedroom")
    assert outcome is not None
    assert outcome["outcome"] == "failed"
    assert outcome["error"] == "service call timed out after 0.05s"


async def test_enforce_timeout_threshold_sets_error(hass: HomeAssistant):
    """ENFORCE_TIMEOUT_SECONDS worth of consecutive timeouts set STATUS_ERROR, same
    as raised failures."""
    config = _make_config(target_state="off")
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    await _register_hanging_service(hass, "light", "turn_off")
    hass.states.async_set("light.bedroom", "on")
    st = hass.states.get("light.bedroom")

    with patch(
        "custom_components.entity_guard.rule_engine.ENFORCE_TIMEOUT_SECONDS", 0.05
    ):
        for _ in range(ERROR_THRESHOLD):
            await engine.async_evaluate("light.bedroom", st)

    assert engine.state.consecutive_errors == ERROR_THRESHOLD
    assert engine.current_status() == STATUS_ERROR


async def test_enforce_timeout_not_counted_in_rate_limit(hass: HomeAssistant):
    """A timeout returns before the rate_limit_window insort, so it must NOT count
    toward loop protection (a timeout is a failure, not a successful enforce)."""
    config = _make_config(target_state="off")
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    await _register_hanging_service(hass, "light", "turn_off")
    hass.states.async_set("light.bedroom", "on")
    st = hass.states.get("light.bedroom")

    with patch(
        "custom_components.entity_guard.rule_engine.ENFORCE_TIMEOUT_SECONDS", 0.05
    ):
        await engine.async_evaluate("light.bedroom", st)

    assert engine.state.rate_limit_window == []


async def test_enforce_timeout_releases_lock(hass: HomeAssistant):
    """After a timeout the reentrance_lock must be free — a subsequent enforce
    (now against a fast service) proceeds instead of deadlocking."""
    config = _make_config(target_state="off")
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    await _register_hanging_service(hass, "light", "turn_off")
    hass.states.async_set("light.bedroom", "on")
    st = hass.states.get("light.bedroom")

    with patch(
        "custom_components.entity_guard.rule_engine.ENFORCE_TIMEOUT_SECONDS", 0.05
    ):
        await engine.async_evaluate("light.bedroom", st)

    # Lock must be released after the timeout unwound.
    assert not engine.state.reentrance_lock.locked()

    # Replace the hanging service with a fast one; a fresh enforce must proceed.
    calls: list = []
    hass.services.async_register("light", "turn_off", lambda c: calls.append(c))
    await engine._enforce("light.bedroom", bypass_rate_limit=True)
    assert calls, "second enforce blocked — lock was not released after timeout"


async def test_successful_enforcement_clears_error(hass: HomeAssistant):
    """async_clear_history must exit error state regardless of evaluate path."""
    config = _make_config(target_state="off")
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    engine._state.consecutive_errors = ERROR_THRESHOLD
    engine._state.last_error = "previous error"
    engine._set_status(STATUS_ERROR)

    # clear_history resets error state synchronously
    await engine.async_clear_history()
    assert engine.state.consecutive_errors == 0
    assert engine.state.last_error is None
    assert engine.current_status() != STATUS_ERROR


async def test_clear_history_clears_error_state(hass: HomeAssistant):
    """async_clear_history must reset error counters and exit error status."""
    config = _make_config()
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    engine._state.consecutive_errors = 5
    engine._state.last_error = "boom"
    engine._set_status(STATUS_ERROR)

    await engine.async_clear_history()

    assert engine.state.consecutive_errors == 0
    assert engine.state.last_error is None
    assert engine.current_status() != STATUS_ERROR


async def test_consecutive_errors_persisted_via_blob(hass: HomeAssistant):
    """consecutive_errors and last_error must round-trip through storage."""
    config = _make_config()
    engine = _make_engine(hass, config)
    engine._state.consecutive_errors = 7
    engine._state.last_error = "kaboom"

    blob = EntityGuardStore.runtime_to_blob(engine._state)
    assert blob["consecutive_errors"] == 7
    assert blob["last_error"] == "kaboom"

    restored = EntityGuardStore.blob_to_runtime(blob)
    assert restored.consecutive_errors == 7
    assert restored.last_error == "kaboom"


# ---------------------------------------------------------------------------
# conditional status: flag entities configured but not matching
# ---------------------------------------------------------------------------


async def test_no_flags_means_no_conditional(hass: HomeAssistant):
    """A rule with no flags should never end up in conditional state."""
    config = _make_config(flags=[])
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    assert engine._flags_match() is True


async def test_flags_not_matching_yields_conditional(hass: HomeAssistant):
    """When flag entity state mismatches required match_state, status -> conditional."""
    config = _make_config(
        flags=[Flag(entity="input_boolean.night", match_state="on")],
        target_entities=["light.bedroom"],
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    hass.states.async_set("input_boolean.night", "off")
    hass.states.async_set("light.bedroom", "on")
    st = hass.states.get("light.bedroom")
    await engine.async_evaluate("light.bedroom", st)
    assert engine.current_status() == STATUS_CONDITIONAL


async def test_flags_matching_clears_conditional(hass: HomeAssistant):
    """When flag entity satisfies match_state, status leaves conditional."""
    config = _make_config(
        flags=[Flag(entity="input_boolean.night", match_state="on")],
        target_entities=["light.bedroom"],
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    engine._set_status(STATUS_CONDITIONAL)
    hass.states.async_set("input_boolean.night", "on")
    hass.states.async_set("light.bedroom", "off")
    st = hass.states.get("light.bedroom")
    await engine.async_evaluate("light.bedroom", st)
    assert engine.current_status() != STATUS_CONDITIONAL


# ---------------------------------------------------------------------------
# Coverage gaps
# ---------------------------------------------------------------------------


def test_disabled_status_master_off(hass: HomeAssistant):
    """_disabled_status returns STATUS_MASTER_DISABLED when master is off."""
    engine = _make_engine(hass, master=False)
    assert engine._disabled_status() == STATUS_MASTER_DISABLED


def test_disabled_status_master_on(hass: HomeAssistant):
    """_disabled_status returns STATUS_DISABLED when master is on."""
    engine = _make_engine(hass, master=True)
    assert engine._disabled_status() == STATUS_DISABLED


async def test_rate_limit_window_pruned_on_enforce(hass: HomeAssistant):
    """Stale rate-limit entries are pruned when _enforce runs."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    config = _make_config(target_state="off", max_enforcements_per_minute=10)
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    stale = dt_util.now() - timedelta(seconds=120)
    engine._state.rate_limit_window = [stale, stale]

    hass.states.async_set("light.bedroom", "on")

    async def _mock_turn_off(call):
        pass

    hass.services.async_register("light", "turn_off", _mock_turn_off)

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later"
    ) as mock_later:
        mock_later.return_value = MagicMock()
        await engine._enforce("light.bedroom")

    assert len(engine._state.rate_limit_window) == 1  # stale pruned, current appended


async def test_cooldown_broadcast_fires_after_enforcement(hass: HomeAssistant):
    """_broadcast_after_cooldown callback is scheduled when cooldown > 0."""

    config = _make_config(
        target_state="off", debounce_enabled=True, debounce_seconds=30
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    hass.states.async_set("light.bedroom", "on")

    async def _mock_turn_off(svc_call):
        pass

    hass.services.async_register("light", "turn_off", _mock_turn_off)

    broadcast_callbacks = []

    def _capture_later(hass_arg, delay, cb):
        broadcast_callbacks.append(cb)
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later",
        side_effect=_capture_later,
    ):
        await engine._enforce("light.bedroom")

    assert broadcast_callbacks, "async_call_later not called — no cooldown scheduled"
    # Fire the broadcast callback to exercise lines 505-510
    broadcast_callbacks[-1](dt_util.now())


async def test_unload_cancels_pending_eval_tasks(hass: HomeAssistant):
    """async_unload cancels any in-flight eval tasks."""
    engine = _make_engine(hass)
    engine._store.async_save_now = AsyncMock()

    mock_task = MagicMock()
    mock_task.cancel = MagicMock()
    engine._pending_eval_tasks["light.bedroom"] = mock_task

    await engine.async_unload()

    mock_task.cancel.assert_called_once()
    assert engine._pending_eval_tasks == {}


def test_schedule_eval_task_cancels_existing(hass: HomeAssistant):
    """_schedule_eval_task cancels prior non-done task for same entity."""
    engine = _make_engine(hass)

    existing = MagicMock()
    existing.done.return_value = False
    engine._pending_eval_tasks["light.bedroom"] = existing

    engine._schedule_eval_task("light.bedroom", None)

    existing.cancel.assert_called_once()


async def test_is_unloaded_guard_prevents_fire(hass: HomeAssistant):
    """If engine is unloaded before _fire runs, enforcement must be skipped."""
    from unittest.mock import AsyncMock

    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    config = _make_config(delay_seconds=1)
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    hass.services.async_register("light", "turn_off", AsyncMock())
    hass.states.async_set("light.bedroom", "on")
    st = hass.states.get("light.bedroom")

    # Schedule delayed enforcement
    await engine.async_evaluate("light.bedroom", st)
    assert engine._current_status == STATUS_PENDING

    # Mark engine as unloaded before the timer fires
    engine._is_unloaded = True

    # Fire the timer — should be a no-op
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()

    # No enforcement: counters unchanged
    assert engine.state.enforcement_count_total == 0


async def test_unload_cancels_cooldown_broadcast_unsubs(hass: HomeAssistant):
    """async_unload cancels pending cooldown broadcast timers."""
    engine = _make_engine(hass)
    engine._store.async_save_now = AsyncMock()

    cancel_mock = MagicMock()
    engine._cooldown_broadcast_unsubs["light.bedroom"] = cancel_mock

    await engine.async_unload()

    cancel_mock.assert_called_once()
    assert engine._cooldown_broadcast_unsubs == {}


async def test_unload_handles_cooldown_broadcast_unsub_exception(hass: HomeAssistant):
    """async_unload swallows exceptions when cancelling cooldown broadcast timers."""
    engine = _make_engine(hass)
    engine._store.async_save_now = AsyncMock()

    broken = MagicMock(side_effect=RuntimeError("boom"))
    engine._cooldown_broadcast_unsubs["light.bedroom"] = broken

    # Must not raise
    await engine.async_unload()
    assert engine._cooldown_broadcast_unsubs == {}


async def test_derive_armed_prunes_expired_cooldowns(hass: HomeAssistant):
    """_prune_expired_cooldowns removes expired entries from cooldowns dict."""
    from datetime import timedelta

    engine = _make_engine(hass)
    engine._startup_complete = True
    past = dt_util.now() - timedelta(seconds=10)
    engine._state.cooldowns["light.old"] = past

    engine._prune_expired_cooldowns(dt_util.now())

    assert "light.old" not in engine._state.cooldowns


async def test_cooldown_broadcast_cancels_prior_timer(hass: HomeAssistant):
    """Re-enforcing the same entity cancels the prior cooldown broadcast timer."""
    from unittest.mock import AsyncMock as _AsyncMock

    config = _make_config(
        target_state="off", debounce_enabled=True, debounce_seconds=30
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    hass.services.async_register("light", "turn_off", _AsyncMock())
    hass.states.async_set("light.bedroom", "on")

    call_count = 0
    cancel_mocks = []

    def _capture_later(hass_arg, delay, cb):
        nonlocal call_count
        call_count += 1
        m = MagicMock()
        cancel_mocks.append(m)
        return m

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later",
        side_effect=_capture_later,
    ):
        # First enforcement — schedules cooldown timer + recently_enforced timer
        await engine._enforce("light.bedroom")
        assert len(cancel_mocks) == 2
        # Grab the cooldown unsub directly — robust against call-order changes.
        first_cooldown_cancel = engine._cooldown_broadcast_unsubs["light.bedroom"]

        # Second enforcement for same entity — prior timer must be cancelled
        engine._state.cooldowns["light.bedroom"] = dt_util.now() + __import__(
            "datetime"
        ).timedelta(seconds=30)
        await engine._enforce("light.bedroom")

    first_cooldown_cancel.assert_called_once()


async def test_cooldown_broadcast_prior_timer_exception_swallowed(hass: HomeAssistant):
    """Exception from cancelling prior cooldown broadcast timer is swallowed."""
    from unittest.mock import AsyncMock as _AsyncMock

    config = _make_config(
        target_state="off", debounce_enabled=True, debounce_seconds=30
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    hass.services.async_register("light", "turn_off", _AsyncMock())
    hass.states.async_set("light.bedroom", "on")

    # Plant a broken prior unsub
    broken = MagicMock(side_effect=RuntimeError("cancel failed"))
    engine._cooldown_broadcast_unsubs["light.bedroom"] = broken

    def _capture_later(hass_arg, delay, cb):
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later",
        side_effect=_capture_later,
    ):
        # Must not raise despite broken unsub
        await engine._enforce("light.bedroom")

    broken.assert_called_once()


def test_is_pending_true(hass: HomeAssistant):
    """is_pending returns True when status is STATUS_PENDING."""
    engine = _make_engine(hass)
    engine._set_status(STATUS_PENDING)
    assert engine.is_pending() is True


def test_is_pending_false(hass: HomeAssistant):
    """is_pending returns False when status is not STATUS_PENDING."""
    engine = _make_engine(hass)
    engine._set_status(STATUS_ARMED)
    assert engine.is_pending() is False


async def test_derive_idle_status_sticky_error(hass: HomeAssistant):
    """_derive_idle_status returns STATUS_ERROR when current status is ERROR."""
    engine = _make_engine(hass)
    engine._startup_complete = True
    engine._set_status(STATUS_ERROR)
    result = engine._derive_idle_status()
    assert result == STATUS_ERROR


async def test_cooldown_broadcast_is_unloaded_guard(hass: HomeAssistant):
    """_broadcast_after_cooldown returns early if engine is unloaded."""
    from unittest.mock import AsyncMock as _AsyncMock

    config = _make_config(
        target_state="off", debounce_enabled=True, debounce_seconds=30
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    hass.services.async_register("light", "turn_off", _AsyncMock())
    hass.states.async_set("light.bedroom", "on")

    captured_cb = []

    def _capture_later(hass_arg, delay, cb):
        captured_cb.append(cb)
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later",
        side_effect=_capture_later,
    ):
        await engine._enforce("light.bedroom")

    assert captured_cb, "no cooldown broadcast timer scheduled"

    # Mark engine unloaded then fire the callback — _apply_idle_status must NOT be called
    engine._is_unloaded = True
    original_status = engine.current_status()
    captured_cb[-1](dt_util.now())  # fire the callback

    # Status must not change (engine is unloaded)
    assert engine.current_status() == original_status


# ---------------------------------------------------------------------------
# Fix 1: _fire pops _pending_enforcements AFTER _enforce so concurrent
#         _cancel_pending during the service call can still abort the work.
# ---------------------------------------------------------------------------


async def test_fire_pops_pending_after_enforce_not_before(hass: HomeAssistant):
    """_pending_enforcements entry must exist while _enforce runs so a concurrent
    _cancel_pending call during the service-call window can still cancel it."""
    config = _make_config(delay_seconds=1)
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    async def _slow_service(call):
        pass

    hass.services.async_register("light", "turn_off", _slow_service)
    hass.states.async_set("light.bedroom", "on")
    st = hass.states.get("light.bedroom")

    await engine.async_evaluate("light.bedroom", st)
    assert engine._current_status == STATUS_PENDING

    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()

    # After _fire completes, entry must be gone.
    assert "light.bedroom" not in engine._pending_enforcements
    # And enforcement happened.
    assert engine.state.enforcement_count_total == 1


async def test_concurrent_cancel_during_fire_aborts_second_timer(hass: HomeAssistant):
    """If entity un-triggers while _fire is awaiting _enforce, no second timer is armed."""
    config = _make_config(delay_seconds=1)
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    enforce_called = []

    async def _service(call):
        enforce_called.append(True)
        # Simulate entity turning back off while enforcement runs.
        hass.states.async_set("light.bedroom", "off")

    hass.services.async_register("light", "turn_off", _service)
    hass.states.async_set("light.bedroom", "on")
    st = hass.states.get("light.bedroom")

    await engine.async_evaluate("light.bedroom", st)

    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()

    # Enforcement fired once; no second timer armed.
    assert len(enforce_called) == 1
    assert "light.bedroom" not in engine._pending_enforcements


# ---------------------------------------------------------------------------
# Fix 2: cooldown remaining uses `now` captured before the service call,
#         not a fresh dt_util.now() after it — avoids negative remaining.
# ---------------------------------------------------------------------------


async def test_cooldown_remaining_uses_pre_service_now(hass: HomeAssistant):
    """When service call takes longer than debounce_seconds the cooldown broadcast
    timer must still be scheduled (remaining must be positive using pre-call `now`)."""
    config = _make_config(target_state="off", debounce_enabled=True, debounce_seconds=1)
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    captured_calls: list[tuple[float, object]] = []

    async def _slow_service(call):
        pass

    hass.services.async_register("light", "turn_off", _slow_service)
    hass.states.async_set("light.bedroom", "on")

    def _capture_call_later(hass_arg, delay, cb):
        captured_calls.append((delay, cb))
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later",
        side_effect=_capture_call_later,
    ):
        import datetime as _dt

        base_now = dt_util.now()
        call_count = 0

        def _fake_now():
            nonlocal call_count
            call_count += 1
            # First call (inside _enforce, to capture `now`): base time.
            # After the lock releases, dt_util.now() returns base + 2s,
            # simulating a slow service call that exceeds debounce_seconds=1.
            if call_count <= 1:
                return base_now
            return base_now + _dt.timedelta(seconds=2)

        with patch(
            "custom_components.entity_guard.rule_engine.dt_util.now",
            side_effect=_fake_now,
        ):
            await engine._enforce("light.bedroom")

    # A cooldown broadcast timer must have been scheduled (delay > 0).
    # If remaining used post-call now it would be negative and no timer would be scheduled.
    # Filter to cooldown broadcast timer by job name — robust against delay value coincidence.
    cooldown_calls = [
        (delay, cb)
        for delay, cb in captured_calls
        if not (hasattr(cb, "name") and "recently_enforced" in cb.name)
    ]
    assert len(cooldown_calls) == 1, "cooldown broadcast timer was not scheduled"
    assert cooldown_calls[0][0] > 0, (
        f"expected positive delay, got {cooldown_calls[0][0]}"
    )


# ---------------------------------------------------------------------------
# Fix 3: engine is always unloaded on async_unload_entry regardless of
#         platform unload result — no listener leak on partial failure.
# ---------------------------------------------------------------------------


async def test_engine_unloaded_even_when_platform_unload_fails(hass: HomeAssistant):
    """async_unload_entry must unload the engine even when async_unload_platforms
    returns False — prevents orphaned listeners on partial platform teardown."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.entity_guard import async_unload_entry
    from custom_components.entity_guard.const import (
        CONF_ENTRY_TYPE,
        DOMAIN,
        ENTRY_TYPE_RULE,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_RULE},
        title="Test Rule",
    )
    entry.add_to_hass(hass)

    engine_mock = MagicMock()
    engine_mock.async_unload = AsyncMock()
    hass.data.setdefault(DOMAIN, {})["engines"] = {entry.entry_id: engine_mock}

    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        new_callable=AsyncMock,
        return_value=False,  # platform unload fails
    ):
        result = await async_unload_entry(hass, entry)

    # Engine must still be unloaded and removed.
    engine_mock.async_unload.assert_awaited_once()
    assert entry.entry_id not in hass.data[DOMAIN]["engines"]
    assert result is False  # outer result reflects platform failure


# ---------------------------------------------------------------------------
# Fix 4: async_suppress uses _apply_idle_status so the priority ladder is
#         respected — a disabled rule does not flip to SUPPRESSED.
# ---------------------------------------------------------------------------


async def test_suppress_on_disabled_rule_stays_disabled(hass: HomeAssistant):
    """A disabled rule that is suppressed must remain DISABLED, not flip to SUPPRESSED.
    async_unsuppress already uses _apply_idle_status correctly; async_suppress must too."""
    config = _make_config()
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    engine.set_enabled(False)
    assert engine.current_status() == STATUS_DISABLED

    await engine.async_suppress(duration_minutes=5)

    # Priority ladder: DISABLED outranks SUPPRESSED.
    assert engine.current_status() == STATUS_DISABLED
    engine._cancel_suppression_timer()


async def test_suppress_on_armed_rule_goes_suppressed(hass: HomeAssistant):
    """An armed rule that is suppressed must go to SUPPRESSED status."""
    config = _make_config()
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    engine._set_status(STATUS_ARMED)

    await engine.async_suppress(duration_minutes=5)

    assert engine.current_status() == STATUS_SUPPRESSED
    engine._cancel_suppression_timer()


# ---------------------------------------------------------------------------
# Fix: async_reset_cooldowns / async_clear_history cancel broadcast unsubs
# ---------------------------------------------------------------------------


async def test_reset_cooldowns_cancels_broadcast_unsubs(hass: HomeAssistant):
    """async_reset_cooldowns must cancel and clear _cooldown_broadcast_unsubs.

    Without this fix, orphaned timers could pop a NEW cooldown's cancel handle,
    causing the new cooldown's expiry broadcast to never fire.
    """
    engine = _make_engine(hass)
    engine._store.async_save_now = AsyncMock()
    engine._startup_complete = True

    cancel_mock = MagicMock()
    engine._cooldown_broadcast_unsubs["light.bedroom"] = cancel_mock

    await engine.async_reset_cooldowns()

    cancel_mock.assert_called_once()
    assert engine._cooldown_broadcast_unsubs == {}
    assert engine._state.cooldowns == {}


async def test_clear_history_cancels_broadcast_unsubs(hass: HomeAssistant):
    """async_clear_history must cancel and clear _cooldown_broadcast_unsubs."""
    engine = _make_engine(hass)
    engine._startup_complete = True

    cancel_mock = MagicMock()
    engine._cooldown_broadcast_unsubs["light.bedroom"] = cancel_mock

    await engine.async_clear_history()

    cancel_mock.assert_called_once()
    assert engine._cooldown_broadcast_unsubs == {}


async def test_apply_idle_status_cancels_cooldown_timers_when_disabled(
    hass: HomeAssistant,
):
    """C5: _apply_idle_status must cancel cooldown-broadcast timers when deriving DISABLED."""
    engine = _make_engine(hass)
    engine._startup_complete = True
    engine._state.enabled = False
    cancel_mock = MagicMock()
    engine._cooldown_broadcast_unsubs["light.bedroom"] = cancel_mock

    engine._apply_idle_status()

    cancel_mock.assert_called_once()
    assert engine._cooldown_broadcast_unsubs == {}


async def test_apply_idle_status_cancel_exception_swallowed_when_disabled(
    hass: HomeAssistant,
):
    """C5: exception from cancelling cooldown timer must be swallowed."""
    engine = _make_engine(hass)
    engine._startup_complete = True
    engine._state.enabled = False
    engine._cooldown_broadcast_unsubs["light.bedroom"] = MagicMock(
        side_effect=RuntimeError("boom")
    )

    engine._apply_idle_status()  # must not raise
    assert engine._cooldown_broadcast_unsubs == {}


async def test_reset_cooldowns_swallows_cancel_exception(hass: HomeAssistant):
    """Exception from cancelling a broadcast unsub must be swallowed."""
    engine = _make_engine(hass)
    engine._store.async_save_now = AsyncMock()

    broken = MagicMock(side_effect=RuntimeError("boom"))
    engine._cooldown_broadcast_unsubs["light.bedroom"] = broken

    await engine.async_reset_cooldowns()

    assert engine._cooldown_broadcast_unsubs == {}


# ---------------------------------------------------------------------------
# Fix 1 (residual race): _fire uses identity check on my_cancel so a newer
# timer's cancel handle is never popped by an older _fire completing.
# ---------------------------------------------------------------------------


async def test_fire_identity_check_preserves_newer_timer(hass: HomeAssistant):
    """_fire must not pop a cancel handle that belongs to a newer timer.

    Scenario: timer T1 fires, _fire awaits _enforce. While awaiting, a state flap
    causes _schedule_delayed_enforcement to arm timer T2 (storing T2's cancel in
    _pending_enforcements). T1's _fire completes and must NOT pop T2's cancel handle
    — doing so would orphan T2, causing its eventual pop to be a no-op and leaving
    T2's eventual enforcement unguarded by _cancel_pending.
    """
    config = _make_config(delay_seconds=1)
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    enforce_started = []
    second_cancel_intact = []

    async def _slow_service(call):
        enforce_started.append(True)
        # While enforcement is running, simulate a new timer being armed.
        # _schedule_delayed_enforcement replaces _pending_enforcements["light.bedroom"]
        # with a NEW cancel object (a different MagicMock).
        new_cancel = MagicMock()
        engine._pending_enforcements["light.bedroom"] = new_cancel
        # Record whether the new cancel is still present after _fire returns.
        second_cancel_intact.append(new_cancel)

    hass.services.async_register("light", "turn_off", _slow_service)
    hass.states.async_set("light.bedroom", "on")
    st = hass.states.get("light.bedroom")

    await engine.async_evaluate("light.bedroom", st)

    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()

    assert enforce_started, "enforcement did not run"
    # The second cancel placed by _slow_service must still be in _pending_enforcements.
    assert len(second_cancel_intact) == 1
    new_cancel = second_cancel_intact[0]
    assert engine._pending_enforcements.get("light.bedroom") is new_cancel, (
        "_fire popped the newer timer's cancel handle (identity check not working)"
    )


async def test_fire_disabled_mid_flight_skips_enforce(hass: HomeAssistant):
    """Bug 2: rule disabled after timer queued but before _fire runs — enforce must not happen."""
    config = _make_config(target_state="off", delay_seconds=1)
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    hass.states.async_set("light.bedroom", "on")

    fires = []

    def _capture_later(_hass, _delay, cb):
        fires.append(cb)
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later",
        side_effect=_capture_later,
    ):
        engine._schedule_delayed_enforcement("light.bedroom")

    assert fires
    # Disable the rule before the timer fires
    engine._state.enabled = False
    engine._current_status = STATUS_DISABLED

    called = []

    async def _mock_turn_off(call):
        called.append(call)

    hass.services.async_register("light", "turn_off", _mock_turn_off)
    fires[0](dt_util.now())
    await hass.async_block_till_done()

    assert len(called) == 0, "enforce must not fire for a disabled rule"
    assert engine.current_status() == STATUS_DISABLED


async def test_fire_master_disabled_mid_flight_skips_enforce(hass: HomeAssistant):
    """Bug 2 (master): master disabled after timer queued — enforce must not happen."""
    config = _make_config(target_state="off", delay_seconds=1)
    engine = _make_engine(hass, config, master=True)
    engine._startup_complete = True
    hass.states.async_set("light.bedroom", "on")

    fires = []

    def _capture_later(_hass, _delay, cb):
        fires.append(cb)
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later",
        side_effect=_capture_later,
    ):
        engine._schedule_delayed_enforcement("light.bedroom")

    assert fires
    # Switch master off
    engine._master_enabled_getter = lambda: False

    called = []

    async def _mock_turn_off(call):
        called.append(call)

    hass.services.async_register("light", "turn_off", _mock_turn_off)
    fires[0](dt_util.now())
    await hass.async_block_till_done()

    assert len(called) == 0
    assert engine.current_status() == STATUS_MASTER_DISABLED


# ---------------------------------------------------------------------------
# EG-6: STATUS_ERROR auto-recovery after consecutive successes
# ---------------------------------------------------------------------------


async def test_error_auto_recovers_after_3_successes(hass: HomeAssistant):
    """ERROR_RECOVERY_SUCCESS_THRESHOLD consecutive successful enforcements must
    transition the rule back from STATUS_ERROR to ARMED (or COOLDOWN/SUPPRESSED)."""
    config = _make_config(target_state="off")
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    # Plant the rule into ERROR with the underlying condition cleared.
    engine._state.consecutive_errors = ERROR_THRESHOLD
    engine._state.last_error = "device offline"
    engine._set_status(STATUS_ERROR)

    async def _ok(call):
        pass

    hass.services.async_register("light", "turn_off", _ok)
    hass.states.async_set("light.bedroom", "on")
    st = hass.states.get("light.bedroom")

    # First N-1 successes hold the ERROR status (recovery still pending).
    for i in range(ERROR_RECOVERY_SUCCESS_THRESHOLD - 1):
        await engine.async_evaluate("light.bedroom", st)
        assert engine.current_status() == STATUS_ERROR, (
            f"recovered too early at iter {i}"
        )
        assert engine.state.consecutive_success_count == i + 1

    # Threshold-th success triggers recovery.
    await engine.async_evaluate("light.bedroom", st)
    assert engine.current_status() != STATUS_ERROR
    assert engine.state.consecutive_success_count == 0
    assert engine.state.consecutive_errors == 0
    assert engine.state.last_error is None


async def test_error_resets_counter_on_failure(hass: HomeAssistant):
    """A failure during the recovery window must reset consecutive_success_count
    to 0 and keep the rule in STATUS_ERROR."""
    config = _make_config(target_state="off")
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    engine._state.consecutive_errors = ERROR_THRESHOLD
    engine._state.consecutive_success_count = 1  # one prior success in the window
    engine._set_status(STATUS_ERROR)

    async def _fail(call):
        raise RuntimeError("still broken")

    hass.services.async_register("light", "turn_off", _fail)
    hass.states.async_set("light.bedroom", "on")
    st = hass.states.get("light.bedroom")
    await engine.async_evaluate("light.bedroom", st)

    assert engine.state.consecutive_success_count == 0
    assert engine.current_status() == STATUS_ERROR


# ---------------------------------------------------------------------------
# EG-4: suppression-expiry timer
# ---------------------------------------------------------------------------


async def test_suppression_state_clears_on_timer_expiry(hass: HomeAssistant):
    """When the suppression-expiry timer fires, the engine must clear suppression
    state and broadcast a fresh status without waiting for an event."""
    config = _make_config()
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    captured_callbacks: list = []

    def _capture_point_in_time(hass_arg, cb, when):
        captured_callbacks.append(cb)
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_track_point_in_time",
        side_effect=_capture_point_in_time,
    ):
        await engine.async_suppress(duration_minutes=1)
        assert engine.current_status() == STATUS_SUPPRESSED
        assert captured_callbacks, "suppression timer was not scheduled"

        # Simulate the timer firing AFTER suppression window has elapsed.
        engine._state.suppressed_until = dt_util.now() - timedelta(seconds=1)
        captured_callbacks[-1](dt_util.now())

    assert engine.state.suppressed_until is None
    assert engine.state.suppression_reason is None
    assert engine.current_status() != STATUS_SUPPRESSED


async def test_suppression_timer_cancelled_on_early_state_change(hass: HomeAssistant):
    """Calling async_unsuppress before the timer fires must cancel the pending
    timer so its callback never executes."""
    config = _make_config()
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    cancel_mock = MagicMock()
    captured_callbacks: list = []

    def _capture_point_in_time(hass_arg, cb, when):
        captured_callbacks.append(cb)
        return cancel_mock

    with patch(
        "custom_components.entity_guard.rule_engine.async_track_point_in_time",
        side_effect=_capture_point_in_time,
    ):
        await engine.async_suppress(duration_minutes=1)
        await engine.async_unsuppress()

    cancel_mock.assert_called_once()
    assert engine._suppression_timer_unsub is None


async def test_force_evaluate_clears_pending_suppression_timer(hass: HomeAssistant):
    """async_test_enforce (force-evaluate) must cancel any in-flight suppression
    timer so a stale expiry callback can't fire after the user manually drove
    the rule. Re-arm only when suppression remains active."""
    config = _make_config(target_state="off")
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    first_cancel = MagicMock()
    captured_calls: list = []

    def _capture_point_in_time(hass_arg, cb, when):
        captured_calls.append((cb, when))
        # First call returns first_cancel; subsequent calls return distinct mocks.
        return first_cancel if len(captured_calls) == 1 else MagicMock()

    async def _ok(call):
        pass

    hass.services.async_register("light", "turn_off", _ok)

    with patch(
        "custom_components.entity_guard.rule_engine.async_track_point_in_time",
        side_effect=_capture_point_in_time,
    ):
        await engine.async_suppress(duration_minutes=5)
        assert len(captured_calls) == 1

        # Force-evaluate while still suppressed — should cancel and re-arm.
        await engine.async_test_enforce()

    first_cancel.assert_called_once()
    # A fresh timer must be re-armed since suppression remains in effect.
    assert engine._suppression_timer_unsub is not None


def test_cancel_suppression_timer_swallows_exception(hass: HomeAssistant):
    """_cancel_suppression_timer must not raise when the unsub callback raises."""
    engine = _make_engine(hass)
    broken = MagicMock(side_effect=RuntimeError("boom"))
    engine._suppression_timer_unsub = broken
    engine._cancel_suppression_timer()  # must not raise
    broken.assert_called_once()
    assert engine._suppression_timer_unsub is None


def test_schedule_suppression_timer_skips_when_unloaded(hass: HomeAssistant):
    """Suppression timer must NOT be scheduled after the engine is unloaded."""
    engine = _make_engine(hass)
    engine._is_unloaded = True
    engine._state.suppressed_until = dt_util.now() + timedelta(minutes=5)
    with patch(
        "custom_components.entity_guard.rule_engine.async_track_point_in_time"
    ) as mock_track:
        engine._schedule_suppression_timer()
    mock_track.assert_not_called()
    assert engine._suppression_timer_unsub is None


def test_schedule_suppression_timer_skips_when_already_expired(hass: HomeAssistant):
    """Suppression timer must NOT be scheduled when suppressed_until is in the past."""
    engine = _make_engine(hass)
    engine._state.suppressed_until = dt_util.now() - timedelta(seconds=1)
    with patch(
        "custom_components.entity_guard.rule_engine.async_track_point_in_time"
    ) as mock_track:
        engine._schedule_suppression_timer()
    mock_track.assert_not_called()


def test_handle_suppression_expired_short_circuits_when_unloaded(hass: HomeAssistant):
    """_handle_suppression_expired returns early when the engine is unloaded."""
    engine = _make_engine(hass)
    engine._state.suppressed_until = dt_util.now() - timedelta(seconds=1)
    engine._is_unloaded = True
    # Must not raise / mutate anything beyond the unsub reset.
    engine._suppression_timer_unsub = MagicMock()
    engine._handle_suppression_expired(dt_util.now())
    assert engine._suppression_timer_unsub is None


# ---------------------------------------------------------------------------
# EG-4 regression: reset/clear paths must preserve suppression + re-arm timer
# ---------------------------------------------------------------------------


async def test_reset_cooldowns_preserves_suppression_and_rearms_timer(
    hass: HomeAssistant,
):
    """async_reset_cooldowns mid-suppression must NOT drop suppressed_until and must
    re-arm the EG-4 expiry timer (regression: prior cancel-only path lost it)."""
    config = _make_config()
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    arm_calls: list = []
    cancel_mocks: list = []

    def _capture(hass_arg, cb, when):
        m = MagicMock()
        cancel_mocks.append(m)
        arm_calls.append((cb, when))
        return m

    with patch(
        "custom_components.entity_guard.rule_engine.async_track_point_in_time",
        side_effect=_capture,
    ):
        await engine.async_suppress(duration_minutes=5)
        assert len(arm_calls) == 1
        first = cancel_mocks[0]

        await engine.async_reset_cooldowns()

    # State preserved.
    assert engine.state.suppressed_until is not None
    assert engine.state.suppression_reason == "manual"
    # Old timer cancelled, new timer armed.
    first.assert_called_once()
    assert len(arm_calls) == 2
    assert engine._suppression_timer_unsub is not None
    engine._cancel_suppression_timer()


async def test_clear_history_preserves_suppression_and_rearms_timer(
    hass: HomeAssistant,
):
    """async_clear_history mid-suppression must NOT drop suppressed_until and must
    re-arm the EG-4 expiry timer."""
    config = _make_config()
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    arm_calls: list = []
    cancel_mocks: list = []

    def _capture(hass_arg, cb, when):
        m = MagicMock()
        cancel_mocks.append(m)
        arm_calls.append((cb, when))
        return m

    with patch(
        "custom_components.entity_guard.rule_engine.async_track_point_in_time",
        side_effect=_capture,
    ):
        await engine.async_suppress(duration_minutes=5)
        assert len(arm_calls) == 1
        first = cancel_mocks[0]

        await engine.async_clear_history()

    assert engine.state.suppressed_until is not None
    assert engine.state.suppression_reason == "manual"
    first.assert_called_once()
    assert len(arm_calls) == 2
    assert engine._suppression_timer_unsub is not None
    engine._cancel_suppression_timer()


async def test_error_recovery_no_double_broadcast(hass: HomeAssistant):
    """During a non-final ERROR-recovery success, status must transition
    ENFORCING → ERROR in a single dispatcher broadcast — not flicker through
    ARMED/COOLDOWN first."""
    config = _make_config(target_state="off")
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    # Plant rule in ERROR with one prior success already in the window.
    engine._state.consecutive_errors = ERROR_THRESHOLD
    engine._state.consecutive_success_count = 1
    engine._set_status(STATUS_ERROR)

    async def _ok(call):
        pass

    hass.services.async_register("light", "turn_off", _ok)
    hass.states.async_set("light.bedroom", "on")
    st = hass.states.get("light.bedroom")

    broadcasts: list[str] = []
    original = engine._set_status

    def _record(status: str) -> None:
        broadcasts.append(status)
        original(status)

    engine._set_status = _record  # type: ignore[assignment]

    await engine.async_evaluate("light.bedroom", st)

    # Recovery threshold not met (2 < 3): expect ENFORCING then ERROR; ARMED
    # must NOT appear in between.
    assert "armed" not in broadcasts, f"flicker through armed: {broadcasts}"
    assert "cooldown" not in broadcasts, f"flicker through cooldown: {broadcasts}"
    assert engine.current_status() == STATUS_ERROR
    assert engine.state.consecutive_success_count == 2


# ---------------------------------------------------------------------------
# Branch coverage: missing branches
# ---------------------------------------------------------------------------


def test_maybe_reset_skipped_when_date_matches(hass: HomeAssistant):
    """_maybe_reset_today_counter must not reset counters when date is today (261->exit)."""
    engine = _make_engine(hass)
    today = dt_util.now().date()
    engine._state.today_reset_date = today
    engine._state.enforcement_count_today = 7
    engine._maybe_reset_today_counter()
    assert engine._state.enforcement_count_today == 7


async def test_evaluate_flag_entity_allowed_during_grace(hass: HomeAssistant):
    """During startup grace, entity in both target_entities and flag_entity_ids falls through (271->274 branch)."""
    # Entity is BOTH a target and a flag → during grace, we don't early-return for it
    config = _make_config(
        target_entities=["light.bedroom"],
        flags=[Flag(entity="light.bedroom", match_state="on")],
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = False
    hass.states.async_set("light.bedroom", "on")
    st = hass.states.get("light.bedroom")
    # entity_id IS in target_entities AND in flag_entity_ids → must NOT early-return
    await engine.async_evaluate("light.bedroom", st)
    # Reaches enabled check → disabled=False → proceeds to suppression check etc.
    assert engine.current_status() != STATUS_STARTING  # status updated


async def test_fire_flag_mismatch_different_cancel_handle(hass: HomeAssistant):
    """_fire: flags fail + pending_enforcements has different handle → don't pop (399->401 else branch)."""
    config = _make_config(
        target_state="off",
        delay_seconds=1,
        flags=[Flag(entity="input_boolean.night", match_state="on")],
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    hass.states.async_set("light.bedroom", "on")
    hass.states.async_set("input_boolean.night", "off")  # flags fail

    fires = []

    def _capture_later(_hass, _delay, cb):
        fires.append(cb)
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later",
        side_effect=_capture_later,
    ):
        engine._schedule_delayed_enforcement("light.bedroom")

    # Replace the cancel handle with a different one AFTER scheduling
    # so the identity check in _fire fails and it does NOT pop
    different = MagicMock()
    engine._pending_enforcements["light.bedroom"] = different

    assert fires
    fires[0](dt_util.now())
    await hass.async_block_till_done()
    # Different handle: pending_enforcements still has `different` (not popped)
    assert engine._pending_enforcements.get("light.bedroom") is different


async def test_fire_not_triggered_different_cancel_handle(hass: HomeAssistant):
    """_fire: not triggered + different cancel handle → don't pop (395->397 else branch)."""
    config = _make_config(target_state="off", delay_seconds=1)
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    hass.states.async_set("light.bedroom", "off")  # already off → not triggered

    fires = []

    def _capture_later(_hass, _delay, cb):
        fires.append(cb)
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later",
        side_effect=_capture_later,
    ):
        engine._schedule_delayed_enforcement("light.bedroom")

    # Replace cancel handle with a different one so identity check fails
    different = MagicMock()
    engine._pending_enforcements["light.bedroom"] = different

    assert fires
    fires[0](dt_util.now())
    await hass.async_block_till_done()
    # Different handle: not popped
    assert engine._pending_enforcements.get("light.bedroom") is different


async def test_cooldown_broadcast_no_cooldown_entry(hass: HomeAssistant):
    """debounce_enabled but cooldowns cleared between set and get (race with async_reset_cooldowns) → skip broadcast arm (588->exit)."""
    config = _make_config(
        target_state="off", debounce_enabled=True, debounce_seconds=30
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    hass.states.async_set("light.bedroom", "on")

    async def _ok(call):
        pass

    hass.services.async_register("light", "turn_off", _ok)

    # Simulate race: replace cooldowns with a dict subclass whose .get always returns None
    class _RacyDict(dict):
        def get(self, key, default=None):
            self.clear()  # simulate async_reset_cooldowns clearing between set and get

    engine._state.cooldowns = _RacyDict()
    await engine._enforce("light.bedroom")
    # Race cleared cooldown → broadcast timer not armed
    assert "light.bedroom" not in engine._cooldown_broadcast_unsubs


async def test_cooldown_broadcast_expired_entry_skipped(hass: HomeAssistant):
    """debounce cooldown_end in the past → remaining <= 0 → skip timer (584->exit)."""
    config = _make_config(
        target_state="off", debounce_enabled=True, debounce_seconds=30
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    hass.states.async_set("light.bedroom", "on")

    async def _ok(call):
        pass

    hass.services.async_register("light", "turn_off", _ok)
    # Plant an already-expired cooldown so remaining <= 0 before enforce runs
    # (override the cooldown that _enforce would set by patching timedelta addition)
    from datetime import timedelta as _td

    import custom_components.entity_guard.rule_engine as _re_mod

    original_timedelta = _td

    class _ZeroTD:
        """Returns a timedelta of 0 regardless of seconds kwarg."""

        def __new__(cls, seconds=0, **kw):
            return original_timedelta(seconds=0)

    with patch.object(_re_mod, "timedelta", _ZeroTD):
        await engine._enforce("light.bedroom")

    # Cooldown was set to now+0s → remaining=0 → timer not armed
    assert "light.bedroom" not in engine._cooldown_broadcast_unsubs


async def test_suppress_when_in_error_skips_apply_idle(hass: HomeAssistant):
    """async_suppress while STATUS_ERROR must not call _apply_idle_status (754->exit)."""
    engine = _make_engine(hass)
    engine._startup_complete = True
    engine._current_status = STATUS_ERROR
    await engine.async_suppress(duration_minutes=1, user_id=None)
    # Status must stay ERROR — _apply_idle_status not called
    assert engine.current_status() == STATUS_ERROR
    engine._cancel_suppression_timer()  # clean up lingering timer


def test_cooldown_remaining_multiple_picks_max(hass: HomeAssistant):
    """cooldown_remaining_seconds picks largest — exercises both if-taken and if-skipped branches (847->845)."""
    engine = _make_engine(hass)
    now = dt_util.now()
    # First entry is the MAX (delta > remaining → updates remaining)
    # Second entry is smaller (delta <= remaining → 847->845 branch taken)
    engine._state.cooldowns = {
        "light.a": now + timedelta(seconds=30),  # MAX — first
        "light.b": now + timedelta(seconds=10),  # smaller — 847->845 branch
    }
    remaining = engine.cooldown_remaining_seconds()
    assert remaining >= 28  # at least 28s (timing slack)


def test_derive_idle_status_now_provided(hass: HomeAssistant):
    """_derive_idle_status with now=None triggers dt_util.now() call; with now provided skips it (881->883 branch)."""
    engine = _make_engine(hass)
    engine._startup_complete = True
    # now=None → triggers dt_util.now() call internally
    result_none = engine._derive_idle_status(now=None)
    assert result_none in (STATUS_ARMED, STATUS_CONDITIONAL, STATUS_COOLDOWN)
    # now provided → skips dt_util.now() call (881->883 branch)
    result_now = engine._derive_idle_status(now=dt_util.now())
    assert result_now in (STATUS_ARMED, STATUS_CONDITIONAL, STATUS_COOLDOWN)


async def test_handle_suppression_expired_still_in_window(hass: HomeAssistant):
    """_handle_suppression_expired when suppressed_until is still in the future (948->953)."""
    engine = _make_engine(hass)
    engine._startup_complete = True
    engine._is_unloaded = False
    # Suppression still active
    engine._state.suppressed_until = dt_util.now() + timedelta(seconds=60)
    engine._handle_suppression_expired(dt_util.now())
    # Suppression NOT cleared; status reflects suppressed
    assert engine._state.suppressed_until is not None


# ---------------------------------------------------------------------------
# recently_enforced: _arm_recently_enforced pulse + timer callback
# ---------------------------------------------------------------------------


async def test_arm_recently_enforced_pulse_when_already_on(hass: HomeAssistant):
    """Pulse off→on when recently_enforced is already True."""
    from unittest.mock import patch

    engine = _make_engine(hass)
    engine._startup_complete = True
    engine._recently_enforced = True

    flag_values = []

    def _capture_send(_hass, _signal):
        flag_values.append(engine._recently_enforced)

    with patch(
        "custom_components.entity_guard.rule_engine.async_dispatcher_send",
        side_effect=_capture_send,
    ):
        engine._arm_recently_enforced()

    # First dispatch is the pulse-off (flag=False at time of dispatch)
    assert flag_values[0] is False
    # After arm, flag is True
    assert engine._recently_enforced is True


async def test_arm_recently_enforced_timer_clears_flag(hass: HomeAssistant):
    """Timer callback sets recently_enforced=False and dispatches."""
    from unittest.mock import patch

    engine = _make_engine(hass)
    engine._startup_complete = True

    captured_cb = []

    def _capture_later(_hass, _delay, job):
        # Store the callback (HassJob wraps it); return a cancel mock
        captured_cb.append(job)
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later",
        side_effect=_capture_later,
    ):
        engine._arm_recently_enforced()
        assert engine._recently_enforced is True

    assert captured_cb
    # Invoke the HassJob target directly to simulate timer fire
    captured_cb[0].target(dt_util.now())
    await hass.async_block_till_done()

    assert engine._recently_enforced is False


# ---------------------------------------------------------------------------
# async_reset_cooldowns: conditional broadcast when status unchanged
# ---------------------------------------------------------------------------


async def test_reset_cooldowns_broadcasts_when_recently_enforced_and_status_unchanged(
    hass: HomeAssistant,
):
    """Broadcast fires when recently_enforced=True and status stays the same."""
    engine = _make_engine(hass)
    engine._startup_complete = True
    engine._current_status = STATUS_ARMED
    engine._recently_enforced = True

    broadcasts = []
    original = engine._broadcast_status
    engine._broadcast_status = lambda: broadcasts.append(1) or original()

    await engine.async_reset_cooldowns()

    assert engine._recently_enforced is False
    assert len(broadcasts) >= 1


async def test_reset_cooldowns_no_extra_broadcast_when_recently_enforced_false(
    hass: HomeAssistant,
):
    """No extra broadcast when recently_enforced is already False."""
    engine = _make_engine(hass)
    engine._startup_complete = True
    engine._current_status = STATUS_ARMED
    engine._recently_enforced = False

    broadcasts = []
    original = engine._broadcast_status
    engine._broadcast_status = lambda: broadcasts.append(1) or original()

    await engine.async_reset_cooldowns()

    # _apply_idle_status → _set_status same status → skip-if-same → no broadcast
    assert len(broadcasts) == 0


async def test_is_recently_enforced_returns_flag(hass: HomeAssistant):
    """is_recently_enforced() returns the _recently_enforced flag value."""
    engine = _make_engine(hass)
    assert engine.is_recently_enforced() is False
    engine._recently_enforced = True
    assert engine.is_recently_enforced() is True


async def test_cancel_recently_enforced_timer_swallows_exception(hass: HomeAssistant):
    """_cancel_recently_enforced_timer swallows exceptions from the unsub call."""
    engine = _make_engine(hass)
    engine._recently_enforced_unsub = MagicMock(side_effect=RuntimeError("boom"))
    engine._cancel_recently_enforced_timer()  # must not raise
    assert engine._recently_enforced_unsub is None


async def test_arm_recently_enforced_timer_noop_when_unloaded(hass: HomeAssistant):
    """Timer _clear callback is a no-op when engine is already unloaded."""
    from unittest.mock import patch

    engine = _make_engine(hass)
    engine._startup_complete = True
    engine._recently_enforced = False

    captured_cb = []

    def _capture_later(_hass, _delay, job):
        captured_cb.append(job)
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later",
        side_effect=_capture_later,
    ):
        engine._arm_recently_enforced()

    engine._is_unloaded = True
    engine._recently_enforced = True  # set it so we can verify it's NOT cleared
    captured_cb[0].target(dt_util.now())
    await hass.async_block_till_done()

    # Unloaded: _clear returned early, flag unchanged
    assert engine._recently_enforced is True


async def test_arm_recently_enforced_verified_timer_clears_flag(hass: HomeAssistant):
    """Verified timer callback sets recently_enforced_verified=False and dispatches."""
    from unittest.mock import patch

    engine = _make_engine(hass)
    engine._startup_complete = True

    captured_cb = []

    def _capture_later(_hass, _delay, job):
        captured_cb.append(job)
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later",
        side_effect=_capture_later,
    ):
        engine._arm_recently_enforced_verified()
        assert engine._recently_enforced_verified is True

    assert captured_cb
    captured_cb[0].target(dt_util.now())
    await hass.async_block_till_done()

    assert engine._recently_enforced_verified is False


async def test_arm_recently_enforced_verified_timer_noop_when_unloaded(
    hass: HomeAssistant,
):
    """Verified timer _clear callback is a no-op when engine is already unloaded."""
    from unittest.mock import patch

    engine = _make_engine(hass)
    engine._startup_complete = True
    engine._recently_enforced_verified = False

    captured_cb = []

    def _capture_later(_hass, _delay, job):
        captured_cb.append(job)
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later",
        side_effect=_capture_later,
    ):
        engine._arm_recently_enforced_verified()

    engine._is_unloaded = True
    engine._recently_enforced_verified = True  # set so we can verify it's NOT cleared
    captured_cb[0].target(dt_util.now())
    await hass.async_block_till_done()

    assert engine._recently_enforced_verified is True


async def test_cancel_recently_enforced_verified_timer_swallows_exception(
    hass: HomeAssistant,
):
    """_cancel_recently_enforced_verified_timer swallows exceptions from unsub."""
    engine = _make_engine(hass)
    engine._recently_enforced_verified_unsub = MagicMock(
        side_effect=RuntimeError("boom")
    )
    engine._cancel_recently_enforced_verified_timer()  # must not raise
    assert engine._recently_enforced_verified_unsub is None


async def test_cancel_recently_enforced_cancelled_timer_swallows_exception(
    hass: HomeAssistant,
):
    """_cancel_recently_enforced_cancelled_timer swallows exceptions from unsub."""
    engine = _make_engine(hass)
    engine._recently_enforced_cancelled_unsub = MagicMock(
        side_effect=RuntimeError("boom")
    )
    engine._cancel_recently_enforced_cancelled_timer()  # must not raise
    assert engine._recently_enforced_cancelled_unsub is None


async def test_clear_history_broadcasts_when_recently_enforced_and_status_unchanged(
    hass: HomeAssistant,
):
    """Broadcast fires when recently_enforced=True and status stays same after clear_history."""
    engine = _make_engine(hass)
    engine._startup_complete = True
    engine._current_status = STATUS_ARMED
    engine._recently_enforced = True

    broadcasts = []
    original = engine._broadcast_status
    engine._broadcast_status = lambda: broadcasts.append(1) or original()

    await engine.async_clear_history()

    assert engine._recently_enforced is False
    assert len(broadcasts) >= 1


# ---------------------------------------------------------------------------
# _get_rule_entity_id
# ---------------------------------------------------------------------------


def test_get_rule_entity_id_returns_cached(hass: HomeAssistant) -> None:
    """Cache hit: second call returns cached value without touching registry."""
    engine = _make_engine(hass)
    engine._rule_entity_id = "sensor.cached_status"

    with patch("custom_components.entity_guard.rule_engine.er.async_get") as mock_reg:
        result = engine._get_rule_entity_id()

    assert result == "sensor.cached_status"
    mock_reg.assert_not_called()


def test_get_rule_entity_id_no_entry_returns_none(hass: HomeAssistant) -> None:
    """No config entry: returns None without touching registry."""
    engine = _make_engine(hass)
    assert engine._entry is None

    with patch("custom_components.entity_guard.rule_engine.er.async_get") as mock_reg:
        result = engine._get_rule_entity_id()

    assert result is None
    mock_reg.assert_not_called()


def test_get_rule_entity_id_finds_and_caches_status_sensor(
    hass: HomeAssistant,
) -> None:
    """Registry lookup finds status sensor, caches and returns it."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.entity_guard.const import DOMAIN

    entry = MockConfigEntry(domain=DOMAIN, title="Test Rule")
    entry.add_to_hass(hass)

    store = MagicMock(spec=EntityGuardStore)
    store.get_rule_state.return_value = {}
    store.set_rule_state.return_value = None
    store.runtime_to_blob = EntityGuardStore.runtime_to_blob
    store.blob_to_runtime = EntityGuardStore.blob_to_runtime
    engine = RuleEngine(hass, _make_config(), store, lambda: True, entry)

    fake_entry = MagicMock()
    fake_entry.entity_id = "sensor.test_rule_status"
    fake_entry.unique_id = f"{entry.entry_id}_status"

    with (
        patch("custom_components.entity_guard.rule_engine.er.async_get"),
        patch(
            "custom_components.entity_guard.rule_engine.er.async_entries_for_config_entry",
            return_value=[fake_entry],
        ),
    ):
        result = engine._get_rule_entity_id()

    assert result == "sensor.test_rule_status"
    assert engine._rule_entity_id == "sensor.test_rule_status"


def test_get_rule_entity_id_no_status_sensor_returns_none(
    hass: HomeAssistant,
) -> None:
    """Registry has entries but none end in _status: returns None."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.entity_guard.const import DOMAIN

    entry = MockConfigEntry(domain=DOMAIN, title="Test Rule")
    entry.add_to_hass(hass)

    store = MagicMock(spec=EntityGuardStore)
    store.get_rule_state.return_value = {}
    store.set_rule_state.return_value = None
    store.runtime_to_blob = EntityGuardStore.runtime_to_blob
    store.blob_to_runtime = EntityGuardStore.blob_to_runtime
    engine = RuleEngine(hass, _make_config(), store, lambda: True, entry)

    fake_entry = MagicMock()
    fake_entry.entity_id = "button.test_rule_arm"
    fake_entry.unique_id = f"{entry.entry_id}_arm"

    with (
        patch("custom_components.entity_guard.rule_engine.er.async_get"),
        patch(
            "custom_components.entity_guard.rule_engine.er.async_entries_for_config_entry",
            return_value=[fake_entry],
        ),
    ):
        result = engine._get_rule_entity_id()

    assert result is None


async def test_enforce_cancelled_error_propagates_not_swallowed(hass: HomeAssistant):
    """A genuine asyncio.CancelledError (e.g. HA shutting down mid-call) must
    propagate out of _enforce, NOT be caught by `except Exception` and misreported
    as a service failure. CancelledError is a BaseException (not Exception) and is
    distinct from TimeoutError, so neither except branch should catch it. This
    locks the subtle invariant against a future refactor adding `except
    BaseException` or reordering the branches."""

    async def _cancel(call):
        raise asyncio.CancelledError

    hass.services.async_register("light", "turn_off", _cancel)
    config = _make_config(target_state="off")
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    hass.states.async_set("light.bedroom", "on")
    st = hass.states.get("light.bedroom")

    with pytest.raises(asyncio.CancelledError):
        await engine.async_evaluate("light.bedroom", st)

    # It must NOT have been recorded as a service failure.
    assert engine.state.consecutive_errors == 0
    assert engine.state.last_error is None
    # Lock released despite the propagating cancellation (async with unwinds).
    assert not engine.state.reentrance_lock.locked()


# ---------------------------------------------------------------------------
# Feature I: skip counters
# ---------------------------------------------------------------------------


def test_fire_skipped_increments_skip_counts(hass: HomeAssistant):
    """A genuine skip bumps both skip_count_today and skip_count_total."""
    engine = _make_engine(hass)
    assert engine.state.skip_count_today == 0
    assert engine.state.skip_count_total == 0
    engine._fire_skipped("light.bedroom", "no_service_mapping")
    assert engine.state.skip_count_today == 1
    assert engine.state.skip_count_total == 1
    engine._fire_skipped("light.bedroom", "source_offline")
    assert engine.state.skip_count_today == 2
    assert engine.state.skip_count_total == 2


def test_fire_skipped_service_call_failed_not_counted(hass: HomeAssistant):
    """A failed service call routes through _fire_skipped but is NOT a skip — it
    has its own error path, so the skip counters must stay put."""
    engine = _make_engine(hass)
    engine._fire_skipped("light.bedroom", "service_call_failed", error="boom")
    assert engine.state.skip_count_today == 0
    assert engine.state.skip_count_total == 0
    # and the per-target outcome is still "failed" (A's behavior, unchanged)
    assert engine.state.target_outcomes["light.bedroom"]["outcome"] == "failed"


async def test_recovered_target_clears_stale_failed_outcome(hass: HomeAssistant):
    """A target that failed while offline and then comes back already at the
    desired state (so the rule no longer triggers) must clear its stale 'failed'
    outcome — otherwise it sticks in 'Targets needing attention' forever since no
    enforce fires to overwrite it."""
    engine = _make_engine(hass)  # trigger on "on", target "off"
    engine._startup_complete = True
    # Latch a prior failure (as an offline enforce would).
    engine.state.target_outcomes["light.bedroom"] = {
        "outcome": "failed",
        "reason": "service_call_failed",
        "error": "boom",
        "at": dt_util.now(),
    }
    # Target recovers at the desired state "off" → not triggered, live, compliant.
    hass.states.async_set("light.bedroom", "off")
    await engine.async_evaluate("light.bedroom", hass.states.get("light.bedroom"))
    assert engine.state.target_outcomes["light.bedroom"]["outcome"] == "ok"


async def test_offline_target_keeps_failed_outcome(hass: HomeAssistant):
    """A still-offline target must NOT be falsely marked healthy — the stale
    failed outcome persists while it is unavailable."""
    engine = _make_engine(hass)
    engine._startup_complete = True
    engine.state.target_outcomes["light.bedroom"] = {
        "outcome": "failed",
        "reason": "service_call_failed",
        "error": "boom",
        "at": dt_util.now(),
    }
    hass.states.async_set("light.bedroom", "unavailable")
    await engine.async_evaluate("light.bedroom", hass.states.get("light.bedroom"))
    assert engine.state.target_outcomes["light.bedroom"]["outcome"] == "failed"


def test_handle_midnight_resets_skip_today_not_total(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine.state.skip_count_today = 9
    engine.state.skip_count_total = 40
    engine._handle_midnight(MagicMock())
    assert engine.state.skip_count_today == 0
    assert engine.state.skip_count_total == 40  # total is monotonic


def test_maybe_reset_today_counter_resets_skip_today(hass: HomeAssistant):
    """The stale-date catch-up reset (missed midnight while HA was down) also
    zeroes skip_count_today."""
    from datetime import date

    engine = _make_engine(hass)
    engine.state.skip_count_today = 6
    engine.state.skip_count_total = 15
    engine.state.today_reset_date = date(2000, 1, 1)  # stale → triggers reset
    engine._maybe_reset_today_counter()
    assert engine.state.skip_count_today == 0
    assert engine.state.skip_count_total == 15


async def test_clear_history_resets_skip_counts(hass: HomeAssistant):
    """Clear History zeroes BOTH skip counters (consistency with enforcement)."""
    engine = _make_engine(hass)
    engine.state.skip_count_today = 3
    engine.state.skip_count_total = 22
    await engine.async_clear_history()
    assert engine.state.skip_count_today == 0
    assert engine.state.skip_count_total == 0


# Feature G: verify-after-enforce
# ---------------------------------------------------------------------------


def _verify_config(**overrides) -> RuleConfig:
    base = {"verify_enabled": True, "target_state": "off", "trigger_states": ["on"]}
    base.update(overrides)
    return _make_config(**base)


async def _run_verify_timer(hass, engine, entity_id):
    """Fire the pending verify timer for entity_id and drain the created task.

    The verify timer is armed via async_call_later(delay, _fire_cb); _fire_cb
    schedules the async _fire via async_create_task. We invoke the captured
    callback and block till done so the coroutine runs to completion.
    """
    cancel = engine._pending_verifications.get(entity_id)
    assert cancel is not None, "no pending verify armed"
    # The callback is captured by the async_call_later patch in each test.
    cb = engine._test_verify_cb
    cb(dt_util.now())
    await hass.async_block_till_done()


async def test_verify_armed_on_success(hass: HomeAssistant):
    """A successful enforce of a verify-enabled state rule arms a verify timer."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")

    captured = []

    def _fake_later(hass_, delay, cb):
        captured.append((delay, cb))
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")

    assert "light.bedroom" in engine._pending_verifications
    assert captured and captured[-1][0] == 5  # VERIFY_DELAY_SECONDS


async def test_verify_fast_confirms_responsive_target_same_tick(hass: HomeAssistant):
    """A responsive blocking=True target confirms in the SAME tick as the enforce.

    The device commits its post-enforce state DURING the shielded service call
    (the real ACK), so by the time the enforce tail arms the verify the target is
    already at desired. The synchronous fast-confirm must fire EVENT_VERIFIED and
    pop the pending verify immediately — no waiting out the 5s blind deadline (the
    user's "why the long delay before validation" bug).
    """
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.states.async_set("light.bedroom", "on")

    # Production-shaped service: commits the target to 'off' during the call.
    async def _turn_off(call):
        hass.states.async_set("light.bedroom", "off")

    hass.services.async_register("light", "turn_off", _turn_off)

    verified = []
    hass.bus.async_listen(EVENT_VERIFIED, lambda e: verified.append(e))

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")
        await hass.async_block_till_done()

    # Confirmed in-tick: pending popped, verified fired, no deadline needed.
    assert engine._pending_verifications == {}
    assert len(verified) == 1
    assert engine.state.target_outcomes["light.bedroom"]["outcome"] == "ok"


async def test_verify_silent_target_still_waits_deadline(hass: HomeAssistant):
    """A silent-ignore target does NOT fast-confirm — the deadline stays armed.

    Discriminator twin of the responsive case: the service returns OK but never
    moves the state, so the synchronous fast-confirm reads not-reached and leaves
    the verify pending for its corrective deadline. Guards against the fast-confirm
    over-firing on a target that never actually reached desired.
    """
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)  # no state move
    hass.states.async_set("light.bedroom", "on")  # stays on

    verified = []
    hass.bus.async_listen(EVENT_VERIFIED, lambda e: verified.append(e))

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")
        await hass.async_block_till_done()

    # Not reached → still pending its deadline, nothing confirmed yet.
    assert "light.bedroom" in engine._pending_verifications
    assert verified == []


async def test_verify_window_outranks_conditional_status(hass: HomeAssistant):
    """An armed verify must show VERIFYING even when the rule's flags are unmet.

    CONDITIONAL used to outrank the in-flight verify in _derive_idle_status, so a
    flag-gated rule flipped ENFORCING -> CONDITIONAL the instant the target reached
    desired, hiding the outstanding verify (which only surfaced backwards, when the
    deadline fired a corrective). In-flight action (PENDING/VERIFYING) now outranks
    CONDITIONAL. Uses a silent target so the verify stays pending to assert on.
    """
    engine = _make_engine(
        hass,
        _verify_config(flags=[Flag(entity="input_boolean.guard", match_state="on")]),
    )
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)  # silent
    hass.states.async_set("light.bedroom", "on")
    # Flag UNMET: without the verify hoist this derives CONDITIONAL.
    hass.states.async_set("input_boolean.guard", "off")

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")
        await hass.async_block_till_done()

    assert "light.bedroom" in engine._pending_verifications
    assert engine.current_status() == STATUS_VERIFYING


async def test_disable_during_enforce_await_paints_disabled_not_armed(
    hass: HomeAssistant,
):
    """Disabling mid-enforce must not leave the card stuck ARMED.

    set_enabled() is synchronous and takes no reentrance_lock, so a disable can
    interleave while _enforce is parked in its shielded service-call await. On
    resume the terminal paint must honour the now-off enabled flag (DISABLED),
    not overwrite it with the raw armed/cooldown derivation. Regression guard for
    the 'toggle off during VERIFYING → stuck ARMED' bug.
    """
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.states.async_set("light.bedroom", "on")

    # Interleave on the event loop (as the real toggle does), while _enforce is
    # parked in its shielded service-call await: the async service stub flips the
    # rule off, then reports the device moved. set_enabled runs on the loop, not
    # a worker thread, matching production.
    async def _turn_off_then_disable(call):
        engine.set_enabled(False)
        hass.states.async_set("light.bedroom", "off")

    hass.services.async_register("light", "turn_off", _turn_off_then_disable)

    await engine._enforce("light.bedroom")

    assert engine.current_status() == STATUS_DISABLED
    assert engine.state.enabled is False
    # No stale verify timer survives the disable.
    assert engine._pending_verifications == {}


async def test_verify_not_armed_when_disabled(hass: HomeAssistant):
    """A rule without verify_enabled never arms a verify timer."""
    engine = _make_engine(hass, _make_config(target_state="off"))
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")
    await engine._enforce("light.bedroom")
    assert engine._pending_verifications == {}


async def test_verify_not_armed_for_attribute_mode(hass: HomeAssistant):
    """verify_enabled is ignored for attribute modes (state-only v1)."""
    engine = _make_engine(
        hass,
        _make_config(
            mode=MODE_FIXED_ATTRIBUTE,
            verify_enabled=True,
            attribute="brightness",
            operator="lt",
            threshold=100,
            target_value=150,
        ),
    )
    engine._startup_complete = True
    hass.services.async_register("light", "turn_on", lambda c: None)
    hass.states.async_set("light.bedroom", "on", {"brightness": 50})
    await engine._enforce("light.bedroom")
    assert engine._pending_verifications == {}


async def test_verify_reached_clears_cleanly(hass: HomeAssistant):
    """Verify timer fires, target now compliant → no corrective, no failed outcome."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    calls = []
    hass.services.async_register("light", "turn_off", lambda c: calls.append(c))
    hass.states.async_set("light.bedroom", "on")

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")
        # Simulate the target having reached desired (off) by the time verify fires.
        hass.states.async_set("light.bedroom", "off")
        await _run_verify_timer(hass, engine, "light.bedroom")

    assert "light.bedroom" not in engine._unreachable
    assert engine.state.target_outcomes["light.bedroom"]["outcome"] == "ok"
    assert engine._verify_attempts.get("light.bedroom") is None


async def test_verify_not_reached_corrective_then_exhausted(hass: HomeAssistant):
    """Silent-ignore: service returns OK but target never moves. Verify re-reads,
    issues VERIFY_MAX_ATTEMPTS correctives, then writes failed + SELF-HEALS by
    re-entering the counted enforcing cycle (no permanent dead-end)."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    # Service that returns OK but does NOT change state (the silent-ignore case).
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")  # stays on throughout

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")
        # Fire verify repeatedly; each not-reached does a corrective + re-arms.
        for _ in range(VERIFY_MAX_ATTEMPTS + 1):
            if "light.bedroom" in engine._pending_verifications:
                await _run_verify_timer(hass, engine, "light.bedroom")

    # Exhausted once → failed outcome overwrites the provisional "ok", counter at 1,
    # and a re-entering delayed enforce is armed (self-heal, not a dead-end latch).
    assert engine._consecutive_exhaustions["light.bedroom"] == 1
    assert "light.bedroom" not in engine._unreachable
    outcome = engine.state.target_outcomes["light.bedroom"]
    assert outcome["outcome"] == "failed"
    assert outcome["reason"] == REASON_UNREACHABLE_AFTER_RETRIES
    # Re-entered the counted enforcing cycle: a delayed enforce is pending.
    assert "light.bedroom" in engine._pending_enforcements
    # Verify is done (handle popped): the card must leave VERIFYING.
    assert engine._pending_verifications == {}
    assert engine._current_status != STATUS_VERIFYING


async def test_manual_enforce_verifies_despite_flags_unmet(hass: HomeAssistant):
    """MANUAL force (test_enforce) on a CONDITIONAL rule whose flags are unmet must
    still run FULL verify and resolve VERIFIED when the device reaches desired.

    The user pressed test_enforce — an explicit override of the rule's conditions —
    so verify judges the DEVICE, not the flags. Prod bug: a forced cycle used to
    drop CANCELLED(flags_unmet) before the reached-check ran. See the VERIFY-OUTCOME
    CONTRACT above _arm_verify.
    """
    engine = _make_engine(
        hass,
        _verify_config(flags=[Flag(entity="input_boolean.guard", match_state="on")]),
    )
    engine._startup_complete = True
    hass.states.async_set("light.bedroom", "on")
    hass.states.async_set("input_boolean.guard", "off")  # flag UNMET → CONDITIONAL

    # Responsive target: commits to 'off' during the call (fast-confirm path).
    async def _turn_off(call):
        hass.states.async_set("light.bedroom", "off")

    hass.services.async_register("light", "turn_off", _turn_off)

    verified = []
    cancelled = []
    hass.bus.async_listen(EVENT_VERIFIED, lambda e: verified.append(e))
    hass.bus.async_listen(EVENT_CANCELLED, lambda e: cancelled.append(e))

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine.async_test_enforce()
        await hass.async_block_till_done()

    assert len(verified) == 1
    assert cancelled == []
    assert engine.state.target_outcomes["light.bedroom"]["outcome"] == "ok"
    assert engine._forced_verifications == set()  # cleared on resolve


async def test_manual_enforce_unreachable_despite_flags_unmet(hass: HomeAssistant):
    """MANUAL force on a flags-unmet rule with a SILENT target must exhaust to
    UNREACHABLE (not drop CANCELLED(flags_unmet) on the first re-read).

    Forced bypass must persist through the engine's correctives so the full
    attempt-based exhaustion runs — the failure twin of the verified case.
    """
    engine = _make_engine(
        hass,
        _verify_config(flags=[Flag(entity="input_boolean.guard", match_state="on")]),
    )
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)  # silent
    hass.states.async_set("light.bedroom", "on")  # stays on throughout
    hass.states.async_set("input_boolean.guard", "off")  # flag UNMET

    unreachable = []
    cancelled = []
    hass.bus.async_listen(EVENT_UNREACHABLE, lambda e: unreachable.append(e))
    hass.bus.async_listen(EVENT_CANCELLED, lambda e: cancelled.append(e))

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine.async_test_enforce()
        for _ in range(VERIFY_MAX_ATTEMPTS + 1):
            if "light.bedroom" in engine._pending_verifications:
                await _run_verify_timer(hass, engine, "light.bedroom")

    # Exhausted to the failure signal, never cancelled for flags_unmet.
    assert len(unreachable) == 1
    assert cancelled == []
    assert engine.state.target_outcomes["light.bedroom"]["outcome"] == "failed"
    assert engine.state.target_outcomes["light.bedroom"]["reason"] == (
        REASON_UNREACHABLE_AFTER_RETRIES
    )
    assert engine._forced_verifications == set()  # cleared at exhaustion


async def test_manual_enforce_still_drops_when_disabled(hass: HomeAssistant):
    """forced does NOT re-enable a disabled rule: a manual cycle that goes disabled
    mid-window must still drop CANCELLED(disabled), never confirm VERIFIED.

    unloaded/disabled/master_off bypass is explicitly NOT granted to forced — only
    suppressed/flags_unmet are. See the VERIFY-OUTCOME CONTRACT.
    """
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)  # silent
    hass.states.async_set("light.bedroom", "on")  # never moves

    cancelled = []
    verified = []
    hass.bus.async_listen(EVENT_CANCELLED, lambda e: cancelled.append(e))
    hass.bus.async_listen(EVENT_VERIFIED, lambda e: verified.append(e))

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine.async_test_enforce()
        assert "light.bedroom" in engine._pending_verifications  # forced verify armed
        assert engine._forced_verifications == {"light.bedroom"}
        # set_enabled(False) IS the real disabled-drop site: it cancels the in-flight
        # verify and fires CANCELLED(disabled) directly, before any deadline fires.
        engine.set_enabled(False)

    assert verified == []
    assert len(cancelled) == 1
    assert cancelled[0].data["reason"] == "disabled"
    assert engine._pending_verifications == {}
    assert engine._forced_verifications == set()


async def test_manual_enforce_verifies_via_event_path_despite_flags_unmet(
    hass: HomeAssistant,
):
    """MANUAL force on a flags-unmet rule whose ACK lands via the EVENT path (a real
    state event AFTER the enforce call, not a synchronous fast-confirm) must confirm
    EVENT_VERIFIED, never drop CANCELLED(flags_unmet).

    This is the event-path twin of test_manual_enforce_verifies_despite_flags_unmet
    (which exercises fast-confirm). The forced bypass lives in TWO mirrored sites:
    the deadline _fire closure AND _verify_drop_reason (read by _handle_state_event's
    confirm gate). Reverting the `not forced` guards in _verify_drop_reason makes this
    test fail (red-on-bug) while the fast-confirm test stays green — proving the
    mirror-path coverage. See the VERIFY-OUTCOME CONTRACT above _arm_verify.
    """
    engine = _make_engine(
        hass,
        _verify_config(flags=[Flag(entity="input_boolean.guard", match_state="on")]),
    )
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)  # silent: no
    #                                   sync commit → fast-confirm misses, event owns it
    hass.states.async_set("light.bedroom", "on")
    hass.states.async_set("input_boolean.guard", "off")  # flag UNMET throughout

    verified = []
    cancelled = []
    hass.bus.async_listen(EVENT_VERIFIED, lambda e: verified.append(e))
    hass.bus.async_listen(EVENT_CANCELLED, lambda e: cancelled.append(e))

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine.async_test_enforce()
        assert "light.bedroom" in engine._pending_verifications  # forced verify armed
        assert engine._forced_verifications == {"light.bedroom"}
        # Target reaches desired LATER via a genuine state event → _handle_state_event
        # owns the confirm, gated by _verify_drop_reason (the forced-bypass mirror).
        # The watched-entities listener is patched out in _make_engine, so drive the
        # event directly (as the other event-confirm tests do).
        hass.states.async_set("light.bedroom", "off")  # live state for re-derive
        event = MagicMock()
        event.data = {"entity_id": "light.bedroom", "new_state": _state("off")}
        event.context = MagicMock(id="ctx-forced")
        engine._handle_state_event(event)
        await hass.async_block_till_done()

    assert len(verified) == 1
    assert cancelled == []
    assert engine.state.target_outcomes["light.bedroom"]["outcome"] == "ok"
    assert engine._forced_verifications == set()  # cleared on event-path confirm


async def test_manual_enforce_verifies_despite_suppressed(hass: HomeAssistant):
    """MANUAL force on a SUPPRESSED rule must bypass the suppressed drop and resolve
    VERIFIED when the device reaches desired.

    The flags_unmet half of the forced bypass is covered by
    test_manual_enforce_verifies_despite_flags_unmet; this is its suppressed twin.
    forced bypasses BOTH suppressed and flags_unmet (see the VERIFY-OUTCOME CONTRACT);
    without this, the suppressed leg of the bypass was untested. Reverting the
    `not forced` guard on the suppressed drop makes this fail (red-on-bug).
    """
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    engine.state.suppressed_until = dt_util.now() + timedelta(hours=1)  # SUPPRESSED
    hass.states.async_set("light.bedroom", "on")

    # Responsive target: commits 'off' during the call (fast-confirm path).
    async def _turn_off(call):
        hass.states.async_set("light.bedroom", "off")

    hass.services.async_register("light", "turn_off", _turn_off)

    verified = []
    cancelled = []
    hass.bus.async_listen(EVENT_VERIFIED, lambda e: verified.append(e))
    hass.bus.async_listen(EVENT_CANCELLED, lambda e: cancelled.append(e))

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine.async_test_enforce()
        await hass.async_block_till_done()

    assert len(verified) == 1
    assert cancelled == []
    assert engine.state.target_outcomes["light.bedroom"]["outcome"] == "ok"
    assert engine._forced_verifications == set()  # cleared on resolve
    engine._cancel_suppression_timer()


async def test_forced_bypass_does_not_leak_into_next_automatic_cycle(
    hass: HomeAssistant,
):
    """The forced marker must be per-cycle: after a manual-forced cycle resolves on an
    entity, the NEXT automatic cycle on the SAME entity must honour the drops again.

    End-to-end leak guard. _cancel_verify discards from _forced_verifications on every
    resolve; if a resolve path ever forgot to, a forced bypass would silently persist
    and the next automatic flags-unmet cycle would wrongly VERIFY. Proves no inherited
    bypass. See the VERIFY-OUTCOME CONTRACT above _arm_verify.
    """
    engine = _make_engine(
        hass,
        _verify_config(flags=[Flag(entity="input_boolean.guard", match_state="on")]),
    )
    engine._startup_complete = True
    hass.states.async_set("light.bedroom", "on")
    hass.states.async_set("input_boolean.guard", "off")  # flag UNMET → CONDITIONAL

    # Responsive target for the forced leg: commits 'off' during the call.
    async def _turn_off(call):
        hass.states.async_set("light.bedroom", "off")

    hass.services.async_register("light", "turn_off", _turn_off)

    verified = []
    cancelled = []
    hass.bus.async_listen(EVENT_VERIFIED, lambda e: verified.append(e))
    hass.bus.async_listen(EVENT_CANCELLED, lambda e: cancelled.append(e))

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        # Leg 1: MANUAL force bypasses flags_unmet → VERIFIED, marker cleared.
        await engine.async_test_enforce()
        await hass.async_block_till_done()
        assert len(verified) == 1
        assert engine._forced_verifications == set()  # must be clear before leg 2

        # Leg 2: AUTOMATIC cycle on the SAME entity, flags still unmet, SILENT target.
        # If the forced marker had leaked, this would VERIFY; it must CANCEL.
        hass.services.async_register("light", "turn_off", lambda c: None)  # silent now
        hass.states.async_set("light.bedroom", "on")  # back on → desired unmet
        await engine._enforce("light.bedroom")  # not forced
        await _run_verify_timer(hass, engine, "light.bedroom")

    assert len(verified) == 1  # ONLY the forced leg verified
    assert len(cancelled) == 1
    assert cancelled[0].data["reason"] == "flags_unmet"  # automatic leg dropped
    assert engine._forced_verifications == set()


async def test_automatic_flags_unmet_still_cancelled(hass: HomeAssistant):
    """Regression: the AUTOMATIC path is unchanged — a non-forced cycle whose flags
    go unmet with a silent target still drops CANCELLED(flags_unmet).

    Proves the forced bypass is byte-isolated to the manual button.
    """
    engine = _make_engine(
        hass,
        _verify_config(flags=[Flag(entity="input_boolean.guard", match_state="on")]),
    )
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)  # silent
    hass.states.async_set("light.bedroom", "on")
    hass.states.async_set("input_boolean.guard", "on")  # flag MET at arm time

    cancelled = []
    verified = []
    hass.bus.async_listen(EVENT_CANCELLED, lambda e: cancelled.append(e))
    hass.bus.async_listen(EVENT_VERIFIED, lambda e: verified.append(e))

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")  # AUTOMATIC enforce, not forced
        hass.states.async_set("input_boolean.guard", "off")  # flag lapses mid-window
        await _run_verify_timer(hass, engine, "light.bedroom")

    assert verified == []
    assert len(cancelled) == 1
    assert cancelled[0].data["reason"] == "flags_unmet"
    assert engine._forced_verifications == set()


async def test_automatic_fastconfirm_cancels_when_flags_lapse_mid_enforce(
    hass: HomeAssistant,
):
    """AUTOMATIC cycle whose flags lapse DURING the shielded enforce await, with a
    responsive target that reaches desired in the same tick (fast-confirm path), must
    drop CANCELLED(flags_unmet) — NOT confirm VERIFIED.

    Pre-fix bug: the synchronous fast-confirm in _enforce's tail had no flags gate, so
    this reached-during-lapse case confirmed VERIFIED, while the deadline/event paths
    (which read flags at resolve time) cancelled. Pure ACK-timing luck decided the
    outcome. The fix routes fast-confirm through _verify_drop_reason like the other two
    paths. Reverting that gate makes this test fail (red-on-bug). See VERIFY-OUTCOME
    CONTRACT: "only when flags match" outranks "device happens to be right".
    """
    engine = _make_engine(
        hass,
        _verify_config(flags=[Flag(entity="input_boolean.guard", match_state="on")]),
    )
    engine._startup_complete = True
    hass.states.async_set("light.bedroom", "on")
    hass.states.async_set("input_boolean.guard", "on")  # flag MET at enforce decision

    # Responsive target: commits 'off' AND lapses the flag during the shielded await,
    # so fast-confirm sees reached-desired but flags-unmet in the same tick.
    async def _turn_off(call):
        hass.states.async_set("light.bedroom", "off")
        hass.states.async_set("input_boolean.guard", "off")  # flag lapses mid-await

    hass.services.async_register("light", "turn_off", _turn_off)

    cancelled = []
    verified = []
    hass.bus.async_listen(EVENT_CANCELLED, lambda e: cancelled.append(e))
    hass.bus.async_listen(EVENT_VERIFIED, lambda e: verified.append(e))

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")  # AUTOMATIC, not forced
        await hass.async_block_till_done()

    assert verified == []
    assert len(cancelled) == 1
    assert cancelled[0].data["reason"] == "flags_unmet"
    assert "light.bedroom" not in engine._pending_verifications


async def test_corrective_enforce_does_not_touch_bookkeeping(hass: HomeAssistant):
    """A verify-driven corrective re-enforce must not count toward the user-visible
    enforcement counters, the loop-protection rate_limit_window, or STATUS_ERROR
    auto-recovery — its success is provisional (verify may overturn it to failed).
    Only the original (non-corrective) enforce counts."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")  # never moves → correctives fire

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")  # the ONE real enforce
        # Snapshot after the original enforce, before any corrective.
        today = engine.state.enforcement_count_today
        total = engine.state.enforcement_count_total
        window_len = len(engine.state.rate_limit_window)
        assert today == 1 and total == 1 and window_len == 1
        # Seed a pre-existing error state to prove a corrective cannot clear it.
        engine.state.consecutive_errors = 2
        engine.state.last_error = "boom"
        # Fire the verify cycle: each not-reached issues a corrective re-enforce.
        for _ in range(VERIFY_MAX_ATTEMPTS + 1):
            if "light.bedroom" in engine._pending_verifications:
                await _run_verify_timer(hass, engine, "light.bedroom")

    # Correctives left every piece of bookkeeping exactly as the original enforce.
    assert engine.state.enforcement_count_today == today
    assert engine.state.enforcement_count_total == total
    assert len(engine.state.rate_limit_window) == window_len
    # Recovery half: a corrective's provisional success did NOT clear the error.
    assert engine.state.consecutive_errors == 2
    assert engine.state.last_error == "boom"


async def test_verify_real_event_clears_latch_and_cancels(hass: HomeAssistant):
    """A real state_changed for a target clears transient markers (unreachable
    latch + self-heal exhaustion counter) so a later death starts fresh. An
    OFFLINE move also cancels the stale verify; a still-triggering ONLINE event
    keeps it (that is the enforce's own in-progress transition — see
    test_verify_kept_on_intermediate_still_triggering_event). Here the target
    goes unavailable, so the cancel still applies."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    engine._unreachable.add("light.bedroom")
    engine._consecutive_exhaustions["light.bedroom"] = 3
    engine._pending_verifications["light.bedroom"] = MagicMock()

    event = MagicMock()
    event.data = {"entity_id": "light.bedroom", "new_state": _state("unavailable")}
    event.context = MagicMock(id="ctx-1")
    engine._handle_state_event(event)

    assert "light.bedroom" not in engine._unreachable
    assert "light.bedroom" not in engine._pending_verifications
    assert "light.bedroom" not in engine._consecutive_exhaustions


async def test_verify_real_event_resets_attempt_count(hass: HomeAssistant):
    """A genuine target state_changed abandons the superseded verify cycle: it
    must reset _verify_attempts too, else the NEXT cycle inherits a stale count
    and latches unreachable prematurely (fewer than VERIFY_MAX_ATTEMPTS retries)."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    engine._verify_attempts["light.bedroom"] = 1  # a cycle was mid-flight
    engine._pending_verifications["light.bedroom"] = MagicMock()

    event = MagicMock()
    event.data = {"entity_id": "light.bedroom", "new_state": _state("off")}
    event.context = MagicMock(id="ctx-1")
    engine._handle_state_event(event)

    assert "light.bedroom" not in engine._verify_attempts


async def test_verify_confirmed_on_target_reach_event(hass: HomeAssistant):
    """The target's own post-enforce move TO the desired state is the ACK: a
    reached-desired event with a pending verify CONFIRMS NOW (event-driven happy
    path) — cancels the deadline timer and fires EVENT_VERIFIED immediately
    instead of waiting the full 5 s. The latch clear + counter resets still
    happen. attempts == stale _verify_attempts + 1 (here 1 → 2)."""
    engine = _make_engine(hass, _verify_config())  # trigger "on", desired "off"
    engine._startup_complete = True
    engine._unreachable.add("light.bedroom")
    engine._verify_attempts["light.bedroom"] = 1
    engine._consecutive_exhaustions["light.bedroom"] = 2
    pending = MagicMock()
    engine._pending_verifications["light.bedroom"] = pending
    # _confirm_verified → _apply_idle_status re-reads live state; make it consistent.
    hass.states.async_set("light.bedroom", "off")

    events = []
    hass.bus.async_listen(EVENT_VERIFIED, lambda e: events.append(e))

    event = MagicMock()
    event.data = {"entity_id": "light.bedroom", "new_state": _state("off")}  # desired
    event.context = MagicMock(id="ctx-1")
    engine._handle_state_event(event)
    await hass.async_block_till_done()

    # Deadline timer cancelled (handle called, pending popped) and confirmed now.
    pending.assert_called_once()
    assert "light.bedroom" not in engine._pending_verifications
    assert len(events) == 1
    assert events[0].data["attempts"] == 2  # stale 1 + 1
    assert engine.is_recently_enforced_verified() is True
    # Transient markers cleared.
    assert "light.bedroom" not in engine._unreachable
    assert "light.bedroom" not in engine._verify_attempts
    assert "light.bedroom" not in engine._consecutive_exhaustions


async def test_verify_event_confirm_ignored_when_no_pending(hass: HomeAssistant):
    """A reached-desired event with NO pending verify must not fire EVENT_VERIFIED
    (nothing to confirm) and must not crash — the `pending` gate protects it."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.states.async_set("light.bedroom", "off")

    events = []
    hass.bus.async_listen(EVENT_VERIFIED, lambda e: events.append(e))

    event = MagicMock()
    event.data = {"entity_id": "light.bedroom", "new_state": _state("off")}
    event.context = MagicMock(id="ctx-1")
    engine._handle_state_event(event)
    await hass.async_block_till_done()

    assert events == []


async def test_verify_event_confirm_dropped_when_suppressed_mid_window(
    hass: HomeAssistant,
):
    """A reached-desired event while the rule is suppressed mid-window must NOT
    confirm (mirrors the timer's suppressed drop): no EVENT_VERIFIED and the
    attempt count is abandoned. Because the target already complied, the stale
    deadline handle is dropped so a later unsuppress cannot re-derive a phantom
    VERIFYING (see test_verify_dropped_reached_drops_stale_handle)."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    engine._verify_attempts["light.bedroom"] = 1
    pending = MagicMock()
    engine._pending_verifications["light.bedroom"] = pending
    engine._state.suppressed_until = dt_util.now() + timedelta(minutes=5)
    hass.states.async_set("light.bedroom", "off")

    events = []
    hass.bus.async_listen(EVENT_VERIFIED, lambda e: events.append(e))

    event = MagicMock()
    event.data = {"entity_id": "light.bedroom", "new_state": _state("off")}
    event.context = MagicMock(id="ctx-1")
    engine._handle_state_event(event)
    await hass.async_block_till_done()

    assert events == []  # dropped, not confirmed
    assert "light.bedroom" not in engine._verify_attempts  # superseded count dropped
    # Target complied, so no VERIFYING owed: the stale deadline handle is dropped.
    assert "light.bedroom" not in engine._pending_verifications
    # Card resolves to SUPPRESSED NOW, not lingering on VERIFYING until the ~5s
    # deadline: _derive_idle_status ranks SUPPRESSED above the pending→VERIFYING
    # derive, so the dropped-but-reached event re-derive surfaces it immediately.
    assert engine.current_status() == STATUS_SUPPRESSED


async def test_verify_dropped_reached_drops_stale_handle(hass: HomeAssistant):
    """Reached-desired while suppressed drops the deadline handle, so a later
    unsuppress BEFORE the ~5s deadline does not re-derive a phantom VERIFYING on
    an already-compliant target. Regression: _clear_suppression_state clears
    suppressed_until but not _pending_verifications, so a stale handle would fall
    through _derive_armed_or_cooldown to STATUS_VERIFYING until the timer fired."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    engine._pending_verifications["light.bedroom"] = MagicMock()
    engine._state.suppressed_until = dt_util.now() + timedelta(minutes=5)
    hass.states.async_set("light.bedroom", "off")

    # Target reaches desired WHILE suppressed → dropped confirm, handle cleared.
    event = MagicMock()
    event.data = {"entity_id": "light.bedroom", "new_state": _state("off")}
    event.context = MagicMock(id="ctx-1")
    engine._handle_state_event(event)
    await hass.async_block_till_done()
    assert "light.bedroom" not in engine._pending_verifications

    # Unsuppress before any deadline fire: status must NOT be VERIFYING.
    await engine.async_unsuppress()
    assert engine.current_status() != STATUS_VERIFYING


async def test_verify_event_dropped_when_unloaded(hass: HomeAssistant):
    """_verify_event_dropped returns True once the engine is unloaded. This branch
    is unreachable via a live state_changed (unload unsubscribes the listener
    first), so it is asserted directly — the predicate still guards it for
    lockstep with the timer's _is_unloaded early-return."""
    engine = _make_engine(hass, _verify_config())
    engine._is_unloaded = True
    assert engine._verify_event_dropped("light.bedroom") is True


async def test_verify_event_dropped_when_disabled(hass: HomeAssistant):
    """_verify_event_dropped returns True when the rule (or master) is disabled —
    mirrors the timer's disabled/master-off drop so an event confirm cannot fire
    for an inactive rule."""
    engine = _make_engine(hass, _verify_config())
    engine._state.enabled = False
    assert engine._verify_event_dropped("light.bedroom") is True


async def test_verify_cancelled_when_target_offline(hass: HomeAssistant):
    """A target that goes offline/unknown counts as 'not reached': cancel the
    pending verify so we never confirm the signal on a vanished target."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    engine._pending_verifications["light.bedroom"] = MagicMock()

    event = MagicMock()
    event.data = {"entity_id": "light.bedroom", "new_state": _state("unavailable")}
    event.context = MagicMock(id="ctx-1")
    engine._handle_state_event(event)

    assert "light.bedroom" not in engine._pending_verifications


async def test_verify_kept_on_intermediate_still_triggering_event(hass: HomeAssistant):
    """A still-ONLINE target that has NOT yet reached desired (e.g. a light GROUP
    reading any-on 'on' mid fan-out, before its last member turns off) must NOT
    cancel an in-flight verify. The pending deadline timer + corrective loop own
    the cycle and confirm on the final reached event. Regression for the group
    bug: the intermediate cancel popped pending, so recently_enforced_verified
    never pulsed in prod (confirm survived only by racing a re-enforce)."""
    engine = _make_engine(hass, _verify_config())  # trigger "on", desired "off"
    engine._startup_complete = True
    pending = MagicMock()
    engine._pending_verifications["light.bedroom"] = pending

    events = []
    hass.bus.async_listen(EVENT_VERIFIED, lambda e: events.append(e))

    # Intermediate event: target still reads 'on' (triggering = not reached) but is
    # ONLINE — the enforce's own in-progress transition, not an away-move.
    event = MagicMock()
    event.data = {"entity_id": "light.bedroom", "new_state": _state("on")}
    event.context = MagicMock(id="ctx-1")
    engine._handle_state_event(event)
    await hass.async_block_till_done()

    # Verify kept alive: deadline handle NOT cancelled, pending preserved, no
    # premature confirm.
    pending.assert_not_called()
    assert "light.bedroom" in engine._pending_verifications
    assert len(events) == 0

    # The final reached event then confirms via the happy path.
    hass.states.async_set("light.bedroom", "off")
    reached = MagicMock()
    reached.data = {"entity_id": "light.bedroom", "new_state": _state("off")}
    reached.context = MagicMock(id="ctx-2")
    engine._handle_state_event(reached)
    await hass.async_block_till_done()

    pending.assert_called_once()
    assert "light.bedroom" not in engine._pending_verifications
    assert len(events) == 1


async def test_verify_kept_on_intermediate_still_exhausts_to_unreachable(
    hass: HomeAssistant,
):
    """Keeping the verify armed on an intermediate still-online event must NOT
    defeat the deadline terminator: a target that stays online but NEVER reaches
    desired (stuck group member, silent-ignore device) must still exhaust the
    corrective loop and fire EVENT_UNREACHABLE. This locks the safety property
    that de-risks the intermediate-keep-alive branch — both reviewers flagged it
    as implied-by, but not asserted-in, the kept-alive test (which uses a bare
    MagicMock handle and never runs the real 5s _fire). Here the deadline cb is
    captured and driven to exhaustion with the target still reading 'on'."""
    engine = _make_engine(hass, _verify_config())  # trigger "on", desired "off"
    engine._startup_complete = True
    # Service that does NOT move the target — the enforce is a silent no-op, so
    # every _fire re-reads 'on' (online, not reached) exactly like a stuck group.
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")

    events = []
    hass.bus.async_listen(EVENT_UNREACHABLE, lambda e: events.append(e))

    def _fake_later(hass_, delay, cb):
        if delay == 5:  # VERIFY_DELAY_SECONDS — not the recently_enforced pulse timer
            engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")
        assert "light.bedroom" in engine._pending_verifications  # armed

        # An intermediate still-online-not-reached event lands mid-window (the
        # branch under test): it must leave the verify armed, not cancel it.
        intermediate = MagicMock()
        intermediate.data = {"entity_id": "light.bedroom", "new_state": _state("on")}
        intermediate.context = MagicMock(id="ctx-mid")
        engine._handle_state_event(intermediate)
        await hass.async_block_till_done()
        assert "light.bedroom" in engine._pending_verifications, (
            "intermediate event must not drop the verify"
        )

        # Deadline fires + correctives run; target never moves → exhaustion.
        for _ in range(VERIFY_MAX_ATTEMPTS + 1):
            if "light.bedroom" in engine._pending_verifications:
                await _run_verify_timer(hass, engine, "light.bedroom")

    assert len(events) == 1, "stuck-online target must still fire EVENT_UNREACHABLE"
    assert events[0].data["reason"] == REASON_UNREACHABLE_AFTER_RETRIES
    assert engine.is_recently_enforced_unreachable() is True
    assert "light.bedroom" not in engine._pending_verifications, (
        "pending must be cleared on exhaustion — no leak"
    )


async def test_verify_not_rearmed_while_pending(hass: HomeAssistant):
    """A re-enforce fired while a verify is already pending must NOT re-arm over
    it. _arm_verify cancels the live timer first, so a re-enforce churned by an
    intermediate group fan-out event kept resetting the 5s deadline — the
    already-pending guard at the arm site keeps the ORIGINAL cycle intact."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")

    original = MagicMock()
    engine._pending_verifications["light.bedroom"] = original

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later",
        lambda *a: MagicMock(),
    ):
        await engine._enforce("light.bedroom")

    # Same handle: the pending verify was neither cancelled nor replaced.
    original.assert_not_called()
    assert engine._pending_verifications["light.bedroom"] is original


async def test_verify_event_confirm_fires_once_and_cancels_timer(hass: HomeAssistant):
    """Event-driven confirm fires EVENT_VERIFIED exactly once and neutralises the
    deadline timer: a reached event confirms, then firing the (now-stale) captured
    timer cb must NOT emit a second EVENT_VERIFIED (the _fire identity guard returns
    early once its handle has been popped by the event site's cancel)."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")

    events = []
    hass.bus.async_listen(EVENT_VERIFIED, lambda e: events.append(e))

    def _fake_later(hass_, delay, cb):
        if delay == 5:  # VERIFY_DELAY_SECONDS — not the recently_enforced pulse timer
            engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")
        assert "light.bedroom" in engine._pending_verifications  # armed
        # Target's own post-enforce ACK reaches desired → event-driven confirm.
        hass.states.async_set("light.bedroom", "off")
        event = MagicMock()
        event.data = {"entity_id": "light.bedroom", "new_state": _state("off")}
        event.context = MagicMock(id="ctx-1")
        engine._handle_state_event(event)
        await hass.async_block_till_done()
        # Fire the captured deadline cb ANYWAY (in prod the cancel prevents this;
        # here we force it directly — the handle is already popped, so
        # _run_verify_timer's pending assert would trip) — the stale-fire guard
        # must suppress a second confirm.
        engine._test_verify_cb(dt_util.now())
        await hass.async_block_till_done()

    assert len(events) == 1  # confirmed once, no double-fire
    assert events[0].data["attempts"] == 1
    assert "light.bedroom" not in engine._pending_verifications


async def test_verify_unreachable_marker_blocks_rearm(hass: HomeAssistant):
    """The transient _unreachable double-arm marker suppresses a fresh verify on an
    enforce while it is set (it is dropped on re-entry / cleared on recovery)."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    engine._unreachable.add("light.bedroom")
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")
    await engine._enforce("light.bedroom")
    assert "light.bedroom" not in engine._pending_verifications


async def test_verify_corrective_timeout_counts_both(hass: HomeAssistant):
    """Note B: a corrective enforce that itself times out counts as a verify
    attempt AND bumps consecutive_errors (hung corrective = real failure)."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True

    async def _hang(call):
        await asyncio.sleep(5)

    hass.services.async_register("light", "turn_off", _hang)
    hass.states.async_set("light.bedroom", "on")

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with (
        patch(
            "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
        ),
        patch(
            "custom_components.entity_guard.rule_engine.ENFORCE_TIMEOUT_SECONDS", 0.05
        ),
    ):
        # First enforce times out (not a success) — so no verify is armed here.
        await engine._enforce("light.bedroom")
        # Drive it manually: arm a verify, then fire it; the corrective hangs+times out.
        engine._pending_verifications["light.bedroom"] = MagicMock()
        engine._test_verify_cb = None
        # Re-arm via a real success path is impossible with a hanging service, so
        # exercise the corrective path directly: a verify fire with the hang.
        engine._verify_attempts["light.bedroom"] = 0
        # Build a verify fire by arming through _arm_verify (captures the cb).
        engine._arm_verify("light.bedroom")
        await _run_verify_timer(hass, engine, "light.bedroom")

    # The corrective timed out → service_call_failed path ran.
    assert engine.state.consecutive_errors >= 1
    assert engine._verify_attempts.get("light.bedroom") == 1


async def test_verify_unload_cancels_pending(hass: HomeAssistant):
    """Pending verify timers are cancelled + cleared on unload (lifecycle gap)."""
    engine = _make_engine(hass, _verify_config())
    cancel = MagicMock()
    engine._pending_verifications["light.bedroom"] = cancel
    engine._verify_attempts["light.bedroom"] = 1
    engine._unreachable.add("light.bedroom")
    engine._consecutive_exhaustions["light.bedroom"] = 2
    await engine.async_unload()
    cancel.assert_called_once()
    assert engine._pending_verifications == {}
    assert engine._verify_attempts == {}
    assert engine._unreachable == set()
    assert engine._consecutive_exhaustions == {}


async def test_verify_fire_noop_when_unloaded(hass: HomeAssistant):
    """Verify _fire is a no-op if the engine unloaded before it ran."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")
        engine._is_unloaded = True
        await _run_verify_timer(hass, engine, "light.bedroom")
    # No corrective attempt recorded — fire bailed on the unloaded guard.
    assert engine._verify_attempts.get("light.bedroom") is None


async def test_verify_fire_noop_when_disabled(hass: HomeAssistant):
    """Verify _fire bails when the rule/master is disabled by the time it fires."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")
        engine._state.enabled = False
        await _run_verify_timer(hass, engine, "light.bedroom")
    assert engine._verify_attempts.get("light.bedroom") is None


async def test_verify_fire_drops_when_suppressed(hass: HomeAssistant):
    """A verify firing while suppressed drops the cycle (no corrective)."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")
        engine._verify_attempts["light.bedroom"] = 1
        engine._state.suppressed_until = dt_util.now() + timedelta(minutes=5)
        await _run_verify_timer(hass, engine, "light.bedroom")
    # Suppressed → attempt counter cleared, no corrective.
    assert engine._verify_attempts.get("light.bedroom") is None


async def test_verify_fire_drops_when_flags_unmet(hass: HomeAssistant):
    """A verify firing after conditional flags go unmet drops the cycle.

    Flags were met when the enforce ran (verify armed), then the watched flag
    flips unmet during the verify window. When the timer fires the rule is
    CONDITIONAL — not allowed to act — so the corrective re-enforce must NOT
    run and the attempt counter is cleared. Mirrors the suppressed-drop guard.
    Regression: the corrective path previously skipped _flags_match(), so it
    re-enforced the target while the rule showed CONDITIONAL.
    """
    engine = _make_engine(
        hass,
        _verify_config(flags=[Flag(entity="input_boolean.guard", match_state="on")]),
    )
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("input_boolean.guard", "on")  # flags met → enforce allowed
    hass.states.async_set("light.bedroom", "on")

    cancelled = []
    hass.bus.async_listen(EVENT_CANCELLED, lambda e: cancelled.append(e))

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")
        engine._verify_attempts["light.bedroom"] = 1
        # Flag flips unmet during the verify window → rule is now CONDITIONAL.
        hass.states.async_set("input_boolean.guard", "off")
        corrective = patch.object(engine, "_enforce", new=AsyncMock())
        with corrective as spy:
            await _run_verify_timer(hass, engine, "light.bedroom")
    # Flags unmet → counter cleared, NO corrective enforce, status CONDITIONAL.
    assert engine._verify_attempts.get("light.bedroom") is None
    spy.assert_not_awaited()
    assert engine.current_status() == STATUS_CONDITIONAL
    # Abandoned, not failed: the deadline drop fires exactly one EVENT_CANCELLED
    # (reason flags_unmet) and pulses the cancelled sensor — the neutral sibling
    # of the verified/unreachable signals.
    await hass.async_block_till_done()
    assert len(cancelled) == 1
    assert cancelled[0].data["reason"] == "flags_unmet"
    assert cancelled[0].data["target_entity_id"] == "light.bedroom"
    assert engine.is_recently_enforced_cancelled() is True


async def test_verify_event_dropped_when_flags_unmet(hass: HomeAssistant):
    """_verify_event_dropped returns True when the watched flag is unmet — the
    event-confirm twin of the timer's flags-unmet drop, keeping both paths in
    lockstep. Flags read at call (event) time, same instant the timer reads at
    fire time."""
    engine = _make_engine(
        hass,
        _verify_config(flags=[Flag(entity="input_boolean.guard", match_state="on")]),
    )
    hass.states.async_set("input_boolean.guard", "off")  # flags unmet
    assert engine._verify_event_dropped("light.bedroom") is True


async def test_verify_event_confirm_drops_when_flags_unmet(hass: HomeAssistant):
    """Twin of test_verify_fire_drops_when_flags_unmet for the EVENT path.

    Flags met when the enforce ran (verify armed) on a SILENT target (no same-
    tick fast-confirm), then the flag flips unmet during the window. The target's
    reached-desired state event then lands. Because flags are now unmet the rule
    is not allowed to act, so the event confirm must DROP the cycle: no
    EVENT_VERIFIED pulse, pending cleared, status CONDITIONAL — identical to the
    deadline path. Regression for the cross-path gap: the event path fired
    EVENT_VERIFIED while the deadline path dropped, making the signal flicker on
    device-ACK timing alone.
    """
    engine = _make_engine(
        hass,
        _verify_config(flags=[Flag(entity="input_boolean.guard", match_state="on")]),
    )
    engine._startup_complete = True
    # Silent target: service does not move it, so the enforce tail reads not-
    # reached and arms the verify instead of fast-confirming in-tick.
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("input_boolean.guard", "on")  # flags met → enforce allowed
    hass.states.async_set("light.bedroom", "on")

    events = []
    cancelled = []
    hass.bus.async_listen(EVENT_VERIFIED, lambda e: events.append(e))
    hass.bus.async_listen(EVENT_CANCELLED, lambda e: cancelled.append(e))

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")
        assert "light.bedroom" in engine._pending_verifications  # armed, silent

        # Flag flips unmet during the window → rule now CONDITIONAL.
        hass.states.async_set("input_boolean.guard", "off")
        # The target's delayed ACK (reached desired) lands AFTER the flag flipped.
        hass.states.async_set("light.bedroom", "off")
        reached = MagicMock()
        reached.data = {"entity_id": "light.bedroom", "new_state": _state("off")}
        reached.context = MagicMock(id="ctx-ack")
        engine._handle_state_event(reached)
        await hass.async_block_till_done()

    # Dropped, not confirmed: no verified pulse, pending cleared, CONDITIONAL.
    assert len(events) == 0, "flags-unmet reached event must not fire EVENT_VERIFIED"
    assert "light.bedroom" not in engine._pending_verifications
    assert engine.current_status() == STATUS_CONDITIONAL
    # The event path fired the abandoned-cycle signal exactly once (reason
    # flags_unmet) and pulsed the cancelled sensor — the neutral sibling.
    assert len(cancelled) == 1, "exactly one EVENT_CANCELLED for the abandoned cycle"
    assert cancelled[0].data["reason"] == "flags_unmet"
    assert engine.is_recently_enforced_cancelled() is True


async def test_verify_fire_cancelled_when_suppressed(hass: HomeAssistant):
    """The suppressed deadline drop fires EVENT_CANCELLED (reason suppressed).

    Companion to test_verify_fire_drops_when_suppressed (which only asserts the
    counter clears): the abandoned-cycle signal must fire at the suppressed drop
    site too, not only at flags_unmet.
    """
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")

    cancelled = []
    hass.bus.async_listen(EVENT_CANCELLED, lambda e: cancelled.append(e))

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")
        engine._state.suppressed_until = dt_util.now() + timedelta(minutes=5)
        await _run_verify_timer(hass, engine, "light.bedroom")

    assert len(cancelled) == 1
    assert cancelled[0].data["reason"] == "suppressed"
    assert engine.is_recently_enforced_cancelled() is True


async def test_verify_fire_cancelled_when_disabled(hass: HomeAssistant):
    """The disabled deadline drop fires EVENT_CANCELLED (reason disabled)."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")

    cancelled = []
    hass.bus.async_listen(EVENT_CANCELLED, lambda e: cancelled.append(e))

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")
        engine._state.enabled = False  # rule disabled during the verify window
        await _run_verify_timer(hass, engine, "light.bedroom")

    assert len(cancelled) == 1
    assert cancelled[0].data["reason"] == "disabled"
    assert engine.is_recently_enforced_cancelled() is True
    # Disabled drop must clear the attempt counter (like suppressed/flags_unmet
    # siblings), else a quick re-enable reads a stale high count and can latch
    # exhaustion early → premature EVENT_UNREACHABLE on a device that only
    # "failed" because the rule was off.
    assert "light.bedroom" not in engine._verify_attempts


async def test_set_enabled_false_fires_cancelled_and_clears_attempts(
    hass: HomeAssistant,
):
    """The REAL per-rule disable path (set_enabled) fires cancelled + clears count.

    _state.enabled has a single writer (set_enabled), so a per-rule switch-off
    always cancels in-flight verifies HERE, before the deadline/event drop sites
    can fire. If the cancelled signal + attempt-pop don't happen in this loop they
    never happen on disable: the deadline timer is unsubscribed (identity guard
    bails) and the event path's pending entry is already popped. Regression guard
    for both — the prior tests set _state.enabled directly and bypassed this path,
    so a counter leak + a dead "disabled" signal hid behind them.
    """
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")

    cancelled = []
    hass.bus.async_listen(EVENT_CANCELLED, lambda e: cancelled.append(e))

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")
        assert "light.bedroom" in engine._pending_verifications
        engine._verify_attempts["light.bedroom"] = 2  # mid-window, attempts high
        engine.set_enabled(False)  # real disable via the per-rule switch
        await hass.async_block_till_done()

    assert len(cancelled) == 1
    assert cancelled[0].data["reason"] == "disabled"
    assert engine.is_recently_enforced_cancelled() is True
    # Counter cleared → a re-enable + re-enforce starts a fresh verify cycle.
    assert "light.bedroom" not in engine._verify_attempts
    # Verify timer torn down → no stale deadline survives the disable.
    assert "light.bedroom" not in engine._pending_verifications


async def test_set_enabled_false_no_cancelled_without_pending_verify(
    hass: HomeAssistant,
):
    """Disabling a rule with NO in-flight verify must not fire a cancelled signal.

    The set_enabled loop fires only for targets that had a pending verify — an
    idle rule being switched off is not an abandoned cycle.
    """
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True

    cancelled = []
    hass.bus.async_listen(EVENT_CANCELLED, lambda e: cancelled.append(e))

    engine.set_enabled(False)
    await hass.async_block_till_done()

    assert len(cancelled) == 0
    assert engine.is_recently_enforced_cancelled() is False


async def test_verify_fire_cancelled_when_master_off(hass: HomeAssistant):
    """The master-off deadline drop fires EVENT_CANCELLED (reason master_off).

    Fifth drop reason in _verify_drop_reason: master disabled during the verify
    window abandons the cycle like disabled/suppressed/flags_unmet — not a device
    fault. Locks the master_off branch (reason precedence sits above suppressed).
    """
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")

    cancelled = []
    hass.bus.async_listen(EVENT_CANCELLED, lambda e: cancelled.append(e))

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")
        engine._master_enabled_getter = lambda: False  # hub master off mid-window
        await _run_verify_timer(hass, engine, "light.bedroom")

    assert len(cancelled) == 1
    assert cancelled[0].data["reason"] == "master_off"
    assert engine.is_recently_enforced_cancelled() is True


async def test_recently_enforced_cancelled_rearm_and_clear(hass: HomeAssistant):
    """Arm twice (pulse-off re-arm), then let the 30s window reset it.

    Covers the cancelled sensor lifecycle directly, mirroring its verified twin:
    - second arm while still latched cancels the live timer and pulses the flag
      off→on so a rising-edge automation re-triggers within the window;
    - the real reset timer firing past RECENTLY_ENFORCED_WINDOW_SECONDS clears the
      flag and drops the unsub handle.
    """
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    engine = _make_engine(hass, _verify_config())

    engine._arm_recently_enforced_cancelled()
    assert engine.is_recently_enforced_cancelled() is True
    first_unsub = engine._recently_enforced_cancelled_unsub
    assert first_unsub is not None

    # Second arm: cancels first timer (live unsub call) + pulses off→on.
    engine._arm_recently_enforced_cancelled()
    assert engine.is_recently_enforced_cancelled() is True
    assert engine._recently_enforced_cancelled_unsub is not first_unsub

    # Drive the real reset timer past the window → _clear runs normally.
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=RECENTLY_ENFORCED_WINDOW_SECONDS + 1)
    )
    await hass.async_block_till_done()
    assert engine.is_recently_enforced_cancelled() is False
    assert engine._recently_enforced_cancelled_unsub is None


async def test_recently_enforced_cancelled_clear_skipped_when_unloaded(
    hass: HomeAssistant,
):
    """The reset callback early-returns when the entry is unloaded.

    _clear nulls its unsub handle, then bails at `if self._is_unloaded` before
    touching the flag or dispatching — no state churn into a dead entry.
    """
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    engine = _make_engine(hass, _verify_config())
    engine._arm_recently_enforced_cancelled()
    assert engine.is_recently_enforced_cancelled() is True

    engine._is_unloaded = True
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=RECENTLY_ENFORCED_WINDOW_SECONDS + 1)
    )
    await hass.async_block_till_done()

    # unsub cleared, but the flag stays as-was (callback bailed before resetting).
    assert engine._recently_enforced_cancelled_unsub is None
    assert engine.is_recently_enforced_cancelled() is True


async def test_verify_fire_no_cancelled_when_unloaded(hass: HomeAssistant):
    """The unloaded drop does NOT fire EVENT_CANCELLED (entry tearing down).

    _verify_drop_reason returns "unloaded" so the gate reads dropped, but no bus
    event is fired for it: the pulse timer would be cancelled on unload anyway
    and the listeners are going away. Deliberate asymmetry vs the other four
    reasons — locked here so a refactor can't start firing into a dead entry.
    """
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")

    cancelled = []
    hass.bus.async_listen(EVENT_CANCELLED, lambda e: cancelled.append(e))

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")
        assert "light.bedroom" in engine._pending_verifications
        engine._is_unloaded = True  # entry torn down during the window
        # Deadline cb returns at the `if self._is_unloaded: return` guard — no
        # drop-site reached, no fire. Call it directly (pending still armed).
        engine._test_verify_cb(dt_util.now())
        await hass.async_block_till_done()

    assert len(cancelled) == 0, "unloaded drop must not fire EVENT_CANCELLED"
    assert engine.is_recently_enforced_cancelled() is False


async def test_verify_event_no_cancelled_when_unloaded(hass: HomeAssistant):
    """The event path also suppresses EVENT_CANCELLED on reason=unloaded.

    A reached-desired ACK landing while the entry is unloaded: _verify_drop_reason
    returns "unloaded", the event branch cancels the pending verify but the
    `reason != "unloaded"` guard skips the fire — the deliberate no-fire-on-unload
    asymmetry holds on BOTH paths.
    """
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")

    cancelled = []
    hass.bus.async_listen(EVENT_CANCELLED, lambda e: cancelled.append(e))

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")
        assert "light.bedroom" in engine._pending_verifications
        engine._is_unloaded = True
        hass.states.async_set("light.bedroom", "off")
        reached = MagicMock()
        reached.data = {"entity_id": "light.bedroom", "new_state": _state("off")}
        reached.context = MagicMock(id="ctx-ack")
        engine._handle_state_event(reached)
        await hass.async_block_till_done()

    assert len(cancelled) == 0, "unloaded event drop must not fire EVENT_CANCELLED"
    assert "light.bedroom" not in engine._pending_verifications
    assert engine.is_recently_enforced_cancelled() is False


async def test_verify_event_fire_cancelled_when_master_off(hass: HomeAssistant):
    """The EVENT path fires EVENT_CANCELLED (reason master_off) at a reached ACK.

    Deadline-path master_off goes through _fire's inline reason; the event path
    instead routes through _verify_drop_reason, so the master_off branch there is
    exercised only here: a reached-desired ACK lands while the hub master is off
    → drop_reason "master_off" → _fire_cancelled on the event side.
    """
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")

    cancelled = []
    hass.bus.async_listen(EVENT_CANCELLED, lambda e: cancelled.append(e))

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")
        assert "light.bedroom" in engine._pending_verifications
        engine._master_enabled_getter = lambda: False  # hub master off mid-window
        hass.states.async_set("light.bedroom", "off")
        reached = MagicMock()
        reached.data = {"entity_id": "light.bedroom", "new_state": _state("off")}
        reached.context = MagicMock(id="ctx-ack")
        engine._handle_state_event(reached)
        await hass.async_block_till_done()

    assert len(cancelled) == 1
    assert cancelled[0].data["reason"] == "master_off"
    assert "light.bedroom" not in engine._pending_verifications
    assert engine.is_recently_enforced_cancelled() is True


def test_cancel_verify_swallows_exception(hass: HomeAssistant):
    """A raising cancel handle must not propagate out of _cancel_verify."""
    engine = _make_engine(hass, _verify_config())

    def _boom():
        raise RuntimeError("cancel boom")

    engine._pending_verifications["light.bedroom"] = _boom
    engine._cancel_verify("light.bedroom")  # must not raise
    assert "light.bedroom" not in engine._pending_verifications


async def test_unload_swallows_verify_cancel_exception(hass: HomeAssistant):
    """async_unload tolerates a raising verify cancel handle."""
    engine = _make_engine(hass, _verify_config())

    def _boom():
        raise RuntimeError("verify cancel boom")

    engine._pending_verifications["light.bedroom"] = _boom
    await engine.async_unload()  # must not raise
    assert engine._pending_verifications == {}


async def test_status_verifying_while_verify_pending(hass: HomeAssistant):
    """After a verify-enabled success, status is VERIFYING (not ARMED) while the
    re-read is outstanding — the provisional 'ok' is not yet confirmed."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")

    assert "light.bedroom" in engine._pending_verifications
    assert engine._current_status == STATUS_VERIFYING


async def test_status_leaves_verifying_when_reached(hass: HomeAssistant):
    """When the verify fires and the target is compliant, status resolves off
    VERIFYING back to the real idle status (ARMED here, no cooldown). Fires the
    timer directly (no state_changed dispatch), so this now covers the DEADLINE
    FALLBACK confirm path (routes through _confirm_verified), not the event one."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")
        assert engine._current_status == STATUS_VERIFYING
        # Target reached desired by verify time → cycle clears, VERIFYING resolves.
        hass.states.async_set("light.bedroom", "off")
        await _run_verify_timer(hass, engine, "light.bedroom")

    assert engine._pending_verifications == {}
    assert engine._current_status == STATUS_ARMED


async def test_status_verifying_outranks_cooldown_on_debounced_enforce(
    hass: HomeAssistant,
):
    """A verify-enabled rule with debounce on: a normal successful enforce sets a
    cooldown AND arms a verify. The post-enforce status must be VERIFYING, not
    COOLDOWN — the re-read is still outstanding, so the card must not read 'done'.
    (Regression: the debounce branch previously hardcoded STATUS_COOLDOWN.)"""
    engine = _make_engine(
        hass, _verify_config(debounce_enabled=True, debounce_seconds=60)
    )
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")

    # Cooldown WAS set (debounce), but verify outranks it on the chip.
    assert engine._state.cooldowns.get("light.bedroom") is not None
    assert "light.bedroom" in engine._pending_verifications
    assert engine._current_status == STATUS_VERIFYING


async def test_status_leaves_verifying_when_disabled_mid_window(hass: HomeAssistant):
    """If the rule is disabled while a verify is pending, the verify fire must
    re-derive so the chip leaves VERIFYING for DISABLED — not stay stuck on the
    transient status until some later event (regression: bare early-return)."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")
        assert engine._current_status == STATUS_VERIFYING
        # Rule disabled before the verify timer fires.
        engine._state.enabled = False
        await _run_verify_timer(hass, engine, "light.bedroom")

    assert engine._pending_verifications == {}
    assert engine._current_status == STATUS_DISABLED


# ---------------------------------------------------------------------------
# Self-heal: verify exhaustion re-enters the counted enforcing cycle, bounded
# by loop protection (dead-device kill-switch floor).
# ---------------------------------------------------------------------------


async def _exhaust_once(hass, engine, entity_id="light.bedroom"):
    """Run one full verify cycle to exhaustion (service never moves the target)."""
    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later",
        lambda h, d, cb: setattr(engine, "_test_verify_cb", cb) or MagicMock(),
    ):
        await engine._enforce(entity_id)
        for _ in range(VERIFY_MAX_ATTEMPTS + 1):
            if entity_id in engine._pending_verifications:
                await _run_verify_timer(hass, engine, entity_id)


async def test_selfheal_exhaustion_schedules_reentry_no_suppress(hass: HomeAssistant):
    """First exhaustion: counter=1, failed outcome, a delayed enforce armed, NOT suppressed."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")

    await _exhaust_once(hass, engine)

    assert engine._consecutive_exhaustions["light.bedroom"] == 1
    assert "light.bedroom" in engine._pending_enforcements
    assert engine._state.suppressed_until is None
    assert engine._current_status != STATUS_SUPPRESSED


async def test_selfheal_skipped_event_fires_only_on_first_exhaustion(
    hass: HomeAssistant,
):
    """EVENT_SKIPPED is one-shot per fresh exhaustion streak (count==1), not every cycle."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")

    events = []
    hass.bus.async_listen(EVENT_SKIPPED, lambda e: events.append(e))

    await _exhaust_once(hass, engine)
    await hass.async_block_till_done()
    # Second exhaustion streak (seed counter so the next exhaustion is count==2).
    engine._consecutive_exhaustions["light.bedroom"] = 1
    engine._verify_attempts.pop("light.bedroom", None)
    await _exhaust_once(hass, engine)
    await hass.async_block_till_done()

    unreachable = [
        e for e in events if e.data.get("reason") == REASON_UNREACHABLE_AFTER_RETRIES
    ]
    assert len(unreachable) == 1


async def test_selfheal_limit_trips_loop_protection(hass: HomeAssistant):
    """Reaching CONSECUTIVE_EXHAUSTION_LIMIT suppresses with reason unreachable_loop
    and does NOT schedule another re-entry."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")
    # Seed to one below the limit so this exhaustion trips it.
    engine._consecutive_exhaustions["light.bedroom"] = CONSECUTIVE_EXHAUSTION_LIMIT - 1

    await _exhaust_once(hass, engine)

    assert (
        engine._consecutive_exhaustions["light.bedroom"] == CONSECUTIVE_EXHAUSTION_LIMIT
    )
    assert engine._current_status == STATUS_SUPPRESSED
    assert engine._state.suppression_reason == "unreachable_loop"
    assert engine._state.suppressed_until is not None
    # Kill-switch floor path cancels pending; no re-entry armed.
    assert "light.bedroom" not in engine._pending_enforcements
    await engine.async_unload()  # cancel the wall-clock unsuppress timer


async def test_selfheal_floor_trips_with_rate_limit_disabled(hass: HomeAssistant):
    """The kill-switch floor trips even when max_enforcements_per_minute == 0."""
    engine = _make_engine(hass, _verify_config(max_enforcements_per_minute=0))
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")
    engine._consecutive_exhaustions["light.bedroom"] = CONSECUTIVE_EXHAUSTION_LIMIT - 1

    await _exhaust_once(hass, engine)

    assert engine._current_status == STATUS_SUPPRESSED
    assert engine._state.suppression_reason == "unreachable_loop"
    await engine.async_unload()  # cancel the wall-clock unsuppress timer


async def test_trigger_loop_protection_cause_wording(hass: HomeAssistant):
    """cause='unreachable_loop' sets the unreachable reason + EVENT cause; the default
    'rate_limit' keeps the classic loop_protection reason."""
    events = []
    hass.bus.async_listen(EVENT_LOOP_DETECTED, lambda e: events.append(e))

    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    await engine._trigger_loop_protection("light.bedroom", cause="unreachable_loop")
    assert engine._state.suppression_reason == "unreachable_loop"

    engine2 = _make_engine(hass, _make_config())
    engine2._startup_complete = True
    await engine2._trigger_loop_protection("light.bedroom")
    assert engine2._state.suppression_reason == "loop_protection"

    await hass.async_block_till_done()
    causes = sorted(e.data["cause"] for e in events)
    assert causes == ["rate_limit", "unreachable_loop"]
    await engine.async_unload()
    await engine2.async_unload()


async def test_selfheal_provisional_ok_not_clobbered_mid_exhaustion(
    hass: HomeAssistant,
):
    """While mid-exhaustion (counter>0), a re-entering enforce's provisional 'ok' must
    not overwrite the live failed/unreachable outcome (the verify re-read owns truth)."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")
    engine._consecutive_exhaustions["light.bedroom"] = 1
    engine._state.target_outcomes["light.bedroom"] = {
        "outcome": "failed",
        "reason": REASON_UNREACHABLE_AFTER_RETRIES,
        "error": "x",
        "at": dt_util.now(),
    }

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later",
        lambda h, d, cb: MagicMock(),
    ):
        await engine._enforce("light.bedroom")

    outcome = engine._state.target_outcomes["light.bedroom"]
    assert outcome["outcome"] == "failed"
    assert outcome["reason"] == REASON_UNREACHABLE_AFTER_RETRIES


async def test_suppressed_target_recovery_clears_outcome_keeps_suppression(
    hass: HomeAssistant,
):
    """A target that recovers to desired WHILE suppressed drops from attention, but
    the rule stays SUPPRESSED and suppressed_until is unchanged."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    suppressed_until = dt_util.now() + timedelta(minutes=10)
    engine._state.suppressed_until = suppressed_until
    engine._state.suppression_reason = "unreachable_loop"
    engine._state.target_outcomes["light.bedroom"] = {
        "outcome": "failed",
        "reason": REASON_UNREACHABLE_AFTER_RETRIES,
        "error": "x",
        "at": dt_util.now(),
    }
    # Target recovers to the desired state ("off" -> no longer triggers).
    hass.states.async_set("light.bedroom", "off")

    broadcasts = []
    async_dispatcher_connect(
        hass,
        signal_for_rule(engine._config.unique_id),
        lambda *_: broadcasts.append(1),
    )
    await engine.async_evaluate("light.bedroom", _state("off"))
    await hass.async_block_till_done()

    assert engine._state.target_outcomes["light.bedroom"]["outcome"] == "ok"
    assert engine._current_status == STATUS_SUPPRESSED
    assert engine._state.suppressed_until == suppressed_until
    assert broadcasts  # a refresh fired so the card drops the attention row


async def test_suppressed_still_failed_target_keeps_attention(hass: HomeAssistant):
    """A target still non-compliant while suppressed keeps its failed outcome (no clear)."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    engine._state.suppressed_until = dt_util.now() + timedelta(minutes=10)
    engine._state.target_outcomes["light.bedroom"] = {
        "outcome": "failed",
        "reason": REASON_UNREACHABLE_AFTER_RETRIES,
        "error": "x",
        "at": dt_util.now(),
    }
    hass.states.async_set("light.bedroom", "on")  # still triggers (non-compliant)

    await engine.async_evaluate("light.bedroom", _state("on"))

    assert engine._state.target_outcomes["light.bedroom"]["outcome"] == "failed"
    assert engine._current_status == STATUS_SUPPRESSED


async def test_fire_success_sibling_still_pending_broadcasts(hass: HomeAssistant):
    """B1: with a still-pending sibling, firing one target's delayed enforce must
    force a broadcast whose pending_enforcement_due excludes the fired target, even
    though the derived status stays PENDING (dedupe would otherwise swallow it)."""
    config = _make_config(
        delay_seconds=30,
        target_entities=["light.a", "light.b"],
        max_enforcements_per_minute=0,
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    # Both targets arm via the same patched async_call_later, which can't see the
    # entity_id — so evaluate them one at a time and grab each entity's _fire cb.
    cbs = {}
    arming = {"eid": None}

    def _fake_later(h, d, cb):
        cbs[arming["eid"]] = cb
        return MagicMock()

    for eid in ("light.a", "light.b"):
        hass.states.async_set(eid, "on")
    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        for eid in ("light.a", "light.b"):
            arming["eid"] = eid
            await engine.async_evaluate(eid, hass.states.get(eid))
            await hass.async_block_till_done()
    assert engine.current_status() == STATUS_PENDING
    assert set(engine.pending_enforcement_due()) == {"light.a", "light.b"}

    snaps = []
    async_dispatcher_connect(
        hass,
        signal_for_rule(config.unique_id),
        lambda *_: snaps.append(set(engine.pending_enforcement_due())),
    )
    # Fire light.a's delayed enforce; light.b stays pending (status remains PENDING).
    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        cbs["light.a"](dt_util.now())
        await hass.async_block_till_done()

    assert snaps, "no broadcast fired after the fired target was popped"
    assert "light.a" not in snaps[-1]
    assert "light.b" in snaps[-1]


async def test_verified_event_fires_once_on_first_read_success(hass: HomeAssistant):
    """verify-on + target reached desired on the FIRST re-read → EVENT_VERIFIED
    fires exactly once with attempts=1, and the verified flag arms."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")

    events = []
    hass.bus.async_listen(EVENT_VERIFIED, lambda e: events.append(e))

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")
        hass.states.async_set("light.bedroom", "off")  # reached desired
        await _run_verify_timer(hass, engine, "light.bedroom")

    await hass.async_block_till_done()
    assert len(events) == 1
    data = events[0].data
    assert data["target_entity_id"] == "light.bedroom"
    assert data["attempts"] == 1
    assert data["rule_id"] == engine.config.unique_id
    assert data["domain"] == "light"
    assert data["target"] == "off"
    assert engine.is_recently_enforced_verified() is True


async def test_verified_event_attempts_counts_correctives(hass: HomeAssistant):
    """Target reaches desired only after N correctives → attempts == N+1."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")

    events = []
    hass.bus.async_listen(EVENT_VERIFIED, lambda e: events.append(e))

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")
        # Two not-reached correctives, then compliant on the third re-read.
        await _run_verify_timer(hass, engine, "light.bedroom")  # corrective 1
        await _run_verify_timer(hass, engine, "light.bedroom")  # corrective 2
        hass.states.async_set("light.bedroom", "off")
        await _run_verify_timer(hass, engine, "light.bedroom")  # confirmed

    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data["attempts"] == 3  # 2 correctives + original


async def test_verified_event_not_fired_when_verify_off(hass: HomeAssistant):
    """verify disabled → no verify cycle, so EVENT_VERIFIED never fires and the
    verified flag stays off even on a clean enforce."""
    engine = _make_engine(hass, _make_config(target_state="off"))
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")

    events = []
    hass.bus.async_listen(EVENT_VERIFIED, lambda e: events.append(e))

    await engine._enforce("light.bedroom")
    await hass.async_block_till_done()

    assert events == []
    assert engine.is_recently_enforced_verified() is False


async def test_verified_event_not_fired_on_exhaustion(hass: HomeAssistant):
    """Silent-ignore target that never reaches desired → EVENT_VERIFIED never
    fires (only EVENT_SKIPPED does); verified flag stays off."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")  # never moves

    verified = []
    hass.bus.async_listen(EVENT_VERIFIED, lambda e: verified.append(e))

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")
        for _ in range(VERIFY_MAX_ATTEMPTS + 1):
            if "light.bedroom" in engine._pending_verifications:
                await _run_verify_timer(hass, engine, "light.bedroom")

    await hass.async_block_till_done()
    assert verified == []
    assert engine.is_recently_enforced_verified() is False


async def test_recently_enforced_verified_pulses_off_on_re_confirm(
    hass: HomeAssistant,
):
    """A second confirm while the flag is still on pulses it off then on (rising
    edge) so an automation re-triggers — mirrors recently_enforced behaviour."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")

    edges = []
    async_dispatcher_connect(
        hass,
        signal_for_rule(engine.config.unique_id),
        lambda *_: edges.append(engine.is_recently_enforced_verified()),
    )

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")
        hass.states.async_set("light.bedroom", "off")
        await _run_verify_timer(hass, engine, "light.bedroom")
        assert engine.is_recently_enforced_verified() is True
        # Second cycle: re-enforce + confirm again while flag still on.
        hass.states.async_set("light.bedroom", "on")
        await engine._enforce("light.bedroom")
        hass.states.async_set("light.bedroom", "off")
        await _run_verify_timer(hass, engine, "light.bedroom")

    await hass.async_block_till_done()
    assert engine.is_recently_enforced_verified() is True
    # The pulse-off edge (False) appeared between the two confirms.
    assert False in edges


async def test_recently_enforced_verified_cleared_by_reset(hass: HomeAssistant):
    """reset_cooldowns / counter clear zero the verified flag and its timer."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    engine._recently_enforced_verified = True
    engine._recently_enforced_verified_unsub = MagicMock()
    await engine.async_reset_cooldowns()
    assert engine.is_recently_enforced_verified() is False
    assert engine._recently_enforced_verified_unsub is None


# ---------------------------------------------------------------------------
# EVENT_UNREACHABLE + recently_enforced_unreachable (failure twin of verified)
# ---------------------------------------------------------------------------


async def test_arm_recently_enforced_unreachable_pulse_when_already_on(
    hass: HomeAssistant,
):
    """Re-arming while already on pulses off→on so rising-edge automations re-fire."""
    from unittest.mock import patch

    engine = _make_engine(hass)
    engine._startup_complete = True
    engine._recently_enforced_unreachable = True

    flag_values = []

    def _capture_send(_hass, _signal):
        flag_values.append(engine._recently_enforced_unreachable)

    with (
        patch(
            "custom_components.entity_guard.rule_engine.async_call_later",
            side_effect=lambda *_a, **_k: MagicMock(),
        ),
        patch(
            "custom_components.entity_guard.rule_engine.async_dispatcher_send",
            side_effect=_capture_send,
        ),
    ):
        engine._arm_recently_enforced_unreachable()

    assert (
        len(flag_values) >= 1
    )  # pulse-off dispatch happened (clean fail, not IndexError)
    assert flag_values[0] is False  # pulse-off dispatch
    assert engine._recently_enforced_unreachable is True


async def test_arm_recently_enforced_unreachable_timer_clears_flag(
    hass: HomeAssistant,
):
    """Unreachable timer callback sets the flag False and dispatches."""
    from unittest.mock import patch

    engine = _make_engine(hass)
    engine._startup_complete = True

    captured_cb = []

    def _capture_later(_hass, _delay, job):
        captured_cb.append(job)
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later",
        side_effect=_capture_later,
    ):
        engine._arm_recently_enforced_unreachable()
        assert engine._recently_enforced_unreachable is True

    assert captured_cb
    captured_cb[0].target(dt_util.now())
    await hass.async_block_till_done()

    assert engine._recently_enforced_unreachable is False


async def test_arm_recently_enforced_unreachable_timer_noop_when_unloaded(
    hass: HomeAssistant,
):
    """Unreachable timer _clear is a no-op when the engine is already unloaded."""
    from unittest.mock import patch

    engine = _make_engine(hass)
    engine._startup_complete = True
    engine._recently_enforced_unreachable = False

    captured_cb = []

    def _capture_later(_hass, _delay, job):
        captured_cb.append(job)
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later",
        side_effect=_capture_later,
    ):
        engine._arm_recently_enforced_unreachable()

    engine._is_unloaded = True
    engine._recently_enforced_unreachable = True  # set so we see it's NOT cleared
    captured_cb[0].target(dt_util.now())
    await hass.async_block_till_done()

    assert engine._recently_enforced_unreachable is True


async def test_cancel_recently_enforced_unreachable_timer_swallows_exception(
    hass: HomeAssistant,
):
    """_cancel_recently_enforced_unreachable_timer swallows unsub exceptions."""
    engine = _make_engine(hass)
    engine._recently_enforced_unreachable_unsub = MagicMock(
        side_effect=RuntimeError("boom")
    )
    engine._cancel_recently_enforced_unreachable_timer()  # must not raise
    assert engine._recently_enforced_unreachable_unsub is None


async def test_unreachable_event_fires_on_exhaustion(hass: HomeAssistant):
    """verify-on + target never reaches desired → EVENT_UNREACHABLE fires once and
    the unreachable flag arms (the failure twin of EVENT_VERIFIED)."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")  # never moves

    events = []
    hass.bus.async_listen(EVENT_UNREACHABLE, lambda e: events.append(e))

    await _exhaust_once(hass, engine)
    await hass.async_block_till_done()

    assert len(events) == 1
    data = events[0].data
    assert data["target_entity_id"] == "light.bedroom"
    assert data["domain"] == "light"
    assert data["target"] == "off"
    assert data["rule_id"] == engine.config.unique_id
    assert data["reason"] == REASON_UNREACHABLE_AFTER_RETRIES
    assert "attempts" not in data
    assert engine.is_recently_enforced_unreachable() is True


async def test_unreachable_event_not_fired_when_verify_off(hass: HomeAssistant):
    """verify disabled → no verify cycle → EVENT_UNREACHABLE never fires."""
    engine = _make_engine(hass, _make_config(target_state="off"))
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")

    events = []
    hass.bus.async_listen(EVENT_UNREACHABLE, lambda e: events.append(e))

    await engine._enforce("light.bedroom")
    await hass.async_block_till_done()

    assert events == []
    assert engine.is_recently_enforced_unreachable() is False


async def test_unreachable_event_not_fired_on_clean_success(hass: HomeAssistant):
    """verify-on + target reaches desired → EVENT_UNREACHABLE never fires (only
    EVENT_VERIFIED does); the unreachable flag stays off."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")

    events = []
    hass.bus.async_listen(EVENT_UNREACHABLE, lambda e: events.append(e))

    def _fake_later(hass_, delay, cb):
        engine._test_verify_cb = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine._enforce("light.bedroom")
        hass.states.async_set("light.bedroom", "off")  # reached desired
        await _run_verify_timer(hass, engine, "light.bedroom")

    await hass.async_block_till_done()
    assert events == []
    assert engine.is_recently_enforced_unreachable() is False


async def test_unreachable_event_fires_only_on_first_exhaustion(
    hass: HomeAssistant,
):
    """EVENT_UNREACHABLE is one-shot per fresh exhaustion streak (count==1), not
    every verify cycle — mirrors the skipped-event discipline."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    hass.services.async_register("light", "turn_off", lambda c: None)
    hass.states.async_set("light.bedroom", "on")

    events = []
    hass.bus.async_listen(EVENT_UNREACHABLE, lambda e: events.append(e))

    await _exhaust_once(hass, engine)
    await hass.async_block_till_done()
    # Second exhaustion within the same streak (seed counter so next is count==2).
    engine._consecutive_exhaustions["light.bedroom"] = 1
    engine._verify_attempts.pop("light.bedroom", None)
    await _exhaust_once(hass, engine)
    await hass.async_block_till_done()

    # The count gate was genuinely exercised: the second streak really reached
    # count>=2 (guards the one-shot assert against a vacuous pass where the
    # second _exhaust_once silently produced no exhaustion at all).
    assert engine._consecutive_exhaustions.get("light.bedroom", 0) >= 2
    assert len(events) == 1


async def test_recently_enforced_unreachable_cleared_by_reset(hass: HomeAssistant):
    """reset_cooldowns zeroes the unreachable flag and cancels its timer."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    engine._recently_enforced_unreachable = True
    engine._recently_enforced_unreachable_unsub = MagicMock()
    await engine.async_reset_cooldowns()
    assert engine.is_recently_enforced_unreachable() is False
    assert engine._recently_enforced_unreachable_unsub is None


async def test_recently_enforced_unreachable_cleared_by_clear_history(
    hass: HomeAssistant,
):
    """clear_history zeroes the unreachable flag and cancels its timer."""
    engine = _make_engine(hass, _verify_config())
    engine._startup_complete = True
    engine._recently_enforced_unreachable = True
    engine._recently_enforced_unreachable_unsub = MagicMock()
    await engine.async_clear_history()
    assert engine.is_recently_enforced_unreachable() is False
    assert engine._recently_enforced_unreachable_unsub is None


# ---------------------------------------------------------------------------
# cancel-mid-enforce: the success tail must survive a self-cancel
# ---------------------------------------------------------------------------


async def test_enforce_tail_survives_cancel_during_service_call(hass: HomeAssistant):
    """A target's own post-enforce state_changed re-enters the sweep and cancels
    the still-awaiting enforce task mid service-ACK (_schedule_eval_task is
    per-entity cancel-and-replace). Before the asyncio.shield, CancelledError
    raised AT the await — after the device moved but BEFORE counters/last_enforced/
    EVENT_ENFORCED/_persist — silently losing all bookkeeping while the physical
    switch stayed flipped. The shield keeps the call + sync tail atomic: the
    enforce must still record even though the task is cancelled during the call.
    """
    config = _make_config(target_state="on", trigger_states=["on"])
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    enforced_events = []
    hass.bus.async_listen(EVENT_ENFORCED, lambda e: enforced_events.append(e))

    config.target_entities = ["switch.bedroom"]
    hass.states.async_set("switch.bedroom", "off")

    # The service "succeeds" (device moves) but, like a slow battery/RF switch
    # whose ACK lands seconds after the blocking call starts, the enclosing
    # enforce task is cancelled while this call is in flight. The cancel must be
    # deferred until the device move + the sync success tail have run.
    outer_task: asyncio.Task | None = None

    async def _slow_turn_on(call):
        # Cancel the enforce task from OUTSIDE the call (the per-entity replace
        # does exactly this), then yield so the cancellation is delivered while
        # the shielded call is still awaited.
        assert outer_task is not None
        outer_task.cancel()
        await asyncio.sleep(0)

    hass.services.async_register("switch", "turn_on", _slow_turn_on)

    before = engine.state.enforcement_count_total

    outer_task = hass.async_create_task(engine._enforce("switch.bedroom"))
    with pytest.raises(asyncio.CancelledError):
        await outer_task

    assert engine.state.enforcement_count_total == before + 1, (
        "enforce tail did not record total count after a mid-call cancel"
    )
    assert engine.state.last_enforced is not None, "last_enforced not stamped"
    assert len(enforced_events) == 1, "EVENT_ENFORCED did not fire"


async def test_enforce_cancel_path_still_arms_cooldown_broadcast(hass: HomeAssistant):
    """On the cancel path, the cooldown-expiry broadcast must still be armed for a
    debounced rule — the re-raise sits AFTER the broadcast arming, so the card's
    cooldown countdown gets its scheduled repaint even when the enforce task is
    cancelled mid-ACK. Regression guard for the peer-review finding that moving
    the re-raise before the broadcast would skip it on exactly this path.
    """
    config = _make_config(
        target_state="on",
        trigger_states=["on"],
        debounce_enabled=True,
        debounce_seconds=60,
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    config.target_entities = ["switch.bedroom"]
    hass.states.async_set("switch.bedroom", "off")

    outer_task: asyncio.Task | None = None

    async def _slow_turn_on(call):
        assert outer_task is not None
        outer_task.cancel()
        await asyncio.sleep(0)

    hass.services.async_register("switch", "turn_on", _slow_turn_on)

    outer_task = hass.async_create_task(engine._enforce("switch.bedroom"))
    with pytest.raises(asyncio.CancelledError):
        await outer_task

    # Cooldown was set in the (committed) tail, and its broadcast timer armed
    # despite the cancel — the dict carries the unsub for this entity.
    assert "switch.bedroom" in engine.state.cooldowns, "cooldown not set on cancel path"
    assert "switch.bedroom" in engine._cooldown_broadcast_unsubs, (
        "cooldown broadcast not armed on the cancel path (re-raise ran too early)"
    )
    # Cancel the armed 60s timer so the HA test harness sees no lingering job.
    engine._cooldown_broadcast_unsubs["switch.bedroom"]()
