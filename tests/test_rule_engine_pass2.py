"""Pass-2 coverage for rule_engine startup-grace branches and delayed enforcement."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.entity_guard.const import (
    STATUS_CONDITIONAL,
    STATUS_DISABLED,
    STATUS_MASTER_DISABLED,
    STATUS_SUPPRESSED,
)
from custom_components.entity_guard.models import Flag, RuleConfig
from custom_components.entity_guard.rule_engine import RuleEngine
from custom_components.entity_guard.storage import EntityGuardStore
from tests.conftest import capturing_create_task


def _make_config(**overrides) -> RuleConfig:
    defaults = {
        "unique_id": "rule-uid",
        "name": "Rule",
        "target_entities": ["light.bedroom"],
        "mode": "state",
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


# ---------------------------------------------------------------------------
# Startup grace status branches
# ---------------------------------------------------------------------------


def test_startup_grace_sets_disabled_when_disabled(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine._state.enabled = False
    hass.states.async_set("light.bedroom", "on")
    with patch.object(hass, "async_create_task", side_effect=capturing_create_task()):
        engine._handle_startup_grace_done(MagicMock())
    assert engine.current_status() == STATUS_DISABLED


def test_startup_grace_sets_disabled_when_master_off(hass: HomeAssistant):
    engine = _make_engine(hass, master=False)
    hass.states.async_set("light.bedroom", "on")
    with patch.object(hass, "async_create_task", side_effect=capturing_create_task()):
        engine._handle_startup_grace_done(MagicMock())
    assert engine.current_status() == STATUS_MASTER_DISABLED


def test_startup_grace_sets_suppressed(hass: HomeAssistant):
    engine = _make_engine(hass)
    engine._state.suppressed_until = dt_util.now() + timedelta(minutes=10)
    hass.states.async_set("light.bedroom", "on")
    with patch.object(hass, "async_create_task", side_effect=capturing_create_task()):
        engine._handle_startup_grace_done(MagicMock())
    assert engine.current_status() == STATUS_SUPPRESSED


def test_startup_grace_sets_conditional_when_flags_mismatch(hass: HomeAssistant):
    flag = Flag(entity="input_boolean.x", match_state="on")
    engine = _make_engine(hass, _make_config(flags=[flag]))
    hass.states.async_set("input_boolean.x", "off")
    hass.states.async_set("light.bedroom", "on")
    with patch.object(hass, "async_create_task", side_effect=capturing_create_task()):
        engine._handle_startup_grace_done(MagicMock())
    assert engine.current_status() == STATUS_CONDITIONAL


# ---------------------------------------------------------------------------
# Master switch signal handler
# ---------------------------------------------------------------------------


def test_master_changed_to_off_sets_disabled(hass: HomeAssistant):
    """Engine flips to DISABLED when master toggles off, regardless of flags."""
    flag = Flag(entity="input_boolean.x", match_state="on")
    master = {"on": True}
    engine = _make_engine(hass, _make_config(flags=[flag]), master=True)
    engine._master_enabled_getter = lambda: master["on"]
    hass.states.async_set("input_boolean.x", "off")
    hass.states.async_set("light.bedroom", "on")
    # Pretend startup grace completed in conditional.
    engine._set_status(STATUS_CONDITIONAL)
    master["on"] = False
    engine._handle_master_changed()
    assert engine.current_status() == STATUS_MASTER_DISABLED


def test_master_changed_to_on_re_derives(hass: HomeAssistant):
    """Engine flips back to CONDITIONAL/ARMED when master toggles on."""
    flag = Flag(entity="input_boolean.x", match_state="on")
    master = {"on": False}
    engine = _make_engine(hass, _make_config(flags=[flag]))
    engine._master_enabled_getter = lambda: master["on"]
    hass.states.async_set("input_boolean.x", "off")
    hass.states.async_set("light.bedroom", "on")
    engine._set_status(STATUS_DISABLED)
    master["on"] = True
    engine._handle_master_changed()
    assert engine.current_status() == STATUS_CONDITIONAL


def test_set_enabled_true_when_master_off_stays_master_disabled(
    hass: HomeAssistant,
):
    """set_enabled(True) must NOT override master-off (regression)."""
    master = {"on": False}
    engine = _make_engine(hass, _make_config())
    engine._master_enabled_getter = lambda: master["on"]
    hass.states.async_set("light.bedroom", "on")
    engine._set_status(STATUS_MASTER_DISABLED)
    engine.set_enabled(True)
    assert engine.current_status() == STATUS_MASTER_DISABLED


def test_set_enabled_false_when_master_off_uses_master_disabled(
    hass: HomeAssistant,
):
    """set_enabled(False) with master off uses MASTER_DISABLED, not DISABLED."""
    master = {"on": False}
    engine = _make_engine(hass, _make_config())
    engine._master_enabled_getter = lambda: master["on"]
    hass.states.async_set("light.bedroom", "on")
    engine.set_enabled(False)
    assert engine.current_status() == STATUS_MASTER_DISABLED


# ---------------------------------------------------------------------------
# Delayed enforcement _fire branches
# ---------------------------------------------------------------------------


async def test_schedule_delayed_fire_via_async_fire_time_changed(hass: HomeAssistant):
    """Use async_fire_time_changed to invoke the scheduled callback."""
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    engine = _make_engine(hass, _make_config(delay_seconds=1))
    engine._enforce = AsyncMock()
    hass.states.async_set("light.bedroom", "on")
    engine._schedule_delayed_enforcement("light.bedroom")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    engine._enforce.assert_awaited_once_with("light.bedroom", trigger_context_id=None)


async def test_schedule_delayed_fire_no_longer_triggered(hass: HomeAssistant):
    """Entity no longer in trigger state when fire runs → skip enforce."""
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    engine = _make_engine(hass, _make_config(delay_seconds=1))
    engine._enforce = AsyncMock()
    hass.states.async_set("light.bedroom", "on")
    engine._schedule_delayed_enforcement("light.bedroom")
    # Change state to non-triggered before timer fires
    hass.states.async_set("light.bedroom", "off")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    engine._enforce.assert_not_awaited()


async def test_schedule_delayed_fire_state_gone(hass: HomeAssistant):
    """Entity state removed before fire → skip enforce."""
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    engine = _make_engine(hass, _make_config(delay_seconds=1))
    engine._enforce = AsyncMock()
    hass.states.async_set("light.bedroom", "on")
    engine._schedule_delayed_enforcement("light.bedroom")
    hass.states.async_remove("light.bedroom")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    engine._enforce.assert_not_awaited()


async def test_schedule_delayed_fire_flags_mismatch(hass: HomeAssistant):
    """Flags mismatch when fire runs → skip enforce."""
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    flag = Flag(entity="input_boolean.x", match_state="on")
    engine = _make_engine(hass, _make_config(delay_seconds=1, flags=[flag]))
    engine._enforce = AsyncMock()
    hass.states.async_set("input_boolean.x", "on")
    hass.states.async_set("light.bedroom", "on")
    engine._schedule_delayed_enforcement("light.bedroom")
    # Flip flag before timer
    hass.states.async_set("input_boolean.x", "off")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    engine._enforce.assert_not_awaited()


# ---------------------------------------------------------------------------
# Context propagation
# ---------------------------------------------------------------------------


async def test_trigger_context_id_propagated_to_enforce(hass: HomeAssistant):
    """Context id from the triggering state_changed event reaches _enforce as
    parent_id, so HA logbook can chain the enforcement back to its origin."""
    from homeassistant.core import Context, Event, EventOrigin

    engine = _make_engine(hass, _make_config(delay_seconds=0))
    engine._startup_complete = True
    engine._enforce = AsyncMock()

    trigger_ctx = Context()
    hass.states.async_set("light.bedroom", "on")
    event = Event(
        "state_changed",
        {"entity_id": "light.bedroom", "new_state": hass.states.get("light.bedroom")},
        EventOrigin.local,
        context=trigger_ctx,
    )
    engine._handle_state_event(event)
    await hass.async_block_till_done()

    engine._enforce.assert_awaited_once_with(
        "light.bedroom", trigger_context_id=trigger_ctx.id
    )


async def test_enforce_sets_parent_id_on_service_context(hass: HomeAssistant):
    """_enforce builds a Context with parent_id=trigger_context_id so the
    service call carries the chain back to the state_changed event."""
    from unittest.mock import patch

    from homeassistant.core import Context

    engine = _make_engine(hass, _make_config(delay_seconds=0))
    engine._startup_complete = True
    hass.states.async_set("light.bedroom", "on")

    service_ctx_captured = []

    async def _capture_ctx(domain, service, data, *, blocking, context):
        service_ctx_captured.append(context)

    trigger_ctx_id = "test-trigger-ulid-1234"
    with patch(
        "homeassistant.core.ServiceRegistry.async_call",
        side_effect=_capture_ctx,
    ):
        await engine._enforce("light.bedroom", trigger_context_id=trigger_ctx_id)

    assert len(service_ctx_captured) == 1
    ctx = service_ctx_captured[0]
    assert isinstance(ctx, Context)
    assert ctx.parent_id == trigger_ctx_id
