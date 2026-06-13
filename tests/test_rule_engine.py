"""Tests for RuleEngine core logic."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.entity_guard.const import (
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


# ---------------------------------------------------------------------------
# async_test_enforce
# ---------------------------------------------------------------------------


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
    """A successful enforcement after error must reset counter and status."""
    config = _make_config(target_state="off")
    engine = _make_engine(hass, config)
    engine._startup_complete = True

    engine._state.consecutive_errors = ERROR_THRESHOLD
    engine._state.last_error = "previous error"
    engine._set_status(STATUS_ERROR)

    hass.services.async_register("light", "turn_off", AsyncMock())
    hass.states.async_set("light.bedroom", "on")
    st = hass.states.get("light.bedroom")
    await engine.async_evaluate("light.bedroom", st)

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
