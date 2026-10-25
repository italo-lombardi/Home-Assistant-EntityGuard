"""Engine wiring for per-direction dynamic_state delay.

async_evaluate resolves config.effective_delay(_follow_desired_on()) ONCE and reuses
it for the PENDING-vs-immediate gate AND the scheduled timer duration. These tests
drive a dynamic_state rule in each direction and assert the ARMED countdown matches
that direction's delay — not merely that *a* delay happened.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.entity_guard.const import (
    MODE_DYNAMIC_STATE,
    SOURCE_VALUE_FROM_ATTRIBUTE,
    SOURCE_VALUE_FROM_STATE,
    STATUS_PENDING,
)
from custom_components.entity_guard.models import RuleConfig
from custom_components.entity_guard.rule_engine import RuleEngine
from custom_components.entity_guard.storage import EntityGuardStore


def _make_config(**overrides) -> RuleConfig:
    defaults = {
        "unique_id": "rule-uid",
        "name": "Follow",
        "target_entities": ["light.target"],
        "mode": MODE_DYNAMIC_STATE,
        "trigger_states": [],
        "target_state": None,
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
        "delay_on_seconds": None,
        "delay_off_seconds": None,
    }
    defaults.update(overrides)
    return RuleConfig(**defaults)


def _make_engine(hass, config, master=True) -> RuleEngine:
    store = MagicMock(spec=EntityGuardStore)
    store.get_rule_state.return_value = {}
    store.set_rule_state.return_value = None
    store.runtime_to_blob = EntityGuardStore.runtime_to_blob
    store.blob_to_runtime = EntityGuardStore.blob_to_runtime
    engine = RuleEngine(hass, config, store, lambda: master)
    engine._startup_complete = True
    return engine


def _scheduled_delay(engine, entity_id) -> float:
    """Seconds until the pending enforcement for entity_id is due (rounded)."""
    due = engine.pending_enforcement_due()[entity_id]
    return round((due - dt_util.now()).total_seconds())


async def test_driving_on_uses_delay_on(hass: HomeAssistant):
    """Source ON (desired on), target OFF → driving target ON → delay_on countdown."""
    config = _make_config(delay_on_seconds=10, delay_off_seconds=90)
    engine = _make_engine(hass, config)
    engine._enforce = AsyncMock()
    hass.states.async_set("binary_sensor.motion", "on")  # desired_on = True
    hass.states.async_set("light.target", "off")  # non-compliant → drive ON

    await engine.async_evaluate("light.target", hass.states.get("light.target"))

    assert engine.current_status() == STATUS_PENDING
    assert _scheduled_delay(engine, "light.target") == 10
    await engine.async_unload()


async def test_driving_off_uses_delay_off(hass: HomeAssistant):
    """Source OFF (desired off), target ON → driving target OFF → delay_off countdown."""
    config = _make_config(delay_on_seconds=10, delay_off_seconds=90)
    engine = _make_engine(hass, config)
    engine._enforce = AsyncMock()
    hass.states.async_set("binary_sensor.motion", "off")  # desired_on = False
    hass.states.async_set("light.target", "on")  # non-compliant → drive OFF

    await engine.async_evaluate("light.target", hass.states.get("light.target"))

    assert engine.current_status() == STATUS_PENDING
    assert _scheduled_delay(engine, "light.target") == 90
    await engine.async_unload()


async def test_zero_one_direction_goes_immediate_other_waits(hass: HomeAssistant):
    """delay_on=0 → driving ON enforces immediately (no PENDING); delay_off=30 waits.

    This is the load-bearing reuse: the PENDING-vs-immediate GATE and the duration
    must both read the per-direction delay, so one direction can be immediate while
    the other is delayed.
    """
    config = _make_config(delay_on_seconds=0, delay_off_seconds=30)
    engine = _make_engine(hass, config)
    engine._enforce = AsyncMock()

    # Drive ON with delay_on=0 → immediate enforce, no pending countdown.
    hass.states.async_set("binary_sensor.motion", "on")
    hass.states.async_set("light.target", "off")
    await engine.async_evaluate("light.target", hass.states.get("light.target"))
    engine._enforce.assert_awaited_once()
    assert engine.pending_enforcement_due() == {}

    engine._enforce.reset_mock()

    # Drive OFF with delay_off=30 → PENDING countdown of 30, not immediate.
    hass.states.async_set("binary_sensor.motion", "off")
    hass.states.async_set("light.target", "on")
    await engine.async_evaluate("light.target", hass.states.get("light.target"))
    engine._enforce.assert_not_awaited()
    assert engine.current_status() == STATUS_PENDING
    assert _scheduled_delay(engine, "light.target") == 30
    await engine.async_unload()


async def test_unset_direction_falls_back_to_shared_delay(hass: HomeAssistant):
    """delay_off unset → driving OFF uses the shared delay_seconds (not 0/immediate)."""
    config = _make_config(delay_seconds=25, delay_on_seconds=5, delay_off_seconds=None)
    engine = _make_engine(hass, config)
    engine._enforce = AsyncMock()
    hass.states.async_set("binary_sensor.motion", "off")  # desired off
    hass.states.async_set("light.target", "on")  # drive OFF

    await engine.async_evaluate("light.target", hass.states.get("light.target"))

    assert engine.current_status() == STATUS_PENDING
    assert _scheduled_delay(engine, "light.target") == 25
    await engine.async_unload()


async def test_invert_keys_delay_to_target_direction_not_source(hass: HomeAssistant):
    """follow_invert=True: the delay follows the TARGET's driven direction, not the source.

    Locks the subtlest property — _follow_desired_on applies invert LAST, so the value
    effective_delay sees IS the desired target state. With invert, source OFF means
    desired target ON, so driving the target ON must use delay_on (10), not delay_off (90).
    If the mapping were keyed to the source (source off → "off" → delay_off) it would be 90.
    """
    config = _make_config(follow_invert=True, delay_on_seconds=10, delay_off_seconds=90)
    engine = _make_engine(hass, config)
    engine._enforce = AsyncMock()
    hass.states.async_set("binary_sensor.motion", "off")  # invert → desired target ON
    hass.states.async_set("light.target", "off")  # target off → must be driven ON

    await engine.async_evaluate("light.target", hass.states.get("light.target"))

    assert engine.current_status() == STATUS_PENDING
    assert _scheduled_delay(engine, "light.target") == 10  # delay_on, not 90
    await engine.async_unload()

    # And the mirror: source ON + invert → desired target OFF → delay_off (90).
    engine2 = _make_engine(hass, config)
    engine2._enforce = AsyncMock()
    hass.states.async_set("binary_sensor.motion", "on")  # invert → desired target OFF
    hass.states.async_set("light.target", "on")  # target on → must be driven OFF
    await engine2.async_evaluate("light.target", hass.states.get("light.target"))
    assert _scheduled_delay(engine2, "light.target") == 90  # delay_off
    await engine2.async_unload()


async def test_attribute_source_resolves_direction_and_delay(hass: HomeAssistant):
    """source_value_from=attribute: desired direction (and thus the delay) comes from a
    source ATTRIBUTE reduced to on/off, same per-direction pick as state-sourced rules."""
    config = _make_config(
        source_value_from=SOURCE_VALUE_FROM_ATTRIBUTE,
        source_attribute="occupancy",
        delay_on_seconds=15,
        delay_off_seconds=75,
    )
    engine = _make_engine(hass, config)
    engine._enforce = AsyncMock()
    # Attribute truthy → desired target ON; target off → drive ON → delay_on.
    hass.states.async_set("binary_sensor.motion", "on", {"occupancy": True})
    hass.states.async_set("light.target", "off")

    await engine.async_evaluate("light.target", hass.states.get("light.target"))

    assert engine.current_status() == STATUS_PENDING
    assert _scheduled_delay(engine, "light.target") == 15
    await engine.async_unload()

    # Attribute falsy → desired target OFF; target on → drive OFF → delay_off.
    engine2 = _make_engine(hass, config)
    engine2._enforce = AsyncMock()
    hass.states.async_set("binary_sensor.motion", "on", {"occupancy": False})
    hass.states.async_set("light.target", "on")
    await engine2.async_evaluate("light.target", hass.states.get("light.target"))
    assert _scheduled_delay(engine2, "light.target") == 75
    await engine2.async_unload()


async def test_delay_is_pinned_to_arming_direction_until_fire(hass: HomeAssistant):
    """The countdown length is resolved ONCE at arm time (async_evaluate line ~468)
    and is NOT re-resolved mid-countdown.

    Semantics this pins (raised in review): effective_delay runs at schedule time
    for the direction that armed the timer. A later SOURCE flip does not re-resolve
    the already-scheduled duration. When the real _fire callback runs it re-checks
    _trigger_decision against the CURRENT states — so a flip that makes the target
    compliant cancels cleanly (no enforce), and a flip that leaves it non-compliant
    toward the *same* direction still fires on the originally-armed delay. There is
    no path where _fire enforces a direction with that direction's delay unobserved,
    because re-arming on a genuinely new direction goes through async_evaluate again
    (fresh effective_delay). This test locks the arm-time pin + the compliant-flip
    cancel so a refactor can't silently start re-resolving mid-countdown.
    """
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    config = _make_config(delay_on_seconds=10, delay_off_seconds=90)
    engine = _make_engine(hass, config)
    engine._enforce = AsyncMock()

    # Arm ON: source on (desired on), target off → drive ON, delay_on=10 scheduled.
    hass.states.async_set("binary_sensor.motion", "on")
    hass.states.async_set("light.target", "off")
    await engine.async_evaluate("light.target", hass.states.get("light.target"))
    assert engine.current_status() == STATUS_PENDING
    assert _scheduled_delay(engine, "light.target") == 10  # armed on delay_on

    # Mid-countdown the SOURCE flips off → desired now off. The target is still OFF,
    # so it is now COMPLIANT with the new desired. The scheduled timer still carries
    # the arm-time delay_on (10), NOT delay_off (90) — not re-resolved.
    hass.states.async_set("binary_sensor.motion", "off")
    # Advance past the ORIGINAL 10s delay; if the duration had been re-resolved to
    # delay_off=90 the timer would not fire here at all.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done()

    # _fire re-ran _trigger_decision: target off vs desired off → compliant → bailed,
    # no enforce, pending cleared. Proves the fire used the 10s arm-time schedule
    # (it fired) and the direction is decided fresh at fire (it did not enforce OFF).
    engine._enforce.assert_not_awaited()
    assert engine.pending_enforcement_due() == {}
    await engine.async_unload()
