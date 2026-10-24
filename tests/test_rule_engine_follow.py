"""Tests for RuleEngine dynamic modes (MODE_DYNAMIC_STATE + MODE_DYNAMIC_ATTRIBUTE).

Dynamic modes mirror the fixed-mode enforcement path exactly, except the target
value is read live from a source entity instead of from config:
- dynamic_state: target on/off mirrors (or inverts) the source's on/off.
- dynamic_attribute: a target attribute mirrors a live source value (the source's
  state, or one of its attributes).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util

from custom_components.entity_guard.const import (
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    EVENT_ENFORCED,
    EVENT_SKIPPED,
    MODE_DYNAMIC_ATTRIBUTE,
    MODE_DYNAMIC_STATE,
    MODE_FIXED_ATTRIBUTE,
    MODE_FIXED_STATE,
    SOURCE_VALUE_FROM_ATTRIBUTE,
    SOURCE_VALUE_FROM_STATE,
    STATUS_CONDITIONAL,
    STATUS_PENDING,
)
from custom_components.entity_guard.models import Flag, RuleConfig
from custom_components.entity_guard.rule_engine import (
    RuleEngine,
    _value_is_on,
    signal_for_rule,
)
from custom_components.entity_guard.storage import EntityGuardStore


def _make_config(**overrides) -> RuleConfig:
    defaults = {
        "name": "Dynamic Rule",
        "unique_id": "dyn-uid",
        "target_entities": ["light.hallway"],
        "mode": MODE_DYNAMIC_STATE,
        "trigger_states": [],
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
        "source_entity": "binary_sensor.motion",
        "follow_invert": False,
        "source_value_from": SOURCE_VALUE_FROM_STATE,
        "source_attribute": None,
    }
    defaults.update(overrides)
    return RuleConfig(**defaults)


def _make_attr_config(**overrides) -> RuleConfig:
    """Config helper for dynamic_attribute: target attribute = brightness."""
    base = {
        "name": "Dynamic Attr Rule",
        "unique_id": "dyn-attr-uid",
        "mode": MODE_DYNAMIC_ATTRIBUTE,
        "attribute": "brightness",
        "source_value_from": SOURCE_VALUE_FROM_STATE,
        "source_attribute": None,
    }
    base.update(overrides)
    return _make_config(**base)


def _make_engine(hass, config=None, master=True) -> RuleEngine:
    config = config or _make_config()
    store = MagicMock(spec=EntityGuardStore)
    store.get_rule_state.return_value = {}
    store.set_rule_state.return_value = None
    store.runtime_to_blob = EntityGuardStore.runtime_to_blob
    store.blob_to_runtime = EntityGuardStore.blob_to_runtime
    return RuleEngine(hass, config, store, lambda: master)


def _collect_turn_on_off(hass):
    """Register homeassistant + light turn_on/off capture; return calls list."""
    calls = []

    async def _rec(call):
        calls.append(call)

    for domain in ("homeassistant", "light", "switch"):
        hass.services.async_register(domain, "turn_on", _rec)
        hass.services.async_register(domain, "turn_off", _rec)
    return calls


def _collect_light_turn_on(hass):
    """Register light.turn_on capture (dynamic_attribute writes brightness)."""
    calls = []

    async def _rec(call):
        calls.append(call)

    hass.services.async_register("light", "turn_on", _rec)
    return calls


def _state_obj(state_str, attributes=None):
    s = MagicMock()
    s.state = state_str
    s.attributes = attributes or {}
    return s


# ===========================================================================
# dynamic_state
# ===========================================================================


# ---------------------------------------------------------------------------
# _follow_desired_on normalizer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "src_state,invert,expected",
    [
        ("on", False, True),
        ("off", False, False),
        ("home", False, True),
        ("not_home", False, False),
        ("open", False, True),
        ("playing", False, True),
        ("paused", False, False),  # ambiguous pinned off
        ("idle", False, False),
        ("docked", False, False),
        ("on", True, False),  # invert
        ("off", True, True),  # invert
        ("", False, False),  # live empty string -> off (not offline)
    ],
)
async def test_follow_desired_on_normalizer(hass, src_state, invert, expected):
    config = _make_config(follow_invert=invert)
    engine = _make_engine(hass, config)
    hass.states.async_set("binary_sensor.motion", src_state)
    assert engine._follow_desired_on() is expected


async def test_follow_desired_on_offline_returns_none(hass):
    engine = _make_engine(hass)
    hass.states.async_set("binary_sensor.motion", "unavailable")
    assert engine._follow_desired_on() is None
    hass.states.async_set("binary_sensor.motion", "unknown")
    assert engine._follow_desired_on() is None


async def test_follow_desired_on_missing_source_returns_none(hass):
    engine = _make_engine(hass)
    assert engine._follow_desired_on() is None


async def test_follow_desired_on_no_source_configured(hass):
    engine = _make_engine(hass, _make_config(source_entity=None))
    assert engine._follow_desired_on() is None


async def test_public_follow_desired_on(hass):
    engine = _make_engine(hass)
    hass.states.async_set("binary_sensor.motion", "on")
    assert engine.follow_desired_on() is True


async def test_public_follow_desired_on_none_for_non_dynamic_state(hass):
    engine = _make_engine(hass, _make_config(mode=MODE_FIXED_STATE))
    assert engine.follow_desired_on() is None


# ---------------------------------------------------------------------------
# mirror / invert enforcement
# ---------------------------------------------------------------------------


async def test_mirror_source_on_turns_target_on(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine._startup_complete = True
    calls = _collect_turn_on_off(hass)
    hass.states.async_set("light.hallway", "off")
    hass.states.async_set("binary_sensor.motion", "on")
    await engine.async_evaluate(
        "binary_sensor.motion", hass.states.get("binary_sensor.motion")
    )
    await hass.async_block_till_done()
    assert calls, "expected a turn_on service call"
    assert calls[0].service == "turn_on"
    assert calls[0].data["entity_id"] == "light.hallway"


async def test_mirror_source_off_turns_target_off(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine._startup_complete = True
    calls = _collect_turn_on_off(hass)
    hass.states.async_set("light.hallway", "on")
    hass.states.async_set("binary_sensor.motion", "off")
    await engine.async_evaluate(
        "binary_sensor.motion", hass.states.get("binary_sensor.motion")
    )
    await hass.async_block_till_done()
    assert calls
    assert calls[0].service == "turn_off"


async def test_invert_source_on_turns_target_off(hass: HomeAssistant):
    engine = _make_engine(hass, _make_config(follow_invert=True))
    engine._startup_complete = True
    calls = _collect_turn_on_off(hass)
    hass.states.async_set("light.hallway", "on")
    hass.states.async_set("binary_sensor.motion", "on")
    await engine.async_evaluate(
        "binary_sensor.motion", hass.states.get("binary_sensor.motion")
    )
    await hass.async_block_till_done()
    assert calls
    assert calls[0].service == "turn_off"


# ---------------------------------------------------------------------------
# idempotence / offline / source==target
# ---------------------------------------------------------------------------


async def test_idempotent_no_call_when_target_already_matches(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine._startup_complete = True
    calls = _collect_turn_on_off(hass)
    hass.states.async_set("light.hallway", "on")
    hass.states.async_set("binary_sensor.motion", "on")
    await engine.async_evaluate(
        "binary_sensor.motion", hass.states.get("binary_sensor.motion")
    )
    await hass.async_block_till_done()
    assert not calls


async def test_source_offline_no_enforcement(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine._startup_complete = True
    calls = _collect_turn_on_off(hass)
    hass.states.async_set("light.hallway", "off")
    hass.states.async_set("binary_sensor.motion", "unavailable")
    await engine.async_evaluate(
        "binary_sensor.motion", hass.states.get("binary_sensor.motion")
    )
    await hass.async_block_till_done()
    assert not calls


async def test_dynamic_state_source_is_target_skip_reason(hass: HomeAssistant):
    config = _make_config(
        target_entities=["binary_sensor.motion"],
        source_entity="binary_sensor.motion",
    )
    engine = _make_engine(hass, config)
    triggered, reason = engine._dynamic_state_trigger_decision(
        "binary_sensor.motion", _state_obj("on")
    )
    assert triggered is False
    assert reason == "source_is_target"


async def test_dynamic_state_target_offline_no_compare(hass: HomeAssistant):
    engine = _make_engine(hass)
    hass.states.async_set("binary_sensor.motion", "on")
    triggered, reason = engine._dynamic_state_trigger_decision(
        "light.hallway", _state_obj("unavailable")
    )
    assert triggered is False
    assert reason is None
    triggered, reason = engine._dynamic_state_trigger_decision("light.hallway", None)
    assert triggered is False


# ---------------------------------------------------------------------------
# source-driven sweep + broadcast
# ---------------------------------------------------------------------------


async def test_source_change_broadcasts_when_status_unchanged(hass: HomeAssistant):
    """A source change moves follow_desired_on (exposed on the status sensor for
    the card). If overall status stays the same bucket, _set_status dedupes — the
    engine must force a broadcast so the stale desired value doesn't stick."""
    engine = _make_engine(hass)
    engine._startup_complete = True
    _collect_turn_on_off(hass)
    hass.states.async_set("light.hallway", "on")
    hass.states.async_set("binary_sensor.motion", "on")
    await engine.async_evaluate(
        "binary_sensor.motion", hass.states.get("binary_sensor.motion")
    )

    broadcasts: list[None] = []
    unsub = async_dispatcher_connect(
        hass,
        signal_for_rule(engine.config.unique_id),
        lambda *_a: broadcasts.append(None),
    )
    try:
        hass.states.async_set("binary_sensor.motion", "off")
        await engine.async_evaluate(
            "binary_sensor.motion", hass.states.get("binary_sensor.motion")
        )
        await hass.async_block_till_done()
    finally:
        unsub()
    assert broadcasts, "source change must broadcast so follow_desired_on refreshes"


