"""Suppression must cancel an in-flight pending enforcement (delay_seconds).

Regression: pressing Suppress while a rule was in the pending-enforcement
countdown ("Enforcing in Xs") did NOT cancel the scheduled _fire timer, so the
rule enforced anyway and the card kept showing the countdown. async_suppress and
_trigger_loop_protection now call _cancel_pending_for_entities(target_entities),
which stops the timer AND clears pending_enforcement_due (card countdown gone).
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.entity_guard.const import STATUS_SUPPRESSED
from custom_components.entity_guard.models import RuleConfig
from custom_components.entity_guard.rule_engine import RuleEngine
from custom_components.entity_guard.storage import EntityGuardStore


def _make_config(**overrides) -> RuleConfig:
    defaults = {
        "unique_id": "rule-uid",
        "name": "Rule",
        "target_entities": ["light.bedroom"],
        "mode": "fixed_state",
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


async def test_suppress_cancels_pending_fire_target_not_enforced(hass: HomeAssistant):
    """Suppress during the pending countdown → the real _fire timer never enforces.

    Arms a real async_call_later timer (delay_seconds=30), suppresses, then
    advances the clock past the delay. The _fire callback must not run _enforce.
    """
    engine = _make_engine(hass, _make_config(delay_seconds=30))
    engine._enforce = AsyncMock()
    hass.states.async_set("light.bedroom", "on")

    engine._schedule_delayed_enforcement("light.bedroom")
    assert "light.bedroom" in engine._pending_enforcements
    assert "light.bedroom" in engine.pending_enforcement_due()

    await engine.async_suppress(10)

    # Timer was cancelled: advancing past the delay must not fire enforcement.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
    engine._enforce.assert_not_awaited()
    await engine.async_unload()


async def test_suppress_clears_pending_due_card_countdown_gone(hass: HomeAssistant):
    """Suppress clears pending_enforcement_due so the card countdown disappears."""
    engine = _make_engine(hass, _make_config(delay_seconds=30))
    hass.states.async_set("light.bedroom", "on")

    engine._schedule_delayed_enforcement("light.bedroom")
    assert "light.bedroom" in engine.pending_enforcement_due()

    await engine.async_suppress(10)

    assert engine.pending_enforcement_due() == {}
    assert engine._pending_enforcements == {}
    assert engine.current_status() == STATUS_SUPPRESSED
    await engine.async_unload()


async def test_suppress_cancels_pending_for_all_targets(hass: HomeAssistant):
    """Multi-target rule: suppress cancels every armed target, not just one."""
    config = _make_config(
        delay_seconds=30, target_entities=["light.a", "light.b", "light.c"]
    )
    engine = _make_engine(hass, config)
    engine._enforce = AsyncMock()
    for eid in ("light.a", "light.b", "light.c"):
        hass.states.async_set(eid, "on")
        engine._schedule_delayed_enforcement(eid)
    assert set(engine.pending_enforcement_due()) == {"light.a", "light.b", "light.c"}

    await engine.async_suppress(10)

    assert engine.pending_enforcement_due() == {}
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
    engine._enforce.assert_not_awaited()
    await engine.async_unload()


async def test_loop_protection_cancels_pending(hass: HomeAssistant):
    """Loop-protection auto-suppress also cancels pending enforcements.

    _trigger_loop_protection sets suppression and must stop any armed _fire
    timers, same as manual suppress.
    """
    engine = _make_engine(hass, _make_config(delay_seconds=30))
    hass.states.async_set("light.bedroom", "on")
    # Arm a second (different) target so loop-protection's target-wide cancel is
    # exercised against a live entry, not only the entity that breached.
    engine._schedule_delayed_enforcement("light.bedroom")
    assert "light.bedroom" in engine.pending_enforcement_due()

    with patch("custom_components.entity_guard.rule_engine.persistent_notification"):
        await engine._trigger_loop_protection("light.bedroom")

    assert engine.pending_enforcement_due() == {}
    assert engine._pending_enforcements == {}
    assert engine.current_status() == STATUS_SUPPRESSED
    await engine.async_unload()


async def test_fire_suppression_guard_same_tick_race(hass: HomeAssistant):
    """_fire's suppression guard covers the race the cancel can't.

    If suppressed_until is set AFTER the timer task is already queued (the
    same-tick race async_suppress's cancel cannot stop), _fire must bail without
    enforcing, pop pending + due, and re-derive idle status. Set suppressed_until
    directly (bypassing async_suppress) so no cancel runs — only the guard does.
    """
    engine = _make_engine(hass, _make_config(delay_seconds=1))
    engine._enforce = AsyncMock()
    hass.states.async_set("light.bedroom", "on")
    engine._schedule_delayed_enforcement("light.bedroom")

    # Simulate suppression landing with the timer already live/queued — no cancel.
    engine._state.suppressed_until = dt_util.now() + timedelta(minutes=10)

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()

    engine._enforce.assert_not_awaited()
    assert engine.pending_enforcement_due() == {}
    assert "light.bedroom" not in engine._pending_enforcements
    assert engine.current_status() == STATUS_SUPPRESSED


# ---------------------------------------------------------------------------
# Re-arm when suppression ends (regression for the hole the pending-cancel
# activated: clearing suppression only refreshed the status label and never
# re-evaluated a still-non-compliant target that emits no new state event, so
# the rule silently stopped guarding it). _handle_suppression_expired and
# async_unsuppress now _sweep_targets() on clear.
# ---------------------------------------------------------------------------


async def test_suppression_expiry_re_enforces_still_noncompliant_target(
    hass: HomeAssistant,
):
    """After suppression EXPIRES, a target left non-compliant (no new event) must
    be re-enforced by the clear-time sweep — not silently abandoned.
    """
    engine = _make_engine(hass, _make_config(delay_seconds=0))
    engine._enforce = AsyncMock()
    engine._startup_complete = True
    # Target non-compliant (on, target_state=off) and emits no further event.
    hass.states.async_set("light.bedroom", "on")

    # Suppression already elapsed — the expiry callback will clear it, then sweep.
    engine._state.suppressed_until = dt_util.now() - timedelta(seconds=1)
    engine._state.suppression_reason = "manual"

    engine._handle_suppression_expired(dt_util.now())
    await hass.async_block_till_done()

    assert engine._state.suppressed_until is None
    engine._enforce.assert_awaited_with("light.bedroom", trigger_context_id=None)


async def test_unsuppress_re_enforces_still_noncompliant_target(hass: HomeAssistant):
    """Manual unsuppress (early clear) must also re-arm via sweep so a
    still-non-compliant, non-changing target is re-enforced.
    """
    engine = _make_engine(hass, _make_config(delay_seconds=0))
    engine._enforce = AsyncMock()
    engine._startup_complete = True
    hass.states.async_set("light.bedroom", "on")

    engine._state.suppressed_until = dt_util.now() + timedelta(hours=1)
    engine._state.suppression_reason = "manual"

    await engine.async_unsuppress()
    await hass.async_block_till_done()

    assert engine._state.suppressed_until is None
    engine._enforce.assert_awaited_with("light.bedroom", trigger_context_id=None)


async def test_unsuppress_no_sweep_when_disabled(hass: HomeAssistant):
    """The re-arm sweep is guarded: a DISABLED rule must not re-enforce on clear
    (mirrors the enabled/master guard in set_enabled/_handle_master_changed).
    """
    engine = _make_engine(hass, _make_config(delay_seconds=0))
    engine._enforce = AsyncMock()
    engine._startup_complete = True
    engine._state.enabled = False
    hass.states.async_set("light.bedroom", "on")

    engine._state.suppressed_until = dt_util.now() + timedelta(hours=1)
    engine._state.suppression_reason = "manual"

    await engine.async_unsuppress()
    await hass.async_block_till_done()

    engine._enforce.assert_not_awaited()


# ---------------------------------------------------------------------------
# Lazy suppression expiry (adversarial review #3): a target's OWN event arrives
# after suppressed_until has passed but before the expiry timer drains. The
# inline clear in async_evaluate cancels that timer, so _handle_suppression_expired
# never sweeps; async_evaluate would otherwise re-evaluate only the triggering
# entity. The lazy-clear branch now sweeps the SIBLING targets so a still-
# non-compliant sibling emitting no event of its own is not left unguarded.
# ---------------------------------------------------------------------------


async def test_lazy_expiry_event_re_enforces_noncompliant_sibling(hass: HomeAssistant):
    """Lazy expiry via a target's own event must re-enforce a non-compliant sibling.

    Both targets non-compliant and suppressed; suppressed_until set to the PAST
    without firing _handle_suppression_expired. A state event on ONE target drives
    async_evaluate: it clears suppression inline (cancelling the expiry timer) and
    must sweep the sibling. Without 43b1d0f only the triggering target enforces.
    """
    config = _make_config(delay_seconds=0, target_entities=["light.a", "light.b"])
    engine = _make_engine(hass, config)
    engine._enforce = AsyncMock()
    engine._startup_complete = True
    # Both non-compliant (on, target_state=off). Only light.a emits a new event.
    hass.states.async_set("light.a", "on")
    hass.states.async_set("light.b", "on")

    # Suppression already elapsed but the expiry timer has NOT drained.
    engine._state.suppressed_until = dt_util.now() - timedelta(seconds=1)
    engine._state.suppression_reason = "manual"

    await engine.async_evaluate("light.a", hass.states.get("light.a"))
    await hass.async_block_till_done()

    assert engine._state.suppressed_until is None
    enforced = {c.args[0] for c in engine._enforce.await_args_list}
    # Triggering entity (fall-through) AND the sibling (lazy-clear sweep).
    assert enforced == {"light.a", "light.b"}


async def test_lazy_expiry_sibling_sweep_guarded_when_master_off(hass: HomeAssistant):
    """Lazy-clear sibling sweep is guarded: master OFF → no sibling enforcement.

    master off bails at the top of async_evaluate before the lazy-clear branch,
    so neither the triggering entity nor the sibling is enforced.
    """
    config = _make_config(delay_seconds=0, target_entities=["light.a", "light.b"])
    engine = _make_engine(hass, config, master=False)
    engine._enforce = AsyncMock()
    engine._startup_complete = True
    hass.states.async_set("light.a", "on")
    hass.states.async_set("light.b", "on")

    engine._state.suppressed_until = dt_util.now() - timedelta(seconds=1)
    engine._state.suppression_reason = "manual"

    await engine.async_evaluate("light.a", hass.states.get("light.a"))
    await hass.async_block_till_done()

    engine._enforce.assert_not_awaited()
