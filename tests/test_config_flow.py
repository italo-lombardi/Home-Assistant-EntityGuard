"""Tests for Entity Guard config flow (skeleton)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.entity_guard.const import (
    DOMAIN,
)


async def test_user_step_goes_straight_to_rule(hass: HomeAssistant) -> None:
    """User step skips menu and lands directly on the rule basics form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "rule"


async def test_hub_import_single_instance_aborts(
    hass: HomeAssistant, hub_entry
) -> None:
    """Importing a second hub entry aborts when one already exists."""
    hub_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "import"}, data={}
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"