# ---------------------------------------------------------------------------
# flags gating
# ---------------------------------------------------------------------------


async def test_dynamic_state_flags_gate_conditional_no_enforce(hass: HomeAssistant):
    config = _make_config(flags=[Flag(entity="input_boolean.guard", match_state="on")])
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    calls = _collect_turn_on_off(hass)
    hass.states.async_set("input_boolean.guard", "off")  # flag unmet
    hass.states.async_set("binary_sensor.motion", "on")
    hass.states.async_set("light.hallway", "off")
    await engine.async_evaluate(
        "binary_sensor.motion", hass.states.get("binary_sensor.motion")
    )
    await hass.async_block_till_done()
    assert engine.current_status() == STATUS_CONDITIONAL
    assert not calls


async def test_dynamic_state_flags_met_enforces(hass: HomeAssistant):
    config = _make_config(flags=[Flag(entity="input_boolean.guard", match_state="on")])
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    calls = _collect_turn_on_off(hass)
    hass.states.async_set("input_boolean.guard", "on")
    hass.states.async_set("light.hallway", "off")
    hass.states.async_set("binary_sensor.motion", "on")
    await engine.async_evaluate(
        "binary_sensor.motion", hass.states.get("binary_sensor.motion")
    )
    await hass.async_block_till_done()
    assert calls and calls[0].service == "turn_on"


