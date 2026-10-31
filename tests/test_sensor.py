"""Tests for Entity Guard sensor platform."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.entity_guard.const import (
    CONF_ENTRY_TYPE,
    DOMAIN,
    ENTRY_TYPE_RULE,
    STATUS_ARMED,
    entry_has_safety_target,
    has_safety_target,
    signal_master,
    signal_rule_update,
)
from custom_components.entity_guard.sensor import (
    EntityGuardCooldownRemainingSensor,
    EntityGuardEnforcementCountTodaySensor,
    EntityGuardEnforcementCountTotalSensor,
    EntityGuardLastEnforcedSensor,
    EntityGuardSafetyStatusSensor,
    EntityGuardSkipCountTodaySensor,
    EntityGuardSkipCountTotalSensor,
    EntityGuardStatusSensor,
    EntityGuardSuppressedUntilSensor,
)


def _make_rule_entry(**overrides):
    data = {
        CONF_ENTRY_TYPE: ENTRY_TYPE_RULE,
        "target_entities": ["light.bedroom"],
    }
    data.update(overrides)
    return MockConfigEntry(domain=DOMAIN, data=data, title="Test Rule")


def _make_engine(
    status=STATUS_ARMED, count_today=2, count_total=10, skip_today=0, skip_total=0
):
    engine = MagicMock()
    engine.config.unique_id = "test-uid"
    engine.config.target_state = "off"
    engine.config.target_entities = ["light.bedroom"]
    engine.config.trigger_states = ["on"]
    engine.config.safety_acknowledged = False
    engine.config.flags = []
    engine.config.delay_seconds = 5
    engine.config.debounce_enabled = True
    engine.config.debounce_seconds = 30
    engine.config.max_enforcements_per_minute = 10
    engine.state.consecutive_errors = 0
    engine.state.last_error = None
    engine.current_status.return_value = status
    engine.state.enforcement_count_today = count_today
    engine.state.enforcement_count_total = count_total
    engine.state.skip_count_today = skip_today
    engine.state.skip_count_total = skip_total
    engine.state.last_enforced = None
    engine.state.suppressed_until = None
    engine.state.suppression_reason = None
    engine.state.counter_total_since = None
    engine.cooldown_remaining_seconds.return_value = 0.0
    engine.cooldown_ends.return_value = {}
    return engine


# ---------------------------------------------------------------------------
# signal helpers
# ---------------------------------------------------------------------------


def test_signal_for_rule():
    sig = signal_rule_update("my-uid")
    assert "my-uid" in sig


def test_signal_master():
    assert isinstance(signal_master(), str)


# ---------------------------------------------------------------------------
# _has_safety_target
# ---------------------------------------------------------------------------


def test_has_safety_target_true():
    entry = _make_rule_entry(target_entities=["lock.front_door"])
    assert has_safety_target(entry.data.get("target_entities", [])) is True


def test_has_safety_target_false():
    entry = _make_rule_entry(target_entities=["light.bedroom"])
    assert has_safety_target(entry.data.get("target_entities", [])) is False


def test_has_safety_target_empty():
    entry = _make_rule_entry(target_entities=[])
    assert has_safety_target(entry.data.get("target_entities", [])) is False


# ---------------------------------------------------------------------------
# entry_has_safety_target
# ---------------------------------------------------------------------------


def test_entry_has_safety_target_from_data():
    entry = _make_rule_entry(target_entities=["lock.front_door"])
    assert entry_has_safety_target(entry) is True


def test_entry_has_safety_target_false():
    entry = _make_rule_entry(target_entities=["light.bedroom"])
    assert entry_has_safety_target(entry) is False


def test_entry_has_safety_target_from_options():
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_RULE},
        options={"target_entities": ["cover.garage"]},
        title="Test",
    )
    assert entry_has_safety_target(entry) is True


def test_entry_has_safety_target_empty():
    entry = _make_rule_entry(target_entities=[])
    assert entry_has_safety_target(entry) is False


# ---------------------------------------------------------------------------
# Sensor values
# ---------------------------------------------------------------------------


def test_status_sensor_native_value():
    entry = _make_rule_entry()
    engine = _make_engine(status="conditional")
    sensor = EntityGuardStatusSensor(entry, engine)
    assert sensor.native_value == "conditional"


def test_status_sensor_extra_attrs():
    entry = _make_rule_entry()
    engine = _make_engine()
    engine.state.suppression_reason = "manual"
    sensor = EntityGuardStatusSensor(entry, engine)
    attrs = sensor.extra_state_attributes
    assert attrs["suppression_reason"] == "manual"
    assert "light.bedroom" in attrs["target_entities"]
    assert attrs["target_state"] == "off"
    assert attrs["trigger_states"] == ["on"]
    assert attrs["flags"] == []
    assert attrs["delay_seconds"] == 5
    assert attrs["debounce_enabled"] is True
    assert attrs["debounce_seconds"] == 30
    assert attrs["rate_limit_enabled"] is True
    assert attrs["max_enforcements_per_minute"] == 10


def test_status_sensor_config_attrs_zero_rate_limit():
    entry = _make_rule_entry()
    engine = _make_engine()
    engine.config.delay_seconds = 0
    engine.config.debounce_enabled = False
    engine.config.debounce_seconds = 0
    engine.config.max_enforcements_per_minute = 0
    sensor = EntityGuardStatusSensor(entry, engine)
    attrs = sensor.extra_state_attributes
    assert attrs["delay_seconds"] == 0
    assert attrs["debounce_enabled"] is False
    assert attrs["debounce_seconds"] == 0
    assert attrs["rate_limit_enabled"] is False
    assert attrs["max_enforcements_per_minute"] == 0


def test_status_sensor_flags_attribute(hass: HomeAssistant):
    flag = MagicMock()
    flag.entity = "input_boolean.guest_mode"
    flag.match_state = "on"
    entry = _make_rule_entry()
    engine = _make_engine()
    engine.config.flags = [flag]
    hass.states.async_set("input_boolean.guest_mode", "off")
    sensor = EntityGuardStatusSensor(entry, engine)
    sensor.hass = hass
    flags = sensor.extra_state_attributes["flags"]
    assert len(flags) == 1
    assert flags[0]["entity"] == "input_boolean.guest_mode"
    assert flags[0]["required"] == "on"
    assert flags[0]["current"] == "off"
    assert flags[0]["matches"] is False


def test_last_enforced_sensor_none():
    entry = _make_rule_entry()
    engine = _make_engine()
    engine.state.last_enforced = None
    sensor = EntityGuardLastEnforcedSensor(entry, engine)
    assert sensor.native_value is None


def test_last_enforced_sensor_value():
    from homeassistant.util import dt as dt_util

    entry = _make_rule_entry()
    engine = _make_engine()
    ts = datetime(2025, 6, 1, 12, 0, 0, tzinfo=dt_util.UTC)
    engine.state.last_enforced = ts
    sensor = EntityGuardLastEnforcedSensor(entry, engine)
    assert sensor.native_value == ts


def test_count_today_sensor():
    entry = _make_rule_entry()
    engine = _make_engine(count_today=5)
    sensor = EntityGuardEnforcementCountTodaySensor(entry, engine)
    assert sensor.native_value == 5


def test_count_total_sensor():
    entry = _make_rule_entry()
    engine = _make_engine(count_total=42)
    sensor = EntityGuardEnforcementCountTotalSensor(entry, engine)
    assert sensor.native_value == 42


def test_count_total_sensor_attrs_when_since_none():
    entry = _make_rule_entry()
    engine = _make_engine()
    engine.state.counter_total_since = None
    sensor = EntityGuardEnforcementCountTotalSensor(entry, engine)
    attrs = sensor.extra_state_attributes
    assert attrs == {"counter_since": None, "counter_days": None}


def test_count_total_sensor_attrs_when_since_set():
    from homeassistant.util import dt as dt_util

    entry = _make_rule_entry()
    engine = _make_engine()
    since = dt_util.now() - timedelta(days=3, hours=2)
    engine.state.counter_total_since = since
    sensor = EntityGuardEnforcementCountTotalSensor(entry, engine)
    attrs = sensor.extra_state_attributes
    assert attrs["counter_since"] == since.isoformat()
    assert attrs["counter_days"] == 3


def test_count_total_sensor_attrs_naive_since_coerced_utc():
    """A tz-naive counter_total_since must be coerced (no TypeError)."""

    entry = _make_rule_entry()
    engine = _make_engine()
    from homeassistant.util import dt as dt_util

    # Build a naive datetime whose UTC-coerced value is exactly 5 days before
    # dt_util.now(). Guarantees counter_days == 5 regardless of local tz.
    naive = (dt_util.utcnow() - timedelta(days=5, hours=1)).replace(tzinfo=None)
    engine.state.counter_total_since = naive
    sensor = EntityGuardEnforcementCountTotalSensor(entry, engine)
    attrs = sensor.extra_state_attributes
    assert attrs["counter_since"].endswith("+00:00")
    assert attrs["counter_days"] == 5


def test_cooldown_remaining_sensor():
    entry = _make_rule_entry()
    engine = _make_engine()
    engine.cooldown_remaining_seconds.return_value = 27.5
    sensor = EntityGuardCooldownRemainingSensor(entry, engine)
    assert sensor.native_value == 27


def test_cooldown_remaining_sensor_zero():
    entry = _make_rule_entry()
    engine = _make_engine()
    engine.cooldown_remaining_seconds.return_value = 0.0
    sensor = EntityGuardCooldownRemainingSensor(entry, engine)
    assert sensor.native_value == 0


def test_safety_sensor_acknowledged():
    entry = _make_rule_entry()
    engine = _make_engine()
    engine.config.safety_acknowledged = True
    sensor = EntityGuardSafetyStatusSensor(entry, engine)
    assert sensor.native_value == "acknowledged"


def test_safety_sensor_not_acknowledged():
    entry = _make_rule_entry()
    engine = _make_engine()
    engine.config.safety_acknowledged = False
    sensor = EntityGuardSafetyStatusSensor(entry, engine)
    assert sensor.native_value == "not_acknowledged"


def test_suppressed_until_sensor_none():
    entry = _make_rule_entry()
    engine = _make_engine()
    engine.state.suppressed_until = None
    sensor = EntityGuardSuppressedUntilSensor(entry, engine)
    assert sensor.native_value is None


def test_suppressed_until_sensor_attrs():
    entry = _make_rule_entry()
    engine = _make_engine()
    engine.state.suppression_reason = "loop_protection"
    sensor = EntityGuardSuppressedUntilSensor(entry, engine)
    assert sensor.extra_state_attributes["reason"] == "loop_protection"


# ---------------------------------------------------------------------------
# async_setup_entry skips hub entries
# ---------------------------------------------------------------------------


async def test_setup_entry_skips_non_rule(hass: HomeAssistant):
    from custom_components.entity_guard.const import ENTRY_TYPE_HUB
    from custom_components.entity_guard.sensor import async_setup_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_HUB},
        title="Hub",
    )
    add_entities = MagicMock()
    await async_setup_entry(hass, entry, add_entities)
    add_entities.assert_not_called()


async def test_setup_entry_adds_sensors(hass: HomeAssistant):
    from custom_components.entity_guard.sensor import async_setup_entry

    entry = _make_rule_entry()
    engine = _make_engine()
    hass.data.setdefault(DOMAIN, {})["engines"] = {entry.entry_id: engine}

    added = []
    await async_setup_entry(hass, entry, added.extend)
    assert len(added) >= 6


async def test_setup_entry_adds_safety_sensor_for_lock(hass: HomeAssistant):
    from custom_components.entity_guard.sensor import async_setup_entry

    entry = _make_rule_entry(target_entities=["lock.front"])
    engine = _make_engine()
    hass.data.setdefault(DOMAIN, {})["engines"] = {entry.entry_id: engine}

    added = []
    await async_setup_entry(hass, entry, added.extend)
    types = [type(s).__name__ for s in added]
    assert "EntityGuardSafetyStatusSensor" in types


# ---------------------------------------------------------------------------
# async_added_to_hass — dispatcher subscription
# ---------------------------------------------------------------------------


async def test_sensor_async_added_subscribes(hass: HomeAssistant):
    from homeassistant.helpers.dispatcher import async_dispatcher_send

    entry = _make_rule_entry()
    engine = _make_engine()
    sensor = EntityGuardStatusSensor(entry, engine)
    sensor.hass = hass
    sensor.async_write_ha_state = MagicMock()

    await sensor.async_added_to_hass()

    # Firing the rule signal should call async_write_ha_state
    async_dispatcher_send(hass, f"entity_guard_rule_update_{engine.config.unique_id}")
    await hass.async_block_till_done()
    sensor.async_write_ha_state.assert_called()


def test_rule_id_sensor_native_value():
    from unittest.mock import MagicMock

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.entity_guard.const import (
        CONF_ENTRY_TYPE,
        DOMAIN,
        ENTRY_TYPE_RULE,
    )
    from custom_components.entity_guard.sensor import EntityGuardRuleIdSensor

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_RULE},
        title="R",
        entry_id="abc123",
    )
    engine = MagicMock()
    engine.config.unique_id = "uid"
    sensor = EntityGuardRuleIdSensor(entry, engine)
    # Returns the stable rule ID (unique_id), NOT the config entry_id — this is
    # the value carried as rule_id on events / list_rules, so the sensor agrees.
    assert sensor.native_value == "uid"


# ---------------------------------------------------------------------------
# last_triggered_* sensor attributes
# ---------------------------------------------------------------------------


def test_status_sensor_last_triggered_none_before_enforcement():
    """Both last_triggered_* attrs are None before any enforcement fires."""
    entry = _make_rule_entry()
    engine = _make_engine()
    engine.state.last_triggered_user_id = None
    engine.state.last_triggered_entity = None
    sensor = EntityGuardStatusSensor(entry, engine)
    attrs = sensor.extra_state_attributes
    assert attrs["last_triggered_by"] is None
    assert attrs["last_triggered_entity"] is None


def test_status_sensor_last_triggered_by_user_id():
    """last_triggered_by is the raw user_id when a UI user caused the trigger."""
    entry = _make_rule_entry()
    engine = _make_engine()
    engine.state.last_triggered_user_id = "abc123-user-uuid"
    engine.state.last_triggered_entity = "light.bedroom"
    sensor = EntityGuardStatusSensor(entry, engine)
    attrs = sensor.extra_state_attributes
    assert attrs["last_triggered_by"] == "abc123-user-uuid"
    assert attrs["last_triggered_entity"] == "light.bedroom"


def test_status_sensor_last_triggered_by_none_for_automation():
    """last_triggered_by is None (not a string) when automation triggered it."""
    entry = _make_rule_entry()
    engine = _make_engine()
    engine.state.last_triggered_user_id = None
    engine.state.last_triggered_entity = "switch.fan"
    sensor = EntityGuardStatusSensor(entry, engine)
    attrs = sensor.extra_state_attributes
    assert attrs["last_triggered_by"] is None
    assert attrs["last_triggered_entity"] == "switch.fan"


def test_status_sensor_target_outcomes_attr_serialized():
    """target_outcomes is exposed with `at` ISO-serialized, and both volatile
    attrs are excluded from the recorder."""
    entry = _make_rule_entry()
    engine = _make_engine()
    at = datetime(2026, 10, 27, 12, 0, 0)
    engine.state.target_outcomes = {
        "light.bedroom": {
            "outcome": "failed",
            "reason": "service_call_failed",
            "error": "device offline",
            "at": at,
        }
    }
    sensor = EntityGuardStatusSensor(entry, engine)
    attrs = sensor.extra_state_attributes
    out = attrs["target_outcomes"]["light.bedroom"]
    assert out["outcome"] == "failed"
    assert out["reason"] == "service_call_failed"
    assert out["error"] == "device offline"
    assert out["at"] == at.isoformat()
    # Volatile-per-enforce attrs stay out of state history.
    assert "target_outcomes" in EntityGuardStatusSensor._unrecorded_attributes
    assert "pending_enforcement_due" in EntityGuardStatusSensor._unrecorded_attributes
    assert "cooldown_ends" in EntityGuardStatusSensor._unrecorded_attributes


def test_status_sensor_target_outcomes_empty_by_default():
    """No outcomes → attribute is an empty dict, not an error."""
    entry = _make_rule_entry()
    engine = _make_engine()
    engine.state.target_outcomes = {}
    sensor = EntityGuardStatusSensor(entry, engine)
    assert sensor.extra_state_attributes["target_outcomes"] == {}


def test_status_sensor_cooldown_ends_serialized_iso():
    """cooldown_ends is exposed as {eid: isoformat} (mirrors pending_enforcement_due)."""
    from homeassistant.util import dt as dt_util

    entry = _make_rule_entry()
    engine = _make_engine()
    end = dt_util.now() + timedelta(seconds=42)
    engine.cooldown_ends.return_value = {"light.bedroom": end}
    sensor = EntityGuardStatusSensor(entry, engine)
    attrs = sensor.extra_state_attributes
    assert attrs["cooldown_ends"] == {"light.bedroom": end.isoformat()}


def test_status_sensor_cooldown_ends_empty_by_default():
    """No active cooldowns → empty dict, not an error."""
    entry = _make_rule_entry()
    engine = _make_engine()
    engine.cooldown_ends.return_value = {}
    sensor = EntityGuardStatusSensor(entry, engine)
    assert sensor.extra_state_attributes["cooldown_ends"] == {}


def test_skip_count_today_sensor():
    entry = _make_rule_entry()
    engine = _make_engine(skip_today=6)
    sensor = EntityGuardSkipCountTodaySensor(entry, engine)
    assert sensor.native_value == 6


def test_skip_count_total_sensor():
    entry = _make_rule_entry()
    engine = _make_engine(skip_total=37)
    sensor = EntityGuardSkipCountTotalSensor(entry, engine)
    assert sensor.native_value == 37
