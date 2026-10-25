"""Hardening for the suppression-clear re-arm sweep (deferred from PR #43 review).

Round-3 review of the re-arm fix traced three interactions as "correct but never
exercised by the suite" (uniform fixtures: fixed_state, delay 0/30, debounce off):
  GAP-1  debounce_enabled=True — a swept sibling still in cooldown must NOT re-enforce.
  GAP-2  delay_seconds>0 — the clear-path sweep arms a PENDING countdown, not immediate.
  GAP-4  dynamic_state — a source-event lazy expiry re-arms each target exactly once
         (the lazy sibling sweep + the source-branch sweep both schedule; cancel-replace
         in _schedule_eval_task dedupes to one enforce).
These lock those paths so a future refactor can't silently diverge them.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.entity_guard.const import (
    MODE_DYNAMIC_STATE,
    MODE_FIXED_STATE,
    SOURCE_VALUE_FROM_STATE,
    STATUS_PENDING,
)
from custom_components.entity_guard.models import RuleConfig
from custom_components.entity_guard.rule_engine import RuleEngine
from custom_components.entity_guard.storage import EntityGuardStore


def _fixed_config(**overrides) -> RuleConfig:
    defaults = {
        "unique_id": "uid",
        "name": "Rule",
        "target_entities": ["light.a", "light.b"],
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


def _dynamic_config(**overrides) -> RuleConfig:
    cfg = {
        "unique_id": "uid",
        "name": "Follow",
        "target_entities": ["light.a", "light.b"],
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
    cfg.update(overrides)
    return RuleConfig(**cfg)


def _engine(hass, config, master=True) -> RuleEngine:
    store = MagicMock(spec=EntityGuardStore)
    store.get_rule_state.return_value = {}
    store.set_rule_state.return_value = None
    store.runtime_to_blob = EntityGuardStore.runtime_to_blob
    store.blob_to_runtime = EntityGuardStore.blob_to_runtime
    engine = RuleEngine(hass, config, store, lambda: master)
    engine._startup_complete = True
    return engine


# --------------------------------------------------------------------------- GAP-1


async def test_expiry_sweep_skips_sibling_in_cooldown(hass: HomeAssistant):
    """debounce on: a swept sibling still inside its cooldown must NOT re-enforce.

    The cooldown guard (async_evaluate line ~446) sits downstream of the re-arm
    sweep, so the expiry sweep schedules the sibling but the eval short-circuits.
    """
    config = _fixed_config(debounce_enabled=True, debounce_seconds=300)
    engine = _engine(hass, config)
    engine._enforce = AsyncMock()
    # Both non-compliant (on, target_state=off).
    hass.states.async_set("light.a", "on")
    hass.states.async_set("light.b", "on")
    # light.b was enforced just before suppression → still cooling down.
    engine._state.cooldowns["light.b"] = dt_util.now() + timedelta(seconds=120)

    engine._state.suppressed_until = dt_util.now() - timedelta(seconds=1)
    engine._state.suppression_reason = "manual"
    engine._handle_suppression_expired(dt_util.now())
    await hass.async_block_till_done()

    enforced = {c.args[0] for c in engine._enforce.await_args_list}
    assert "light.a" in enforced  # not in cooldown → re-enforced
    assert "light.b" not in enforced  # cooldown guard held
    await engine.async_unload()


# --------------------------------------------------------------------------- GAP-2


async def test_expiry_sweep_arms_pending_when_delay_positive(hass: HomeAssistant):
    """delay>0: the clear-path sweep arms a PENDING countdown, not an immediate enforce."""
    config = _fixed_config(delay_seconds=30)
    engine = _engine(hass, config)
    engine._enforce = AsyncMock()
    hass.states.async_set("light.a", "on")
    hass.states.async_set("light.b", "on")

    engine._state.suppressed_until = dt_util.now() - timedelta(seconds=1)
    engine._state.suppression_reason = "manual"
    engine._handle_suppression_expired(dt_util.now())
    await hass.async_block_till_done()

    # No immediate enforce; both targets armed with a pending countdown instead.
    engine._enforce.assert_not_awaited()
    assert engine.current_status() == STATUS_PENDING
    assert set(engine.pending_enforcement_due()) == {"light.a", "light.b"}
    await engine.async_unload()


# --------------------------------------------------------------------------- GAP-4


async def test_dynamic_source_lazy_expiry_enforces_each_target_once(
    hass: HomeAssistant,
):
    """dynamic_state: a SOURCE event that lazily expires suppression re-arms each
    target exactly once.

    The lazy-clear sibling sweep schedules every target, then the source-branch
    sweep schedules them again; _schedule_eval_task cancel-replaces per entity, so
    each target enforces once (no double), and none is dropped.
    """
    config = _dynamic_config(delay_seconds=0)
    engine = _engine(hass, config)
    engine._enforce = AsyncMock()
    # Source ON → desired on; both targets OFF → non-compliant, must be driven ON.
    hass.states.async_set("binary_sensor.motion", "on")
    hass.states.async_set("light.a", "off")
    hass.states.async_set("light.b", "off")

    engine._state.suppressed_until = dt_util.now() - timedelta(seconds=1)
    engine._state.suppression_reason = "manual"

    # A source state event drives async_evaluate on the source entity (not a target).
    await engine.async_evaluate(
        "binary_sensor.motion", hass.states.get("binary_sensor.motion")
    )
    await hass.async_block_till_done()

    assert engine._state.suppressed_until is None
    counts = {}
    for c in engine._enforce.await_args_list:
        counts[c.args[0]] = counts.get(c.args[0], 0) + 1
    assert counts == {"light.a": 1, "light.b": 1}  # each once, neither dropped
    await engine.async_unload()