# ---------------------------------------------------------------------------
# delay / debounce
# ---------------------------------------------------------------------------


async def test_dynamic_state_delay_schedules_pending(hass: HomeAssistant):
    config = _make_config(delay_seconds=30)
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    hass.states.async_set("light.hallway", "off")
    hass.states.async_set("binary_sensor.motion", "on")
    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later"
    ) as mock_later:
        mock_later.return_value = MagicMock()
        await engine.async_evaluate(
            "binary_sensor.motion", hass.states.get("binary_sensor.motion")
        )
        await hass.async_block_till_done()
    assert engine.current_status() == STATUS_PENDING
    assert "light.hallway" in engine._pending_enforcements
    assert "binary_sensor.motion" not in engine._pending_enforcements


async def test_dynamic_state_delayed_fire_recomputes_source_flipped_back(
    hass: HomeAssistant,
):
    """A delayed enforce re-derives desired from the CURRENT source at fire time.
    Source flipped back during the delay -> target already matches -> no enforce."""
    config = _make_config(delay_seconds=30)
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    calls = _collect_turn_on_off(hass)
    hass.states.async_set("light.hallway", "off")
    hass.states.async_set("binary_sensor.motion", "on")

    captured = {}

    def _fake_later(_hass, _delay, cb):
        captured["cb"] = cb
        return MagicMock()

    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later", _fake_later
    ):
        await engine.async_evaluate(
            "binary_sensor.motion", hass.states.get("binary_sensor.motion")
        )
        await hass.async_block_till_done()
    assert engine.current_status() == STATUS_PENDING
    assert "cb" in captured

    hass.states.async_set("binary_sensor.motion", "off")
    captured["cb"](dt_util.now())
    await hass.async_block_till_done()
    assert not calls


