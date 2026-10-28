"""Tests for the Entity Guard device_trigger platform."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.entity_guard.const import DOMAIN, EVENT_ENFORCED
from custom_components.entity_guard.device_trigger import (
    async_attach_trigger,
    async_get_triggers,
)


def _rule_device(hass: HomeAssistant, unique_id: str = "rule-uuid-1"):
    """Create a rule config entry + its device; return (entry, device)."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={}, title="My Rule", unique_id=unique_id
    )
    entry.add_to_hass(hass)
    dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name="My Rule",
    )
    return entry, dev


async def test_get_triggers_lists_all_three_for_a_rule(hass: HomeAssistant) -> None:
    _entry, dev = _rule_device(hass)
    triggers = await async_get_triggers(hass, dev.id)
    types = {t[CONF_TYPE] for t in triggers}
    assert types == {"enforced", "skipped", "loop_detected"}
    for t in triggers:
        assert t[CONF_PLATFORM] == "device"
        assert t[CONF_DOMAIN] == DOMAIN
        assert t[CONF_DEVICE_ID] == dev.id


def _hub_entry(hass: HomeAssistant):
    e = MockConfigEntry(domain=DOMAIN, data={}, title="Hub")
    e.add_to_hass(hass)
    return e


async def test_get_triggers_none_for_hub_device(hass: HomeAssistant) -> None:
    """The hub device's identifier is (DOMAIN, 'hub') — not a config entry id —
    so it resolves to no entry and offers no rule triggers."""
    hub = _hub_entry(hass)
    dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=hub.entry_id,
        identifiers={(DOMAIN, "hub")},
        name="Hub",
    )
    assert await async_get_triggers(hass, dev.id) == []


async def test_get_triggers_none_for_unknown_device(hass: HomeAssistant) -> None:
    assert await async_get_triggers(hass, "nonexistent-device-id") == []


async def test_get_triggers_none_for_foreign_domain_device(hass: HomeAssistant) -> None:
    """A device with identifiers but NONE from entity_guard (e.g. a device another
    integration owns, surfaced here only because it shares a config entry) offers
    no triggers — exercises the non-DOMAIN identifier skip + the no-match return."""
    entry = _hub_entry(hass)
    dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("some_other_domain", "x1")},
        name="Foreign",
    )
    assert await async_get_triggers(hass, dev.id) == []


async def test_attach_trigger_fires_on_the_rules_event(hass: HomeAssistant) -> None:
    """Attaching an 'enforced' trigger fires the action when THIS rule's
    EVENT_ENFORCED fires (filtered by rule_id == entry.unique_id)."""
    entry, dev = _rule_device(hass, unique_id="rule-uuid-xyz")
    action = AsyncMock()
    config = {
        CONF_PLATFORM: "device",
        CONF_DOMAIN: DOMAIN,
        CONF_DEVICE_ID: dev.id,
        CONF_TYPE: "enforced",
    }
    unsub = await async_attach_trigger(
        hass, config, action, {"trigger_data": {}, "variables": {}}
    )
    try:
        # This rule's event → action fires.
        hass.bus.async_fire(EVENT_ENFORCED, {"rule_id": "rule-uuid-xyz"})
        await hass.async_block_till_done()
        assert action.call_count == 1
        # A different rule's event → action does NOT fire.
        hass.bus.async_fire(EVENT_ENFORCED, {"rule_id": "some-other-rule"})
        await hass.async_block_till_done()
        assert action.call_count == 1
    finally:
        unsub()


async def test_attach_trigger_raises_for_non_rule_device(hass: HomeAssistant) -> None:
    from homeassistant.components.device_automation import DeviceNotFound

    config = {
        CONF_PLATFORM: "device",
        CONF_DOMAIN: DOMAIN,
        CONF_DEVICE_ID: "nonexistent-device-id",
        CONF_TYPE: "enforced",
    }
    with pytest.raises(DeviceNotFound):
        await async_attach_trigger(
            hass, config, AsyncMock(), {"trigger_data": {}, "variables": {}}
        )
