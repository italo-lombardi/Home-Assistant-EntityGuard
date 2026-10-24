"""Tests for async_migrate_entry (config-entry schema v1 → v2 mode rename)."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.entity_guard import async_migrate_entry
from custom_components.entity_guard.const import (
    CONF_ENTRY_TYPE,
    CONF_MODE,
    DOMAIN,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_RULE,
)


@pytest.mark.parametrize(
    "old,new",
    [
        ("state", "fixed_state"),
        ("attribute", "fixed_attribute"),
        ("follow", "dynamic_state"),
        ("follow_entity", "dynamic_state"),
    ],
)
async def test_migrate_v1_remaps_mode_in_data_and_options(
    hass: HomeAssistant, old: str, new: str
) -> None:
    """v1 entry: the mode value is renamed in BOTH data and options, version→2."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_RULE, CONF_MODE: old, "rule_name": "R"},
        options={CONF_MODE: old},
        title="R",
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True
    assert entry.version == 2
    assert entry.data[CONF_MODE] == new
    assert entry.options[CONF_MODE] == new


@pytest.mark.parametrize("old", ["follow", "follow_entity"])
async def test_migrate_follow_with_attribute_drops_attr(
    hass: HomeAssistant, old: str
) -> None:
    """A pre-release follow(+_entity) rule WITH an attribute migrates to
    dynamic_state with CONF_ATTRIBUTE DROPPED (dynamic_state is state-only; the
    attribute add-on never shipped). Proves the drop is deliberate + clean, not a
    crash or a half-synthesized dynamic_attribute entry."""
    from custom_components.entity_guard.const import CONF_ATTRIBUTE

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_RULE,
            CONF_MODE: old,
            CONF_ATTRIBUTE: "brightness",
            "source_entity": "binary_sensor.motion",
        },
        options={CONF_MODE: old, CONF_ATTRIBUTE: "brightness"},
        title="R",
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True
    assert entry.version == 2
    assert entry.data[CONF_MODE] == "dynamic_state"
    assert entry.options[CONF_MODE] == "dynamic_state"
    assert CONF_ATTRIBUTE not in entry.data
    assert CONF_ATTRIBUTE not in entry.options


async def test_migrate_v1_data_only(hass: HomeAssistant) -> None:
    """Mode only in data (no options) still migrates; options stays empty."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_RULE, CONF_MODE: "attribute"},
        title="R",
    )
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry) is True
    assert entry.version == 2
    assert entry.data[CONF_MODE] == "fixed_attribute"
    assert CONF_MODE not in entry.options


async def test_migrate_v1_options_override(hass: HomeAssistant) -> None:
    """A live mode edit in options is migrated independently of data."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_RULE, CONF_MODE: "state"},
        options={CONF_MODE: "attribute"},
        title="R",
    )
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry) is True
    assert entry.data[CONF_MODE] == "fixed_state"
    assert entry.options[CONF_MODE] == "fixed_attribute"


async def test_migrate_hub_entry_no_mode_passthrough(hass: HomeAssistant) -> None:
    """A hub entry has no mode — migration must not raise, just stamp v2."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_HUB},
        title="Hub",
    )
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry) is True
    assert entry.version == 2
    assert CONF_MODE not in entry.data


async def test_migrate_unknown_mode_passthrough(hass: HomeAssistant) -> None:
    """An unexpected mode value is left unchanged (no KeyError), version→2."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_RULE, CONF_MODE: "weird"},
        title="R",
    )
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry) is True
    assert entry.version == 2
    assert entry.data[CONF_MODE] == "weird"


async def test_migrate_already_v2_is_noop(hass: HomeAssistant) -> None:
    """An entry already at v2 is left untouched (idempotent)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_RULE, CONF_MODE: "fixed_state"},
        title="R",
    )
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry) is True
    assert entry.version == 2
    assert entry.data[CONF_MODE] == "fixed_state"
