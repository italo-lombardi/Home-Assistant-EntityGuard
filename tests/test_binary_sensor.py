"""Tests for Entity Guard binary sensor platform."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.entity_guard.binary_sensor import (
    EntityGuardActiveSensor,
    EntityGuardArmedSensor,
    EntityGuardInCooldownSensor,
)
from custom_components.entity_guard.const import (
    CONF_ENTRY_TYPE,
    DOMAIN,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_RULE,
)


def _make_engine(
    armed=False,
    active=False,
    in_cooldown=False,
    verify_enabled=False,
    recently_enforced_verified=False,
):
    engine = MagicMock()
    engine.config.unique_id = "test-uid"
    engine.config.verify_enabled = verify_enabled
    engine.is_armed.return_value = armed
    engine.is_active.return_value = active
    engine.is_in_cooldown.return_value = in_cooldown
    engine.is_recently_enforced_verified.return_value = recently_enforced_verified
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
    assert len(added) == 5
    types = [type(s).__name__ for s in added]
    assert "EntityGuardArmedSensor" in types
    assert "EntityGuardActiveSensor" in types
    assert "EntityGuardInCooldownSensor" in types
    assert "EntityGuardPendingSensor" in types
    assert "EntityGuardRecentlyEnforcedSensor" in types


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
    from unittest.mock import MagicMock

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.entity_guard.binary_sensor import EntityGuardPendingSensor
    from custom_components.entity_guard.const import (
        CONF_ENTRY_TYPE,
        DOMAIN,
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
    from unittest.mock import MagicMock

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.entity_guard.binary_sensor import EntityGuardPendingSensor
    from custom_components.entity_guard.const import (
        CONF_ENTRY_TYPE,
        DOMAIN,
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


def test_recently_enforced_sensor_is_on():
    from unittest.mock import MagicMock

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.entity_guard.binary_sensor import (
        EntityGuardRecentlyEnforcedSensor,
    )
    from custom_components.entity_guard.const import (
        CONF_ENTRY_TYPE,
        DOMAIN,
        ENTRY_TYPE_RULE,
    )

    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_ENTRY_TYPE: ENTRY_TYPE_RULE}, title="R"
    )
    engine = MagicMock()
    engine.config.unique_id = "uid"
    engine.config.name = "My Rule"
    engine.is_recently_enforced.return_value = True
    sensor = EntityGuardRecentlyEnforcedSensor(entry, engine)
    assert sensor.is_on is True


def test_recently_enforced_sensor_is_off():
    from unittest.mock import MagicMock

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.entity_guard.binary_sensor import (
        EntityGuardRecentlyEnforcedSensor,
    )
    from custom_components.entity_guard.const import (
        CONF_ENTRY_TYPE,
        DOMAIN,
        ENTRY_TYPE_RULE,
    )

    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_ENTRY_TYPE: ENTRY_TYPE_RULE}, title="R"
    )
    engine = MagicMock()
    engine.config.unique_id = "uid"
    engine.config.name = "My Rule"
    engine.is_recently_enforced.return_value = False
    sensor = EntityGuardRecentlyEnforcedSensor(entry, engine)
    assert sensor.is_on is False


def test_recently_enforced_sensor_extra_state_attributes():
    from unittest.mock import MagicMock

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.entity_guard.binary_sensor import (
        EntityGuardRecentlyEnforcedSensor,
    )
    from custom_components.entity_guard.const import (
        CONF_ENTRY_TYPE,
        DOMAIN,
        ENTRY_TYPE_RULE,
        MODE_FIXED_STATE,
    )

    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_ENTRY_TYPE: ENTRY_TYPE_RULE}, title="Balcony Rule"
    )
    engine = MagicMock()
    engine.config.unique_id = "uid"
    engine.config.mode = MODE_FIXED_STATE
    engine.config.target_entities = ["light.balcony"]
    engine.config.target_state = "off"
    engine.config.target_value = None
    engine.config.delay_seconds = 10
    engine.is_recently_enforced.return_value = True
    sensor = EntityGuardRecentlyEnforcedSensor(entry, engine)
    # hass=None → target_entity_names falls back to entity IDs
    sensor.hass = None
    attrs = sensor.extra_state_attributes
    assert attrs["rule_name"] == "Balcony Rule"
    assert attrs["target_entities"] == ["light.balcony"]
    assert attrs["target_entity_names"] == ["light.balcony"]
    assert attrs["target"] == "off"
    assert attrs["delay_seconds"] == 10


async def test_recently_enforced_sensor_target_entity_names_with_hass(
    hass: HomeAssistant,
):
    from unittest.mock import MagicMock

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.entity_guard.binary_sensor import (
        EntityGuardRecentlyEnforcedSensor,
    )
    from custom_components.entity_guard.const import (
        CONF_ENTRY_TYPE,
        DOMAIN,
        ENTRY_TYPE_RULE,
        MODE_FIXED_STATE,
    )

    # Entity with friendly_name in state attributes
    hass.states.async_set("light.balcony", "on", {"friendly_name": "Balcony Light"})
    # Entity with no friendly_name — falls back to entity_id
    hass.states.async_set("light.kitchen", "off", {})

    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_ENTRY_TYPE: ENTRY_TYPE_RULE}, title="R"
    )
    engine = MagicMock()
    engine.config.unique_id = "uid"
    engine.config.mode = MODE_FIXED_STATE
    engine.config.target_entities = ["light.balcony", "light.kitchen", "light.missing"]
    engine.config.target_state = "off"
    engine.config.target_value = None
    engine.config.delay_seconds = 0
    engine.is_recently_enforced.return_value = True
    sensor = EntityGuardRecentlyEnforcedSensor(entry, engine)
    sensor.hass = hass
    attrs = sensor.extra_state_attributes
    assert attrs["target_entity_names"][0] == "Balcony Light"
    assert (
        attrs["target_entity_names"][1] == "light.kitchen"
    )  # no friendly_name → entity_id
    # missing entity → entity_id
    assert attrs["target_entity_names"][2] == "light.missing"


async def test_recently_enforced_subscribes_to_target_entity_changes(
    hass: HomeAssistant,
):
    """Re-writes state when a target entity registry entry changes (for friendly name refresh)."""
    from unittest.mock import MagicMock

    from homeassistant.helpers.entity_registry import async_get as async_get_er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.entity_guard.binary_sensor import (
        EntityGuardRecentlyEnforcedSensor,
    )
    from custom_components.entity_guard.const import (
        CONF_ENTRY_TYPE,
        DOMAIN,
        ENTRY_TYPE_RULE,
        MODE_FIXED_STATE,
    )

    er = async_get_er(hass)
    er.async_get_or_create("light", "test", "balcony", suggested_object_id="balcony")

    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_ENTRY_TYPE: ENTRY_TYPE_RULE}, title="R"
    )
    engine = MagicMock()
    engine.config.unique_id = "uid"
    engine.config.mode = MODE_FIXED_STATE
    engine.config.target_entities = ["light.balcony"]
    engine.config.target_state = "off"
    engine.config.target_value = None
    engine.config.delay_seconds = 0
    engine.is_recently_enforced.return_value = False

    sensor = EntityGuardRecentlyEnforcedSensor(entry, engine)
    sensor.hass = hass
    sensor.async_write_ha_state = MagicMock()

    await sensor.async_added_to_hass()

    # Simulate entity registry update (e.g. rename) — sensor should re-write
    er.async_update_entity("light.balcony", name="Balcony Light")
    await hass.async_block_till_done()
    sensor.async_write_ha_state.assert_called()


async def test_recently_enforced_no_target_entities_no_subscription(
    hass: HomeAssistant,
):
    """No state change subscription when target_entities is empty."""
    from unittest.mock import MagicMock

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.entity_guard.binary_sensor import (
        EntityGuardRecentlyEnforcedSensor,
    )
    from custom_components.entity_guard.const import (
        CONF_ENTRY_TYPE,
        DOMAIN,
        ENTRY_TYPE_RULE,
        MODE_FIXED_STATE,
    )

    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_ENTRY_TYPE: ENTRY_TYPE_RULE}, title="R"
    )
    engine = MagicMock()
    engine.config.unique_id = "uid"
    engine.config.mode = MODE_FIXED_STATE
    engine.config.target_entities = []
    engine.config.target_state = "off"
    engine.config.target_value = None
    engine.config.delay_seconds = 0
    engine.is_recently_enforced.return_value = False

    sensor = EntityGuardRecentlyEnforcedSensor(entry, engine)
    sensor.hass = hass
    sensor.async_write_ha_state = MagicMock()
    await sensor.async_added_to_hass()  # must not raise
    sensor.async_write_ha_state.assert_called()


# ---------------------------------------------------------------------------
# EntityGuardRecentlyEnforcedVerifiedSensor + verify-on lifecycle
# ---------------------------------------------------------------------------


def test_recently_enforced_verified_sensor_is_on():
    from custom_components.entity_guard.binary_sensor import (
        EntityGuardRecentlyEnforcedVerifiedSensor,
    )

    entry = _make_rule_entry()
    engine = _make_engine(recently_enforced_verified=True)
    sensor = EntityGuardRecentlyEnforcedVerifiedSensor(entry, engine)
    assert sensor.is_on is True


def test_recently_enforced_verified_sensor_is_off():
    from custom_components.entity_guard.binary_sensor import (
        EntityGuardRecentlyEnforcedVerifiedSensor,
    )

    entry = _make_rule_entry()
    engine = _make_engine(recently_enforced_verified=False)
    sensor = EntityGuardRecentlyEnforcedVerifiedSensor(entry, engine)
    assert sensor.is_on is False


async def test_setup_entry_adds_verified_sensor_when_verify_on(hass: HomeAssistant):
    from custom_components.entity_guard.binary_sensor import async_setup_entry

    entry = _make_rule_entry()
    engine = _make_engine(verify_enabled=True)
    hass.data.setdefault(DOMAIN, {})["engines"] = {entry.entry_id: engine}
    added = []
    await async_setup_entry(hass, entry, added.extend)
    assert len(added) == 6
    assert "EntityGuardRecentlyEnforcedVerifiedSensor" in [
        type(s).__name__ for s in added
    ]


async def test_setup_entry_omits_verified_sensor_when_verify_off(hass: HomeAssistant):
    from custom_components.entity_guard.binary_sensor import async_setup_entry

    entry = _make_rule_entry()
    engine = _make_engine(verify_enabled=False)
    hass.data.setdefault(DOMAIN, {})["engines"] = {entry.entry_id: engine}
    added = []
    await async_setup_entry(hass, entry, added.extend)
    assert "EntityGuardRecentlyEnforcedVerifiedSensor" not in [
        type(s).__name__ for s in added
    ]


async def test_setup_entry_disables_verified_row_on_verify_off(hass: HomeAssistant):
    """Verify ON->OFF: an existing registry row is disabled (not removed), reversibly."""
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers.entity_registry import async_get as async_get_er

    from custom_components.entity_guard.binary_sensor import async_setup_entry

    entry = _make_rule_entry()
    entry.add_to_hass(hass)
    reg = async_get_er(hass)
    verified_uid = f"{entry.entry_id}_recently_enforced_verified"
    reg.async_get_or_create("binary_sensor", DOMAIN, verified_uid, config_entry=entry)

    engine = _make_engine(verify_enabled=False)
    hass.data.setdefault(DOMAIN, {})["engines"] = {entry.entry_id: engine}
    added = []
    await async_setup_entry(hass, entry, added.extend)

    eid = reg.async_get_entity_id("binary_sensor", DOMAIN, verified_uid)
    assert reg.async_get(eid).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert "EntityGuardRecentlyEnforcedVerifiedSensor" not in [
        type(s).__name__ for s in added
    ]


async def test_setup_entry_reenables_verified_row_on_verify_on(hass: HomeAssistant):
    """Verify OFF->ON: a row disabled by a prior verify-off is re-enabled, then added live."""
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers.entity_registry import async_get as async_get_er

    from custom_components.entity_guard.binary_sensor import async_setup_entry

    entry = _make_rule_entry()
    entry.add_to_hass(hass)
    reg = async_get_er(hass)
    verified_uid = f"{entry.entry_id}_recently_enforced_verified"
    reg.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        verified_uid,
        config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )

    engine = _make_engine(verify_enabled=True)
    hass.data.setdefault(DOMAIN, {})["engines"] = {entry.entry_id: engine}
    added = []
    await async_setup_entry(hass, entry, added.extend)

    eid = reg.async_get_entity_id("binary_sensor", DOMAIN, verified_uid)
    assert reg.async_get(eid).disabled_by is None
    assert "EntityGuardRecentlyEnforcedVerifiedSensor" in [
        type(s).__name__ for s in added
    ]
