"""Tests for Entity Guard number platform."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.entity_guard.const import (
    CONF_DEBOUNCE_SECONDS,
    CONF_DELAY_SECONDS,
    CONF_ENTRY_TYPE,
    CONF_MAX_ENFORCEMENTS_PER_MINUTE,
    DEFAULT_DEBOUNCE_SECONDS,
    DEFAULT_DELAY_SECONDS,
    DEFAULT_MAX_ENFORCEMENTS_PER_MINUTE,
    DOMAIN,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_RULE,
)
from custom_components.entity_guard.number import (
    EntityGuardDebounceSecondsNumber,
    EntityGuardDelaySecondsNumber,
    EntityGuardMaxEnforcementsNumber,
)


def _make_engine(delay=5, debounce=60, rate=10):
    engine = MagicMock()
    engine.config.unique_id = "test-uid"
    engine.config.delay_seconds = delay
    engine.config.debounce_seconds = debounce
    engine.config.max_enforcements_per_minute = rate
    return engine


def _make_rule_entry(**data_overrides):
    data = {
        CONF_ENTRY_TYPE: ENTRY_TYPE_RULE,
        CONF_DELAY_SECONDS: DEFAULT_DELAY_SECONDS,
        CONF_DEBOUNCE_SECONDS: DEFAULT_DEBOUNCE_SECONDS,
        CONF_MAX_ENFORCEMENTS_PER_MINUTE: DEFAULT_MAX_ENFORCEMENTS_PER_MINUTE,
    }
    data.update(data_overrides)
    return MockConfigEntry(domain=DOMAIN, data=data, title="Rule")


# ---------------------------------------------------------------------------
# EntityGuardDelaySecondsNumber
# ---------------------------------------------------------------------------


def test_delay_native_value_from_config():
    entry = _make_rule_entry()
    engine = _make_engine(delay=15)
    num = EntityGuardDelaySecondsNumber(entry, engine)
    assert num.native_value == 15.0


def test_delay_native_value_from_entry_when_config_missing():
    entry = _make_rule_entry(**{CONF_DELAY_SECONDS: 30})
    engine = _make_engine()
    engine.config.delay_seconds = None
    num = EntityGuardDelaySecondsNumber(entry, engine)
    assert num.native_value == 30.0


async def test_delay_set_native_value(hass: HomeAssistant):
    entry = _make_rule_entry()
    entry.add_to_hass(hass)
    engine = _make_engine(delay=0)
    num = EntityGuardDelaySecondsNumber(entry, engine)
    num.hass = hass
    num._attr_available = True
    num.async_write_ha_state = MagicMock()
    await num.async_set_native_value(20.0)
    assert engine.config.delay_seconds == 20
    # Value applied live to engine; not written to entry.data to avoid triggering reload.


# ---------------------------------------------------------------------------
# EntityGuardDebounceSecondsNumber
# ---------------------------------------------------------------------------


def test_debounce_native_value():
    entry = _make_rule_entry()
    engine = _make_engine(debounce=90)
    num = EntityGuardDebounceSecondsNumber(entry, engine)
    assert num.native_value == 90.0


async def test_debounce_set_native_value(hass: HomeAssistant):
    entry = _make_rule_entry()
    entry.add_to_hass(hass)
    engine = _make_engine(debounce=60)
    num = EntityGuardDebounceSecondsNumber(entry, engine)
    num.hass = hass
    num._attr_available = True
    num.async_write_ha_state = MagicMock()
    await num.async_set_native_value(120.0)
    assert engine.config.debounce_seconds == 120
    # Value applied live to engine; not written to entry.data to avoid triggering reload.


# ---------------------------------------------------------------------------
# EntityGuardMaxEnforcementsNumber
# ---------------------------------------------------------------------------


def test_max_enforcements_native_value():
    entry = _make_rule_entry()
    engine = _make_engine(rate=5)
    num = EntityGuardMaxEnforcementsNumber(entry, engine)
    assert num.native_value == 5.0


async def test_max_enforcements_set_native_value(hass: HomeAssistant):
    entry = _make_rule_entry()
    entry.add_to_hass(hass)
    engine = _make_engine(rate=10)
    num = EntityGuardMaxEnforcementsNumber(entry, engine)
    num.hass = hass
    num._attr_available = True
    num.async_write_ha_state = MagicMock()
    await num.async_set_native_value(3.0)
    assert engine.config.max_enforcements_per_minute == 3
    # Value applied live to engine; not written to entry.data to avoid triggering reload.


# ---------------------------------------------------------------------------
# async_setup_entry
# ---------------------------------------------------------------------------


async def test_setup_entry_skips_hub(hass: HomeAssistant):
    from custom_components.entity_guard.number import async_setup_entry

    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_ENTRY_TYPE: ENTRY_TYPE_HUB}, title="Hub"
    )
    added = []
    await async_setup_entry(hass, entry, added.extend)
    assert added == []


async def test_setup_entry_adds_numbers(hass: HomeAssistant):
    from custom_components.entity_guard.number import async_setup_entry

    entry = _make_rule_entry()
    engine = _make_engine()
    hass.data.setdefault(DOMAIN, {})["engines"] = {entry.entry_id: engine}
    added = []
    await async_setup_entry(hass, entry, added.extend)
    assert len(added) == 3
    types = [type(s).__name__ for s in added]
    assert "EntityGuardDelaySecondsNumber" in types
    assert "EntityGuardDebounceSecondsNumber" in types
    assert "EntityGuardMaxEnforcementsNumber" in types


# ---------------------------------------------------------------------------
# async_added_to_hass — dispatcher subscription and availability
# ---------------------------------------------------------------------------


async def test_number_async_added_subscribes(hass: HomeAssistant):
    from homeassistant.helpers.dispatcher import async_dispatcher_send

    entry = _make_rule_entry()
    engine = _make_engine(delay=5)
    num = EntityGuardDelaySecondsNumber(entry, engine)
    num.hass = hass
    num.async_write_ha_state = MagicMock()

    assert num._attr_available is False
    await num.async_added_to_hass()
    assert num._attr_available is True

    async_dispatcher_send(hass, f"entity_guard_rule_update_{engine.config.unique_id}")
    await hass.async_block_till_done()
    num.async_write_ha_state.assert_called()
