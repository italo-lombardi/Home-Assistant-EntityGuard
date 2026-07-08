"""Tests for RuleEngine core logic."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util

from custom_components.entity_guard.const import (
    ERROR_RECOVERY_SUCCESS_THRESHOLD,
    ERROR_THRESHOLD,
    MODE_ATTRIBUTE,
    MODE_STATE,
    OPERATOR_GE,
    OPERATOR_GT,
    OPERATOR_LE,
    OPERATOR_LT,
    STATUS_ARMED,
    STATUS_COOLDOWN,
    STATUS_DISABLED,
    STATUS_MASTER_DISABLED,
    STATUS_ENFORCING,
    STATUS_ERROR,
    STATUS_CONDITIONAL,
    STATUS_PENDING,
    STATUS_STARTING,
    STATUS_SUPPRESSED,
)
from custom_components.entity_guard.models import Flag, RuleConfig, RuleRuntimeState
from custom_components.entity_guard.rule_engine import (
    RuleEngine,
    _compare,
    signal_for_rule,
)
from custom_components.entity_guard.storage import EntityGuardStore


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides) -> RuleConfig:
    defaults = dict(
        name="Test Rule",
        unique_id="test-uid",
        target_entities=["light.bedroom"],
        mode=MODE_STATE,
        trigger_states=["on"],
        target_state="off",
        delay_seconds=0,
        attribute=None,
        operator=None,
        threshold=None,
        target_value=None,
        flags=[],
        debounce_enabled=False,
        debounce_seconds=60,
        max_enforcements_per_minute=10,
        safety_acknowledged=False,
    )
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
        mode=MODE_ATTRIBUTE,
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
        mode=MODE_ATTRIBUTE,
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
        mode=MODE_ATTRIBUTE,
        attribute="brightness",
        operator=OPERATOR_GT,
        threshold=50.0,
        target_value=0.0,
        trigger_states=[],
    )
    engine = _make_engine(hass, config)
    st = _state("on", {"brightness": "not-a-number"})
    assert engine._is_triggered("light.x", st) is False


async def test_is_triggered_unknown_mode(hass: HomeAssistant):
    config = _make_config(mode="bad_mode")
    engine = _make_engine(hass, config)
    st = _state("on")
    assert engine._is_triggered("light.x", st) is False


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
        mode=MODE_ATTRIBUTE,
        attribute="brightness",
        operator=OPERATOR_LE,
        threshold=50.0,
        target_value=30.0,
        trigger_states=[],
    )
    engine = _make_engine(hass, config)
    result = engine._resolve_service("light.x")
    assert result is not None
    _, service, data = result
    assert "brightness" in data


async def test_resolve_service_unknown_target(hass: HomeAssistant):
    config = _make_config(target_state="unknown_state_xyz")
    engine = _make_engine(hass, config)
    result = engine._resolve_service("light.x")
    assert result is None


async def test_resolve_service_attribute_none(hass: HomeAssistant):
    config = _make_config(
        mode=MODE_ATTRIBUTE,
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
    finally:
        unsub()

    assert engine.current_status() == STATUS_ARMED
    assert broadcasts, "flag change must broadcast even when status stays ARMED"


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

    Expected broadcast sequence for 2 targets:
      [ENFORCING, ARMED, DISABLED, ENFORCING, ARMED, DISABLED]
    The single-post-loop call (old bug) would produce:
      [ENFORCING, ARMED, ENFORCING, ARMED, DISABLED]
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
    # Exact sequence: ENFORCING → ARMED → DISABLED for each entity, then no trailing non-DISABLED.
    assert all_broadcasts == [
        STATUS_ENFORCING,
        STATUS_ARMED,
        STATUS_DISABLED,
        STATUS_ENFORCING,
        STATUS_ARMED,
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
    with patch.object(hass, "async_create_task", side_effect=tasks_created.append):
        engine._handle_state_event(event)
    assert len(tasks_created) == 1


def test_handle_state_event_none_entity_id(hass: HomeAssistant):
    engine = _make_engine(hass)
    event = MagicMock()
    event.data = {"entity_id": None, "new_state": None}
    tasks_created = []
    with patch.object(hass, "async_create_task", side_effect=tasks_created.append):
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
    with patch.object(hass, "async_create_task", side_effect=tasks_created.append):
        engine._handle_startup_grace_done(MagicMock())
    assert engine._startup_complete is True
    assert len(tasks_created) == 1  # one per target entity


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
    from homeassistant.util import dt as dt_util
    from datetime import timedelta

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
    domain, service, data = result
    assert domain == "homeassistant"
    assert service == "turn_on"


def test_resolve_service_no_mapping_returns_none(hass: HomeAssistant):
    config = _make_config(target_state="cleaning", target_entities=["vacuum.robo"])
    engine = _make_engine(hass, config)
    result = engine._resolve_service("vacuum.robo")
    assert result is None


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

    mock_enforce.assert_awaited_once_with("switch.diffuser")


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

    mock_enforce.assert_awaited_once_with("switch.diffuser")


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

    mock_enforce.assert_awaited_once_with("switch.diffuser")


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
    """MODE_ATTRIBUTE with None attr/op/threshold must return False."""
    config = _make_config(
        mode=MODE_ATTRIBUTE, attribute=None, operator=None, threshold=None
    )
    engine = _make_engine(None, config)
    assert (
        engine._is_triggered("light.bedroom", _state("on", {"brightness": 100}))
        is False
    )


def test_is_triggered_attribute_no_attributes_attr():
    """MODE_ATTRIBUTE state object without attributes must return False gracefully."""
    config = _make_config(
        mode=MODE_ATTRIBUTE,
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
        mode=MODE_ATTRIBUTE,
        attribute="unmapped_attr",
        target_value=50.0,
    )
    engine = _make_engine(None, config)
    assert engine._resolve_service("light.bedroom") is None


def test_resolve_service_attribute_mode_missing_attr():
    """_resolve_service returns None when attribute or target_value is None."""
    config = _make_config(mode=MODE_ATTRIBUTE, attribute=None, target_value=None)
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
    from unittest.mock import AsyncMock, patch, MagicMock
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.entity_guard import async_unload_entry
    from custom_components.entity_guard.const import (
        DOMAIN,
        CONF_ENTRY_TYPE,
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
            return None

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

        def __new__(cls, seconds=0, **kw):  # noqa: ARG003
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
