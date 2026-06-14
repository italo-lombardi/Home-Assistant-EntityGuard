"""Tests for Entity Guard switch platform."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.entity_guard.const import (
    CONF_ENTRY_TYPE,
    DOMAIN,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_RULE,
    signal_master,
    signal_rule_update,
)
from custom_components.entity_guard.switch import (
    EntityGuardDebounceEnabledSwitch,
    EntityGuardEnabledSwitch,
    EntityGuardMasterEnabledSwitch,
)


def _make_engine(enabled=True, debounce_enabled=False, unique_id="test-uid"):
    engine = MagicMock()
    engine.config.unique_id = unique_id
    engine.config.debounce_enabled = debounce_enabled
    engine.state.enabled = enabled
    engine.set_enabled = MagicMock()
    return engine


def _make_rule_entry(**overrides):
    data = {CONF_ENTRY_TYPE: ENTRY_TYPE_RULE}
    data.update(overrides)
    return MockConfigEntry(domain=DOMAIN, data=data, title="Rule")


def _make_hub_entry(**overrides):
    data = {CONF_ENTRY_TYPE: ENTRY_TYPE_HUB}
    data.update(overrides)
    return MockConfigEntry(
        domain=DOMAIN, data=data, title="Hub", options=overrides.get("options", {})
    )


# ---------------------------------------------------------------------------
# signal helpers
# ---------------------------------------------------------------------------


def test_signal_for_rule():
    assert "my-id" in signal_rule_update("my-id")


def test_signal_master():
    assert isinstance(signal_master(), str)


# ---------------------------------------------------------------------------
# EntityGuardEnabledSwitch
# ---------------------------------------------------------------------------


def test_enabled_switch_is_on():
    entry = _make_rule_entry()
    engine = _make_engine(enabled=True)
    sw = EntityGuardEnabledSwitch(entry, engine)
    assert sw.is_on is True


def test_enabled_switch_is_off():
    entry = _make_rule_entry()
    engine = _make_engine(enabled=False)
    sw = EntityGuardEnabledSwitch(entry, engine)
    assert sw.is_on is False


async def test_enabled_switch_turn_on(hass: HomeAssistant):
    entry = _make_rule_entry()
    engine = _make_engine(enabled=False)
    entry.add_to_hass(hass)
    sw = EntityGuardEnabledSwitch(entry, engine)
    sw.hass = hass
    sw.async_write_ha_state = MagicMock()
    await sw.async_turn_on()
    engine.set_enabled.assert_called_once_with(True)


async def test_enabled_switch_turn_off(hass: HomeAssistant):
    entry = _make_rule_entry()
    engine = _make_engine(enabled=True)
    entry.add_to_hass(hass)
    sw = EntityGuardEnabledSwitch(entry, engine)
    sw.hass = hass
    sw.async_write_ha_state = MagicMock()
    await sw.async_turn_off()
    engine.set_enabled.assert_called_once_with(False)


# ---------------------------------------------------------------------------
# EntityGuardDebounceEnabledSwitch
# ---------------------------------------------------------------------------


def test_debounce_switch_is_on():
    entry = _make_rule_entry()
    engine = _make_engine(debounce_enabled=True)
    sw = EntityGuardDebounceEnabledSwitch(entry, engine)
    assert sw.is_on is True


def test_debounce_switch_is_off():
    entry = _make_rule_entry()
    engine = _make_engine(debounce_enabled=False)
    sw = EntityGuardDebounceEnabledSwitch(entry, engine)
    assert sw.is_on is False


async def test_debounce_switch_turn_on(hass: HomeAssistant):
    entry = _make_rule_entry()
    engine = _make_engine(debounce_enabled=False)
    entry.add_to_hass(hass)
    sw = EntityGuardDebounceEnabledSwitch(entry, engine)
    sw.hass = hass
    sw.async_write_ha_state = MagicMock()
    await sw.async_turn_on()
    assert entry.options.get("debounce_enabled") is True


async def test_debounce_switch_turn_off(hass: HomeAssistant):
    entry = _make_rule_entry()
    engine = _make_engine(debounce_enabled=True)
    entry.add_to_hass(hass)
    sw = EntityGuardDebounceEnabledSwitch(entry, engine)
    sw.hass = hass
    sw.async_write_ha_state = MagicMock()
    await sw.async_turn_off()
    assert entry.options.get("debounce_enabled") is False


# ---------------------------------------------------------------------------
# EntityGuardMasterEnabledSwitch
# ---------------------------------------------------------------------------


def test_master_switch_is_on(hass: HomeAssistant):
    entry = _make_hub_entry()
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})["hub_master_enabled"] = True
    sw = EntityGuardMasterEnabledSwitch(hass, entry)
    sw.hass = hass
    assert sw.is_on is True


def test_master_switch_is_off(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_HUB},
        options={"master_enabled": False},
        title="Hub",
    )
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})["hub_master_enabled"] = False
    sw = EntityGuardMasterEnabledSwitch(hass, entry)
    sw.hass = hass
    assert sw.is_on is False


async def test_master_switch_turn_off(hass: HomeAssistant):
    entry = _make_hub_entry()
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})["hub_master_enabled"] = True
    sw = EntityGuardMasterEnabledSwitch(hass, entry)
    sw.hass = hass
    sw.async_write_ha_state = MagicMock()
    await sw.async_turn_off()
    assert hass.data[DOMAIN]["hub_master_enabled"] is False


async def test_master_switch_turn_on(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_HUB},
        options={"master_enabled": False},
        title="Hub",
    )
    entry.add_to_hass(hass)
    sw = EntityGuardMasterEnabledSwitch(hass, entry)
    sw.hass = hass
    sw.async_write_ha_state = MagicMock()
    await sw.async_turn_on()
    assert hass.data[DOMAIN]["hub_master_enabled"] is True


# ---------------------------------------------------------------------------
# async_setup_entry routing
# ---------------------------------------------------------------------------


async def test_setup_entry_hub(hass: HomeAssistant):
    from custom_components.entity_guard.switch import async_setup_entry

    entry = _make_hub_entry()
    entry.add_to_hass(hass)
    added = []
    await async_setup_entry(hass, entry, added.extend)
    assert len(added) == 1
    assert isinstance(added[0], EntityGuardMasterEnabledSwitch)


async def test_setup_entry_rule(hass: HomeAssistant):
    from custom_components.entity_guard.switch import async_setup_entry

    entry = _make_rule_entry()
    engine = _make_engine()
    hass.data.setdefault(DOMAIN, {})["engines"] = {entry.entry_id: engine}
    added = []
    await async_setup_entry(hass, entry, added.extend)
    assert len(added) == 2
    types = [type(s).__name__ for s in added]
    assert "EntityGuardEnabledSwitch" in types
    assert "EntityGuardDebounceEnabledSwitch" in types


async def test_setup_entry_unknown_type(hass: HomeAssistant):
    from custom_components.entity_guard.switch import async_setup_entry

    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ENTRY_TYPE: "unknown"}, title="X")
    added = []
    await async_setup_entry(hass, entry, added.extend)
    assert added == []


# ---------------------------------------------------------------------------
# async_added_to_hass — dispatcher subscription
# ---------------------------------------------------------------------------


async def test_rule_switch_async_added_subscribes(hass: HomeAssistant):
    from homeassistant.helpers.dispatcher import async_dispatcher_send

    entry = _make_rule_entry()
    engine = _make_engine()
    sw = EntityGuardEnabledSwitch(entry, engine)
    sw.hass = hass
    sw.async_write_ha_state = MagicMock()

    await sw.async_added_to_hass()

    async_dispatcher_send(hass, f"entity_guard_rule_update_{engine.config.unique_id}")
    await hass.async_block_till_done()
    sw.async_write_ha_state.assert_called()


async def test_master_switch_async_added_subscribes(hass: HomeAssistant):
    from homeassistant.helpers.dispatcher import async_dispatcher_send

    entry = _make_hub_entry()
    entry.add_to_hass(hass)
    sw = EntityGuardMasterEnabledSwitch(hass, entry)
    sw.hass = hass
    sw.async_write_ha_state = MagicMock()

    await sw.async_added_to_hass()

    async_dispatcher_send(hass, "entity_guard_master_update")
    await hass.async_block_till_done()
    sw.async_write_ha_state.assert_called()
