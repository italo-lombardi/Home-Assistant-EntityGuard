"""Tests for issue_helpers (missing flag entity detection)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.entity_guard.const import DOMAIN, ENTRY_TYPE_RULE
from custom_components.entity_guard.issue_helpers import (
    ISSUE_FLAG_ENTITY_MISSING,
    async_check_missing_flag_entities,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er


def _rule_entry(**overrides) -> MockConfigEntry:
    data = {
        "entry_type": ENTRY_TYPE_RULE,
        "rule_id": "rule-abc",
        "rule_name": "My Rule",
        "target_entities": ["light.bedroom"],
        "mode": "state",
        "trigger_states": ["on"],
        "target_state": "off",
        "delay_seconds": 0,
        "flags": [],
        "debounce_enabled": False,
        "debounce_seconds": 60,
        "max_enforcements_per_minute": 10,
        "safety_acknowledged": False,
    }
    data.update(overrides)
    return MockConfigEntry(domain=DOMAIN, data=data, title="My Rule")


def _register_entity(hass: HomeAssistant, entity_id: str) -> None:
    """Register entity_id in the entity registry so async_get finds it."""
    domain, object_id = entity_id.split(".", 1)
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        domain, "test", object_id, suggested_object_id=object_id
    )


async def test_missing_entry_returns_early(hass: HomeAssistant) -> None:
    await async_check_missing_flag_entities(hass, "nonexistent-entry-id")


async def test_no_flags_returns_early(hass: HomeAssistant) -> None:
    entry = _rule_entry(flags=[])
    entry.add_to_hass(hass)

    with patch(
        "custom_components.entity_guard.issue_helpers.async_create_issue"
    ) as mock_create:
        await async_check_missing_flag_entities(hass, entry.entry_id)

    mock_create.assert_not_called()


async def test_all_flags_present_deletes_issue(hass: HomeAssistant) -> None:
    entry = _rule_entry(flags=[{"entity": "input_boolean.night", "match_state": "on"}])
    entry.add_to_hass(hass)
    _register_entity(hass, "input_boolean.night")

    with (
        patch(
            "custom_components.entity_guard.issue_helpers.async_create_issue"
        ) as mock_create,
        patch(
            "custom_components.entity_guard.issue_helpers.async_delete_issue"
        ) as mock_delete,
    ):
        await async_check_missing_flag_entities(hass, entry.entry_id)

    mock_create.assert_not_called()
    mock_delete.assert_called_once_with(hass, DOMAIN, f"{entry.entry_id}_missing_flags")


async def test_missing_flag_creates_issue(hass: HomeAssistant) -> None:
    entry = _rule_entry(flags=[{"entity": "input_boolean.night", "match_state": "on"}])
    entry.add_to_hass(hass)
    # entity not registered → missing

    with (
        patch(
            "custom_components.entity_guard.issue_helpers.async_create_issue"
        ) as mock_create,
        patch(
            "custom_components.entity_guard.issue_helpers.async_delete_issue"
        ) as mock_delete,
    ):
        await async_check_missing_flag_entities(hass, entry.entry_id)

    mock_delete.assert_not_called()
    mock_create.assert_called_once()
    args, kwargs = mock_create.call_args
    issue_id = kwargs.get("issue_id") or args[2]
    assert issue_id == f"{entry.entry_id}_missing_flags"
    translation_key = kwargs.get("translation_key")
    assert translation_key == ISSUE_FLAG_ENTITY_MISSING
    placeholders = kwargs.get("translation_placeholders", {})
    assert "input_boolean.night" in placeholders["missing_entities"]


async def test_partial_flags_missing(hass: HomeAssistant) -> None:
    entry = _rule_entry(
        flags=[
            {"entity": "input_boolean.night", "match_state": "on"},
            {"entity": "input_boolean.away", "match_state": "on"},
        ]
    )
    entry.add_to_hass(hass)
    _register_entity(hass, "input_boolean.night")
    # input_boolean.away not registered → missing

    with patch(
        "custom_components.entity_guard.issue_helpers.async_create_issue"
    ) as mock_create:
        await async_check_missing_flag_entities(hass, entry.entry_id)

    mock_create.assert_called_once()
    placeholders = mock_create.call_args.kwargs["translation_placeholders"]
    assert "input_boolean.away" in placeholders["missing_entities"]
    assert "input_boolean.night" not in placeholders["missing_entities"]


async def test_parse_error_returns_early(hass: HomeAssistant) -> None:
    entry = _rule_entry()
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.entity_guard.issue_helpers.parse_rule_config",
            side_effect=ValueError("bad config"),
        ),
        patch(
            "custom_components.entity_guard.issue_helpers.async_create_issue"
        ) as mock_create,
    ):
        await async_check_missing_flag_entities(hass, entry.entry_id)

    mock_create.assert_not_called()


# ---------------------------------------------------------------------------
# Listener wiring — entity_registry_updated triggers check
# ---------------------------------------------------------------------------


async def _setup_rule_entry_with_flags(hass, flags):
    """Set up a rule entry via async_setup_entry with the given flags."""
    from unittest.mock import AsyncMock, patch

    from custom_components.entity_guard import async_setup_entry

    entry = _rule_entry(flags=flags)
    entry.add_to_hass(hass)
    with (
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.entity_guard._async_install_card",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.entity_guard.RuleEngine.async_setup",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.entity_guard._async_ensure_hub",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.entity_guard.async_check_missing_flag_entities",
            new_callable=AsyncMock,
        ),
    ):
        await async_setup_entry(hass, entry)
    return entry


async def test_entity_registry_remove_triggers_check(hass: HomeAssistant) -> None:
    """Deleting a flag entity fires async_check_missing_flag_entities."""
    from custom_components.entity_guard import async_setup_entry

    entry = _rule_entry(flags=[{"entity": "input_boolean.night", "match_state": "on"}])
    entry.add_to_hass(hass)
    _register_entity(hass, "input_boolean.night")

    mock_check = AsyncMock()
    with (
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.entity_guard._async_install_card",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.entity_guard.RuleEngine.async_setup",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.entity_guard._async_ensure_hub",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.entity_guard.async_check_missing_flag_entities",
            mock_check,
        ),
    ):
        await async_setup_entry(hass, entry)
        mock_check.reset_mock()

        ent_reg = er.async_get(hass)
        ent_reg.async_remove("input_boolean.night")
        await hass.async_block_till_done()

    mock_check.assert_awaited_once_with(hass, entry.entry_id)


async def test_entity_registry_create_triggers_check(hass: HomeAssistant) -> None:
    """Recreating a flag entity fires async_check_missing_flag_entities."""
    from custom_components.entity_guard import async_setup_entry

    entry = _rule_entry(flags=[{"entity": "input_boolean.night", "match_state": "on"}])
    entry.add_to_hass(hass)

    mock_check = AsyncMock()
    with (
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.entity_guard._async_install_card",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.entity_guard.RuleEngine.async_setup",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.entity_guard._async_ensure_hub",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.entity_guard.async_check_missing_flag_entities",
            mock_check,
        ),
    ):
        await async_setup_entry(hass, entry)
        mock_check.reset_mock()

        _register_entity(hass, "input_boolean.night")
        await hass.async_block_till_done()

    mock_check.assert_awaited_once_with(hass, entry.entry_id)


async def test_unrelated_entity_does_not_trigger_check(hass: HomeAssistant) -> None:
    """Registry events for non-flag entities don't fire the check."""
    from custom_components.entity_guard import async_setup_entry

    entry = _rule_entry(flags=[{"entity": "input_boolean.night", "match_state": "on"}])
    entry.add_to_hass(hass)

    mock_check = AsyncMock()
    with (
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.entity_guard._async_install_card",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.entity_guard.RuleEngine.async_setup",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.entity_guard._async_ensure_hub",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.entity_guard.async_check_missing_flag_entities",
            mock_check,
        ),
    ):
        await async_setup_entry(hass, entry)
        mock_check.reset_mock()

        _register_entity(hass, "input_boolean.unrelated")
        await hass.async_block_till_done()

    mock_check.assert_not_called()
