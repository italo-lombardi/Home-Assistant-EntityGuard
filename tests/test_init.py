"""Tests for Entity Guard integration setup and unload."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant

from custom_components.entity_guard import (
    PLATFORMS_HUB,
    PLATFORMS_RULE,
    async_remove_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.entity_guard.const import CONF_ENTRY_TYPE, DOMAIN


async def test_setup_hub_entry(hass: HomeAssistant, hub_entry) -> None:
    """Hub entry setup forwards to hub platforms."""
    hub_entry.add_to_hass(hass)
    with (
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new_callable=AsyncMock,
        ) as mock_forward,
        patch(
            "custom_components.entity_guard._async_install_card",
            new_callable=AsyncMock,
        ),
    ):
        result = await async_setup_entry(hass, hub_entry)

    assert result is True
    assert DOMAIN in hass.data
    mock_forward.assert_called_once_with(hub_entry, PLATFORMS_HUB)


async def test_setup_rule_entry(hass: HomeAssistant, rule_entry) -> None:
    """Rule entry setup forwards to rule platforms."""
    rule_entry.add_to_hass(hass)
    with (
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new_callable=AsyncMock,
        ) as mock_forward,
        patch(
            "custom_components.entity_guard._async_install_card",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.entity_guard.RuleEngine.async_setup",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.entity_guard.RuleEngine.async_unload",
            new_callable=AsyncMock,
        ),
    ):
        result = await async_setup_entry(hass, rule_entry)

    assert result is True
    mock_forward.assert_called_once_with(rule_entry, PLATFORMS_RULE)


async def test_unload_hub_entry(hass: HomeAssistant, hub_entry) -> None:
    """Hub entry unloads hub platforms."""
    hub_entry.add_to_hass(hass)
    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock_unload:
        result = await async_unload_entry(hass, hub_entry)

    assert result is True
    mock_unload.assert_called_once_with(hub_entry, PLATFORMS_HUB)


async def test_unload_rule_entry(hass: HomeAssistant, rule_entry) -> None:
    """Rule entry unloads rule platforms."""
    rule_entry.add_to_hass(hass)
    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock_unload:
        result = await async_unload_entry(hass, rule_entry)

    assert result is True
    mock_unload.assert_called_once_with(rule_entry, PLATFORMS_RULE)


async def test_remove_rule_entry_clears_statistics(
    hass: HomeAssistant, rule_entry
) -> None:
    """Rule removal clears orphaned statistics."""
    rule_entry.add_to_hass(hass)

    entities = [
        type("Entity", (), {"entity_id": "sensor.test_rule_cooldown_remaining"})(),
        type("Entity", (), {"entity_id": "sensor.test_rule_enforcement_count_today"})(),
        type("Entity", (), {"entity_id": "sensor.test_rule_enforcement_count_total"})(),
    ]

    # Real Recorder.async_clear_statistics is sync (@callback), so MagicMock matches.
    mock_clear = MagicMock()
    mock_recorder_instance = type("Recorder", (), {})()
    mock_recorder_instance.async_clear_statistics = mock_clear

    with (
        patch("custom_components.entity_guard.er.async_get"),
        patch(
            "custom_components.entity_guard.er.async_entries_for_config_entry",
            return_value=entities,
        ),
        patch(
            "custom_components.entity_guard.recorder_get_instance",
            return_value=mock_recorder_instance,
        ),
    ):
        await async_remove_entry(hass, rule_entry)

    mock_clear.assert_called_once()
    statistic_ids = mock_clear.call_args[0][0]
    assert len(statistic_ids) == 3
    assert all(isinstance(id, str) for id in statistic_ids)
    assert all(id.startswith("sensor.") for id in statistic_ids)


async def test_remove_hub_entry_skips_statistics(
    hass: HomeAssistant, hub_entry
) -> None:
    """Hub removal does not clear statistics."""
    hub_entry.add_to_hass(hass)
    with patch(
        "custom_components.entity_guard.recorder_get_instance"
    ) as mock_get_instance:
        await async_remove_entry(hass, hub_entry)

    mock_get_instance.assert_not_called()


async def test_unload_last_rule_with_hub_present_still_unloads_services(
    hass: HomeAssistant, rule_entry, hub_entry
) -> None:
    """When last rule is unloaded but hub entry still exists, services are unloaded.

    Regression: old code checked 'no remaining entries of ANY type'; hub entry kept
    remaining_entries non-empty, so async_unload_services was never reached.
    Correct: only rule entries block service teardown, hub entry does not.
    """
    hub_entry.add_to_hass(hass)
    rule_entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})["engines"] = {}

    with (
        patch.object(
            hass.config_entries,
            "async_unload_platforms",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "custom_components.entity_guard.async_unload_services"
        ) as mock_unload_services,
    ):
        result = await async_unload_entry(hass, rule_entry)

    assert result is True
    mock_unload_services.assert_called_once_with(hass)


async def test_unload_rule_with_sibling_rule_does_not_unload_services(
    hass: HomeAssistant, rule_entry, hub_entry
) -> None:
    """When a rule is unloaded but another rule entry still exists, services are kept."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.entity_guard.const import ENTRY_TYPE_RULE

    hub_entry.add_to_hass(hass)
    rule_entry.add_to_hass(hass)

    sibling = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_RULE},
        title="Sibling Rule",
    )
    sibling.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})["engines"] = {}

    with (
        patch.object(
            hass.config_entries,
            "async_unload_platforms",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "custom_components.entity_guard.async_unload_services"
        ) as mock_unload_services,
    ):
        result = await async_unload_entry(hass, rule_entry)

    assert result is True
    mock_unload_services.assert_not_called()