async def test_dynamic_state_offline_cancels_pending(hass: HomeAssistant):
    config = _make_config(delay_seconds=30)
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    hass.states.async_set("light.hallway", "off")
    hass.states.async_set("binary_sensor.motion", "on")
    with patch(
        "custom_components.entity_guard.rule_engine.async_call_later"
    ) as mock_later:
        mock_later.return_value = MagicMock()
        await engine.async_evaluate(
            "binary_sensor.motion", hass.states.get("binary_sensor.motion")
        )
        await hass.async_block_till_done()
    assert "light.hallway" in engine._pending_enforcements

    hass.states.async_set("binary_sensor.motion", "unavailable")
    await engine.async_evaluate(
        "binary_sensor.motion", hass.states.get("binary_sensor.motion")
    )
    await hass.async_block_till_done()
    assert "light.hallway" not in engine._pending_enforcements


async def test_dynamic_state_debounce_sets_cooldown(hass: HomeAssistant):
    config = _make_config(debounce_enabled=True, debounce_seconds=60)
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    _collect_turn_on_off(hass)
    hass.states.async_set("light.hallway", "off")
    hass.states.async_set("binary_sensor.motion", "on")
    await engine.async_evaluate(
        "binary_sensor.motion", hass.states.get("binary_sensor.motion")
    )
    await hass.async_block_till_done()
    assert "light.hallway" in engine.state.cooldowns
    await engine.async_unload()


# ---------------------------------------------------------------------------
# _resolve_service + _event_target (dynamic_state)
# ---------------------------------------------------------------------------


async def test_resolve_service_dynamic_state_requires_override(hass: HomeAssistant):
    engine = _make_engine(hass)
    with pytest.raises(AssertionError):
        engine._resolve_service("light.hallway")


async def test_resolve_service_dynamic_state_on_off(hass: HomeAssistant):
    engine = _make_engine(hass)
    on = engine._resolve_service("light.hallway", state_override="on")
    off = engine._resolve_service("light.hallway", state_override="off")
    assert on == ("light", "turn_on", {"entity_id": "light.hallway"})
    assert off == ("light", "turn_off", {"entity_id": "light.hallway"})


