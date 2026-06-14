"""Tests for Entity Guard binary sensor platform."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.entity_guard.const import (
    CONF_ENTRY_TYPE,
    DOMAIN,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_RULE,
)
from custom_components.entity_guard.binary_sensor import (
    EntityGuardActiveSensor,
    EntityGuardArmedSensor,
    EntityGuardInCooldownSensor,
)


def _make_engine(armed=False, active=False, in_cooldown=False):
    engine = MagicMock()
    engine.config.unique_id = "test-uid"
    engine.is_armed.return_value = armed
    engine.is_active.return_value = active
    engine.is_in_cooldown.return_value = in_cooldown
    return engine


def _make_rule_entry():
    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_RULE},
        title="Rule",
    )


# ---------------------------------------------------------------------------
# EntityGuardArmedSensor
# ---------------------------------------------------------------------------


def test_armed_sensor_is_on():
    entry = _make_rule_entry()
    engine = _make_engine(armed=True)
    sensor = EntityGuardArmedSensor(entry, engine)
    assert sensor.is_on is True


def test_armed_sensor_is_off():
    entry = _make_rule_entry()
    engine = _make_engine(armed=False)
    sensor = EntityGuardArmedSensor(entry, engine)
    assert sensor.is_on is False


# ---------------------------------------------------------------------------
# EntityGuardActiveSensor
# ---------------------------------------------------------------------------


def test_active_sensor_is_on():
    entry = _make_rule_entry()
    engine = _make_engine(active=True)
    sensor = EntityGuardActiveSensor(entry, engine)
    assert sensor.is_on is True


def test_active_sensor_is_off():
    entry = _make_rule_entry()
    engine = _make_engine(active=False)
    sensor = EntityGuardActiveSensor(entry, engine)
    assert sensor.is_on is False


# ---------------------------------------------------------------------------
# EntityGuardInCooldownSensor
# ---------------------------------------------------------------------------


def test_in_cooldown_sensor_is_on():
    entry = _make_rule_entry()
    engine = _make_engine(in_cooldown=True)
    sensor = EntityGuardInCooldownSensor(entry, engine)
    assert sensor.is_on is True


def test_in_cooldown_sensor_is_off():
    entry = _make_rule_entry()
    engine = _make_engine(in_cooldown=False)
    sensor = EntityGuardInCooldownSensor(entry, engine)
    assert sensor.is_on is False


# ---------------------------------------------------------------------------
# async_setup_entry
# ---------------------------------------------------------------------------


async def test_setup_entry_skips_hub(hass: HomeAssistant):
    from custom_components.entity_guard.binary_sensor import async_setup_entry

    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_ENTRY_TYPE: ENTRY_TYPE_HUB}, title="Hub"
    )
    added = []
    await async_setup_entry(hass, entry, added.extend)
    assert added == []


async def test_setup_entry_adds_binary_sensors(hass: HomeAssistant):
    from custom_components.entity_guard.binary_sensor import async_setup_entry

    entry = _make_rule_entry()
    engine = _make_engine()
    hass.data.setdefault(DOMAIN, {})["engines"] = {entry.entry_id: engine}
    added = []
    await async_setup_entry(hass, entry, added.extend)
    assert len(added) == 4
    types = [type(s).__name__ for s in added]
    assert "EntityGuardArmedSensor" in types
    assert "EntityGuardActiveSensor" in types
    assert "EntityGuardInCooldownSensor" in types
    assert "EntityGuardPendingSensor" in types


# ---------------------------------------------------------------------------
# async_added_to_hass — dispatcher subscription
# ---------------------------------------------------------------------------


async def test_binary_sensor_async_added_subscribes(hass: HomeAssistant):
    from homeassistant.helpers.dispatcher import async_dispatcher_send

    entry = _make_rule_entry()
    engine = _make_engine(armed=False)
    sensor = EntityGuardArmedSensor(entry, engine)
    sensor.hass = hass
    sensor.async_write_ha_state = MagicMock()

    await sensor.async_added_to_hass()

    async_dispatcher_send(hass, f"entity_guard_rule_update_{engine.config.unique_id}")
    await hass.async_block_till_done()
    sensor.async_write_ha_state.assert_called()


def test_pending_sensor_is_on():
    from custom_components.entity_guard.binary_sensor import EntityGuardPendingSensor
    from unittest.mock import MagicMock
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.entity_guard.const import (
        DOMAIN,
        CONF_ENTRY_TYPE,
        ENTRY_TYPE_RULE,
    )

    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_ENTRY_TYPE: ENTRY_TYPE_RULE}, title="R"
    )
    engine = MagicMock()
    engine.config.unique_id = "uid"
    engine.is_pending.return_value = True
    sensor = EntityGuardPendingSensor(entry, engine)
    assert sensor.is_on is True


def test_pending_sensor_is_off():
    from custom_components.entity_guard.binary_sensor import EntityGuardPendingSensor
    from unittest.mock import MagicMock
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.entity_guard.const import (
        DOMAIN,
        CONF_ENTRY_TYPE,
        ENTRY_TYPE_RULE,
    )

    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_ENTRY_TYPE: ENTRY_TYPE_RULE}, title="R"
    )
    engine = MagicMock()
    engine.config.unique_id = "uid"
    engine.is_pending.return_value = False
    sensor = EntityGuardPendingSensor(entry, engine)
    assert sensor.is_on is False
