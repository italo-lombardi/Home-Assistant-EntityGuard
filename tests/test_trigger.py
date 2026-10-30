"""Tests for the Entity Guard integration-level trigger platform."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import voluptuous as vol
from homeassistant.const import CONF_OPTIONS, CONF_PLATFORM
from homeassistant.core import HomeAssistant
from homeassistant.helpers.trigger import TriggerConfig
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.entity_guard.const import (
    DOMAIN,
    EVENT_CANCELLED,
    EVENT_ENFORCED,
    EVENT_LOOP_DETECTED,
    EVENT_SKIPPED,
    EVENT_SUPPRESSED,
    EVENT_UNREACHABLE,
    EVENT_VERIFIED,
)
from custom_components.entity_guard.trigger import (
    CONF_CONFIG_ENTRY_ID,
    async_get_triggers,
)

_SEVEN = {
    "enforced",
    "skipped",
    "loop_detected",
    "verified",
    "unreachable",
    "cancelled",
    "suppressed",
}


def _rule_entry(hass: HomeAssistant, unique_id: str = "rule-uuid-1") -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, data={}, title="My Rule", unique_id=unique_id
    )
    entry.add_to_hass(hass)
    return entry


async def test_get_triggers_lists_all_seven(hass: HomeAssistant) -> None:
    triggers = await async_get_triggers(hass)
    assert set(triggers) == _SEVEN


async def test_validate_config_accepts_empty_options(hass: HomeAssistant) -> None:
    """No config_entry_id → any-rule; validates fine."""
    cls = (await async_get_triggers(hass))["enforced"]
    out = await cls.async_validate_config(hass, {CONF_OPTIONS: {}})
    assert out[CONF_OPTIONS] == {}


async def test_validate_config_accepts_known_entry(hass: HomeAssistant) -> None:
    entry = _rule_entry(hass)
    cls = (await async_get_triggers(hass))["enforced"]
    out = await cls.async_validate_config(
        hass, {CONF_OPTIONS: {CONF_CONFIG_ENTRY_ID: entry.entry_id}}
    )
    assert out[CONF_OPTIONS][CONF_CONFIG_ENTRY_ID] == entry.entry_id


async def test_validate_config_rejects_unknown_entry(hass: HomeAssistant) -> None:
    cls = (await async_get_triggers(hass))["enforced"]
    with pytest.raises(vol.Invalid):
        await cls.async_validate_config(
            hass, {CONF_OPTIONS: {CONF_CONFIG_ENTRY_ID: "does-not-exist"}}
        )


async def test_validate_complete_config_folds_top_level_field(
    hass: HomeAssistant,
) -> None:
    """A top-level config_entry_id is folded into options (YAML convenience)."""
    entry = _rule_entry(hass)
    cls = (await async_get_triggers(hass))["enforced"]
    complete = await cls.async_validate_complete_config(
        hass,
        {CONF_PLATFORM: f"{DOMAIN}.enforced", CONF_CONFIG_ENTRY_ID: entry.entry_id},
    )
    assert complete[CONF_OPTIONS][CONF_CONFIG_ENTRY_ID] == entry.entry_id


def _runner() -> MagicMock:
    """A run_action stand-in; returns a MagicMock task so the callback is happy."""
    return MagicMock(return_value=MagicMock())


async def _attach(hass, cls, options):
    trig = cls(hass, TriggerConfig(key="enforced", options=options))
    run_action = _runner()
    unsub = await trig.async_attach_runner(run_action)
    return run_action, unsub


async def test_attach_any_rule_fires_for_every_rule(hass: HomeAssistant) -> None:
    """No config_entry_id → fires on the event regardless of rule_id."""
    cls = (await async_get_triggers(hass))["enforced"]
    run_action, unsub = await _attach(hass, cls, {})
    try:
        hass.bus.async_fire(EVENT_ENFORCED, {"rule_id": "rule-a", "rule_name": "A"})
        await hass.async_block_till_done()
        hass.bus.async_fire(EVENT_ENFORCED, {"rule_id": "rule-b"})
        await hass.async_block_till_done()
        assert run_action.call_count == 2
        # Payload carries rule_id + event_type for downstream conditions.
        payload = run_action.call_args_list[0].args[0]
        assert payload["rule_id"] == "rule-a"
        assert payload["rule_name"] == "A"
        assert payload["event_type"] == EVENT_ENFORCED
    finally:
        unsub()


async def test_attach_per_rule_filters(hass: HomeAssistant) -> None:
    """config_entry_id set → fires only for that rule's unique_id."""
    entry = _rule_entry(hass, unique_id="rule-xyz")
    cls = (await async_get_triggers(hass))["enforced"]
    run_action, unsub = await _attach(hass, cls, {CONF_CONFIG_ENTRY_ID: entry.entry_id})
    try:
        hass.bus.async_fire(EVENT_ENFORCED, {"rule_id": "rule-xyz"})
        await hass.async_block_till_done()
        assert run_action.call_count == 1
        hass.bus.async_fire(EVENT_ENFORCED, {"rule_id": "other-rule"})
        await hass.async_block_till_done()
        assert run_action.call_count == 1
    finally:
        unsub()


async def test_attach_scoped_to_removed_entry_matches_nothing(
    hass: HomeAssistant,
) -> None:
    """Entry removed between validate and attach → rule_id None → fires never."""
    entry = _rule_entry(hass, unique_id="gone")
    cls = (await async_get_triggers(hass))["enforced"]
    entry_id = entry.entry_id
    await hass.config_entries.async_remove(entry_id)
    run_action, unsub = await _attach(hass, cls, {CONF_CONFIG_ENTRY_ID: entry_id})
    try:
        hass.bus.async_fire(EVENT_ENFORCED, {"rule_id": "gone"})
        await hass.async_block_till_done()
        assert run_action.call_count == 0
        # Also must not fire for a payload with no rule_id key at all
        # (None != None would be False — the guard must still match nothing).
        hass.bus.async_fire(EVENT_ENFORCED, {})
        await hass.async_block_till_done()
        assert run_action.call_count == 0
    finally:
        unsub()


@pytest.mark.parametrize(
    ("trigger_type", "event_type"),
    [
        ("skipped", EVENT_SKIPPED),
        ("loop_detected", EVENT_LOOP_DETECTED),
        ("verified", EVENT_VERIFIED),
        ("unreachable", EVENT_UNREACHABLE),
        ("cancelled", EVENT_CANCELLED),
        ("suppressed", EVENT_SUPPRESSED),
    ],
)
async def test_each_type_listens_on_its_event(
    hass: HomeAssistant, trigger_type: str, event_type: str
) -> None:
    """Every trigger type binds to its own bus event (generic filter path)."""
    cls = (await async_get_triggers(hass))[trigger_type]
    trig = cls(hass, TriggerConfig(key=trigger_type, options={}))
    run_action = _runner()
    unsub = await trig.async_attach_runner(run_action)
    try:
        hass.bus.async_fire(event_type, {"rule_id": "r1"})
        await hass.async_block_till_done()
        assert run_action.call_count == 1
        assert run_action.call_args.args[0]["event_type"] == event_type
    finally:
        unsub()