async def test_enforce_source_offline_skips(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine._startup_complete = True
    skipped = []
    hass.bus.async_listen(EVENT_SKIPPED, lambda e: skipped.append(e))
    hass.states.async_set("binary_sensor.motion", "unavailable")
    hass.states.async_set("light.hallway", "off")
    await engine._enforce("light.hallway")
    await hass.async_block_till_done()
    assert any(e.data["reason"] == "source_offline" for e in skipped)


async def test_event_target_dynamic_state_string(hass: HomeAssistant):
    engine = _make_engine(hass)
    hass.states.async_set("binary_sensor.motion", "on")
    assert engine._event_target() == "follow:binary_sensor.motion:on"


async def test_event_target_uses_driven_direction_not_reread(hass: HomeAssistant):
    engine = _make_engine(hass)
    hass.states.async_set("binary_sensor.motion", "off")
    assert (
        engine._event_target(follow_direction="on") == "follow:binary_sensor.motion:on"
    )
    hass.states.async_set("binary_sensor.motion", "on")
    assert (
        engine._event_target(follow_direction="off")
        == "follow:binary_sensor.motion:off"
    )


async def test_enforced_event_target_matches_driven_direction(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine._startup_complete = True
    events = []
    hass.bus.async_listen(EVENT_ENFORCED, lambda e: events.append(e))
    _collect_turn_on_off(hass)
    hass.states.async_set("binary_sensor.motion", "on")
    hass.states.async_set("light.hallway", "off")
    await engine._enforce("light.hallway")
    await hass.async_block_till_done()
    assert events
    assert events[0].data["target"] == "follow:binary_sensor.motion:on"
    assert events[0].data["trigger"] == "dynamic_state"


async def test_setup_watches_source(hass: HomeAssistant):
    engine = _make_engine(hass)
    hass.states.async_set("binary_sensor.motion", "off")
    hass.states.async_set("light.hallway", "off")
    await engine.async_setup()
    calls = _collect_turn_on_off(hass)
    # Before startup grace completes, a source change is ignored (no enforce).
    hass.states.async_set("binary_sensor.motion", "on")
    await hass.async_block_till_done()
    assert not calls
    await engine.async_unload()


async def test_startup_grace_ignores_source(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine._startup_complete = False
    calls = _collect_turn_on_off(hass)
    hass.states.async_set("binary_sensor.motion", "on")
    hass.states.async_set("light.hallway", "off")
    await engine.async_evaluate(
        "binary_sensor.motion", hass.states.get("binary_sensor.motion")
    )
    await hass.async_block_till_done()
    assert not calls


# ===========================================================================
# dynamic_attribute
# ===========================================================================


async def test_dynamic_attr_value_from_state_drives_target(hass: HomeAssistant):
    """source.state is the live value; target brightness is set to it."""
    engine = _make_engine(hass, _make_attr_config())
    engine._startup_complete = True
    calls = _collect_light_turn_on(hass)
    hass.states.async_set("binary_sensor.motion", "150")
    hass.states.async_set("light.hallway", "on", {"brightness": 10})
    await engine.async_evaluate(
        "binary_sensor.motion", hass.states.get("binary_sensor.motion")
    )
    await hass.async_block_till_done()
    assert calls
    assert calls[0].data["entity_id"] == "light.hallway"
    assert calls[0].data["brightness"] == "150"


async def test_dynamic_attr_value_from_attribute_drives_target(hass: HomeAssistant):
    """source.attributes[source_attribute] is the live value."""
    config = _make_attr_config(
        source_entity="light.source",
        source_value_from=SOURCE_VALUE_FROM_ATTRIBUTE,
        source_attribute="brightness",
    )
    engine = _make_engine(hass, config)
    engine._startup_complete = True
    calls = _collect_light_turn_on(hass)
    hass.states.async_set("light.source", "on", {"brightness": 200})
    hass.states.async_set("light.hallway", "on", {"brightness": 10})
    await engine.async_evaluate("light.source", hass.states.get("light.source"))
    await hass.async_block_till_done()
    assert calls
    assert calls[0].data["brightness"] == 200


async def test_dynamic_attr_idempotent_when_target_matches(hass: HomeAssistant):
    engine = _make_engine(hass, _make_attr_config())
    engine._startup_complete = True
    calls = _collect_light_turn_on(hass)
    hass.states.async_set("binary_sensor.motion", "150")
    hass.states.async_set("light.hallway", "on", {"brightness": 150})
    await engine.async_evaluate(
        "binary_sensor.motion", hass.states.get("binary_sensor.motion")
    )
    await hass.async_block_till_done()
    assert not calls  # already 150 -> no re-enforce


async def test_dynamic_attr_target_off_no_action(hass: HomeAssistant):
    """Like fixed_attribute: target offline/unknown -> not-triggered."""
    engine = _make_engine(hass, _make_attr_config())
    hass.states.async_set("binary_sensor.motion", "150")
    triggered, reason = engine._dynamic_attribute_trigger_decision(
        "light.hallway", _state_obj("unavailable")
    )
    assert triggered is False
    assert reason is None
    triggered, _ = engine._dynamic_attribute_trigger_decision("light.hallway", None)
    assert triggered is False


async def test_dynamic_attr_source_offline_not_triggered(hass: HomeAssistant):
    engine = _make_engine(hass, _make_attr_config())
    hass.states.async_set("binary_sensor.motion", "unavailable")
    triggered, reason = engine._dynamic_attribute_trigger_decision(
        "light.hallway", _state_obj("on", {"brightness": 10})
    )
    assert triggered is False
    assert reason is None


async def test_dynamic_attr_source_missing_attr_not_triggered(hass: HomeAssistant):
    config = _make_attr_config(
        source_entity="light.source",
        source_value_from=SOURCE_VALUE_FROM_ATTRIBUTE,
        source_attribute="brightness",
    )
    engine = _make_engine(hass, config)
    hass.states.async_set("light.source", "on", {})  # no brightness attr
    triggered, reason = engine._dynamic_attribute_trigger_decision(
        "light.hallway", _state_obj("on", {"brightness": 10})
    )
    assert triggered is False
    assert reason is None


async def test_dynamic_attr_enforce_source_unavailable_skips(hass: HomeAssistant):
    engine = _make_engine(hass, _make_attr_config())
    engine._startup_complete = True
    skipped = []
    hass.bus.async_listen(EVENT_SKIPPED, lambda e: skipped.append(e))
    hass.states.async_set("binary_sensor.motion", "unavailable")
    hass.states.async_set("light.hallway", "on", {"brightness": 10})
    await engine._enforce("light.hallway")
    await hass.async_block_till_done()
    assert any(e.data["reason"] == "source_value_unavailable" for e in skipped)


async def test_dynamic_attr_source_is_target_skip(hass: HomeAssistant):
    config = _make_attr_config(
        target_entities=["binary_sensor.motion"],
        source_entity="binary_sensor.motion",
    )
    engine = _make_engine(hass, config)
    triggered, reason = engine._dynamic_attribute_trigger_decision(
        "binary_sensor.motion", _state_obj("on", {"brightness": 5})
    )
    assert triggered is False
    assert reason == "source_is_target"


async def test_dynamic_attr_no_attribute_configured_not_triggered(hass: HomeAssistant):
    engine = _make_engine(hass, _make_attr_config(attribute=None))
    hass.states.async_set("binary_sensor.motion", "150")
    triggered, _ = engine._dynamic_attribute_trigger_decision(
        "light.hallway", _state_obj("on", {"brightness": 10})
    )
    assert triggered is False


async def test_resolve_service_dynamic_attr_requires_value_override(
    hass: HomeAssistant,
):
    engine = _make_engine(hass, _make_attr_config())
    with pytest.raises(AssertionError):
        engine._resolve_service("light.hallway")


async def test_resolve_service_dynamic_attr_folds_value(hass: HomeAssistant):
    engine = _make_engine(hass, _make_attr_config())
    svc = engine._resolve_service("light.hallway", value_override=123)
    assert svc is not None
    domain, service, data = svc
    assert (domain, service) == ("light", "turn_on")
    assert data["entity_id"] == "light.hallway"
    assert data["brightness"] == 123


# ---------------------------------------------------------------------------
# _attr_values_match
# ---------------------------------------------------------------------------


async def test_attr_values_match_numeric_string_vs_int(hass: HomeAssistant):
    engine = _make_engine(hass, _make_attr_config())
    assert engine._attr_values_match("brightness", "50", 50) is True
    assert engine._attr_values_match("brightness", "50", 60) is False


async def test_attr_values_match_string_fallback(hass: HomeAssistant):
    engine = _make_engine(hass, _make_attr_config(attribute="preset"))
    assert engine._attr_values_match("preset", "eco", "eco") is True
    assert engine._attr_values_match("preset", "eco", "boost") is False


async def test_attr_values_match_rgb_tolerance(hass: HomeAssistant):
    engine = _make_engine(hass, _make_attr_config(attribute=ATTR_RGB_COLOR))
    assert engine._attr_values_match(ATTR_RGB_COLOR, [255, 0, 0], [255, 2, 2]) is True
    assert engine._attr_values_match(ATTR_RGB_COLOR, [255, 0, 0], [0, 0, 255]) is False
    # Unparseable -> no match.
    assert engine._attr_values_match(ATTR_RGB_COLOR, "bad", [1, 2, 3]) is False


async def test_attr_values_match_kelvin_tolerance(hass: HomeAssistant):
    engine = _make_engine(hass, _make_attr_config(attribute=ATTR_COLOR_TEMP_KELVIN))
    assert engine._attr_values_match(ATTR_COLOR_TEMP_KELVIN, 3000, 3010) is True
    assert engine._attr_values_match(ATTR_COLOR_TEMP_KELVIN, 3000, 5000) is False
    assert engine._attr_values_match(ATTR_COLOR_TEMP_KELVIN, "bad", 3000) is False


# ---------------------------------------------------------------------------
# _dynamic_source_value
# ---------------------------------------------------------------------------


async def test_dynamic_source_value_state(hass: HomeAssistant):
    engine = _make_engine(hass, _make_attr_config())
    hass.states.async_set("binary_sensor.motion", "150")
    assert engine._dynamic_source_value() == "150"


async def test_dynamic_source_value_attribute(hass: HomeAssistant):
    config = _make_attr_config(
        source_entity="light.source",
        source_value_from=SOURCE_VALUE_FROM_ATTRIBUTE,
        source_attribute="brightness",
    )
    engine = _make_engine(hass, config)
    hass.states.async_set("light.source", "on", {"brightness": 77})
    assert engine._dynamic_source_value() == 77


async def test_dynamic_source_value_guards(hass: HomeAssistant):
    # No source configured.
    engine = _make_engine(hass, _make_attr_config(source_entity=None))
    assert engine._dynamic_source_value() is None
    # Source absent.
    engine2 = _make_engine(hass, _make_attr_config())
    assert engine2._dynamic_source_value() is None
    # Source offline.
    hass.states.async_set("binary_sensor.motion", "unavailable")
    assert engine2._dynamic_source_value() is None
    # from=attribute but no source_attribute name.
    config = _make_attr_config(
        source_value_from=SOURCE_VALUE_FROM_ATTRIBUTE, source_attribute=None
    )
    engine3 = _make_engine(hass, config)
    hass.states.async_set("binary_sensor.motion", "on", {"brightness": 5})
    assert engine3._dynamic_source_value() is None


# ---------------------------------------------------------------------------
# _event_target (dynamic_attribute)
# ---------------------------------------------------------------------------


async def test_event_target_dynamic_attr_from_state(hass: HomeAssistant):
    engine = _make_engine(hass, _make_attr_config())
    hass.states.async_set("binary_sensor.motion", "150")
    assert engine._event_target() == "follow:binary_sensor.motion->brightness=150"


async def test_event_target_dynamic_attr_from_attribute(hass: HomeAssistant):
    config = _make_attr_config(
        source_entity="light.source",
        source_value_from=SOURCE_VALUE_FROM_ATTRIBUTE,
        source_attribute="brightness",
    )
    engine = _make_engine(hass, config)
    hass.states.async_set("light.source", "on", {"brightness": 88})
    assert engine._event_target() == "follow:light.source.brightness->brightness=88"


async def test_event_target_dynamic_attr_uses_driven_value(hass: HomeAssistant):
    engine = _make_engine(hass, _make_attr_config())
    hass.states.async_set("binary_sensor.motion", "999")
    # follow_value wins over a live re-read.
    assert (
        engine._event_target(follow_value=42)
        == "follow:binary_sensor.motion->brightness=42"
    )


# ---------------------------------------------------------------------------
# dynamic_state reading a SOURCE ATTRIBUTE (common source picker) + _value_is_on
# ---------------------------------------------------------------------------


async def test_dynamic_state_from_attribute_truthy_on(hass: HomeAssistant):
    """dynamic_state reading a source attribute: a truthy value → desired ON."""
    config = _make_config(
        source_value_from=SOURCE_VALUE_FROM_ATTRIBUTE, source_attribute="brightness"
    )
    engine = _make_engine(hass, config)
    hass.states.async_set("binary_sensor.motion", "on", {"brightness": 150})
    assert engine._follow_desired_on() is True


async def test_dynamic_state_from_attribute_zero_off(hass: HomeAssistant):
    """A zero attribute value → desired OFF."""
    config = _make_config(
        source_value_from=SOURCE_VALUE_FROM_ATTRIBUTE, source_attribute="brightness"
    )
    engine = _make_engine(hass, config)
    hass.states.async_set("binary_sensor.motion", "on", {"brightness": 0})
    assert engine._follow_desired_on() is False


async def test_dynamic_state_from_attribute_invert(hass: HomeAssistant):
    """invert flips the attribute-derived on/off."""
    config = _make_config(
        source_value_from=SOURCE_VALUE_FROM_ATTRIBUTE,
        source_attribute="brightness",
        follow_invert=True,
    )
    engine = _make_engine(hass, config)
    hass.states.async_set("binary_sensor.motion", "on", {"brightness": 150})
    assert engine._follow_desired_on() is False


async def test_dynamic_state_from_attribute_missing_none(hass: HomeAssistant):
    """Missing source attribute → None (not-triggered)."""
    config = _make_config(
        source_value_from=SOURCE_VALUE_FROM_ATTRIBUTE, source_attribute="brightness"
    )
    engine = _make_engine(hass, config)
    hass.states.async_set("binary_sensor.motion", "on", {})
    assert engine._follow_desired_on() is None


async def test_dynamic_state_from_attribute_no_name_none(hass: HomeAssistant):
    """source_value_from=attribute but no source_attribute set → None."""
    config = _make_config(
        source_value_from=SOURCE_VALUE_FROM_ATTRIBUTE, source_attribute=None
    )
    engine = _make_engine(hass, config)
    hass.states.async_set("binary_sensor.motion", "on", {"brightness": 150})
    assert engine._follow_desired_on() is None


async def test_dynamic_state_from_attribute_offline_none(hass: HomeAssistant):
    """Source offline → None even with an attribute picker."""
    config = _make_config(
        source_value_from=SOURCE_VALUE_FROM_ATTRIBUTE, source_attribute="brightness"
    )
    engine = _make_engine(hass, config)
    hass.states.async_set("binary_sensor.motion", "unavailable", {"brightness": 150})
    assert engine._follow_desired_on() is None


@pytest.mark.parametrize(
    "value,expected",
    [
        (True, True),
        (False, False),
        (150, True),
        (0, False),
        (0.0, False),
        (12.5, True),
        ("on", True),
        ("home", True),
        ("off", False),
        ("false", False),
        ("0", False),
        ("none", False),
        ("", False),
        ("150", True),
        ("idle", False),  # non-on-ish word → off (matches state path)
        ("nan", False),  # non-finite numeric string → not on-ish → off
        ("inf", False),
        (float("nan"), False),  # non-finite float → off
        (float("inf"), False),
        ([1, 2, 3], True),
        (None, False),
    ],
)
def test_value_is_on(value, expected):
    assert _value_is_on(value) is expected


# ---------------------------------------------------------------------------
# attribute_desired_value accessor (card bound-row desired value)
# ---------------------------------------------------------------------------


async def test_attribute_desired_value_dynamic_attribute(hass: HomeAssistant):
    """dynamic_attribute → live source value (via _dynamic_source_value)."""
    config = _make_attr_config(
        source_value_from=SOURCE_VALUE_FROM_ATTRIBUTE, source_attribute="brightness"
    )
    engine = _make_engine(hass, config)
    hass.states.async_set("binary_sensor.motion", "on", {"brightness": 180})
    assert engine.attribute_desired_value() == 180
    # Source attribute missing → None (card shows holding).
    hass.states.async_set("binary_sensor.motion", "on", {})
    assert engine.attribute_desired_value() is None


async def test_attribute_desired_value_fixed_attribute(hass: HomeAssistant):
    """fixed_attribute → the configured target_value."""
    config = _make_config(mode=MODE_FIXED_ATTRIBUTE, attribute="brightness")
    config.target_value = 120
    engine = _make_engine(hass, config)
    assert engine.attribute_desired_value() == 120


async def test_attribute_desired_value_none_for_state_modes(hass: HomeAssistant):
    """State modes have no attribute desired value → None."""
    engine = _make_engine(hass, _make_config(mode=MODE_DYNAMIC_STATE))
    assert engine.attribute_desired_value() is None
    engine2 = _make_engine(hass, _make_config(mode=MODE_FIXED_STATE))
    assert engine2.attribute_desired_value() is None
