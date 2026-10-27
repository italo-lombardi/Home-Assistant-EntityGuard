"""Tests for issue_helpers (missing flag entity detection)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.entity_guard.const import DOMAIN, ENTRY_TYPE_RULE
from custom_components.entity_guard.issue_helpers import (
    ISSUE_FLAG_ENTITY_MISSING,
    ISSUE_TARGET_ENTITY_MISSING,
    async_check_missing_flag_entities,
    async_check_missing_target_entities,
)


def _rule_entry(**overrides) -> MockConfigEntry:
    data = {
        "entry_type": ENTRY_TYPE_RULE,
        "rule_id": "rule-abc",
        "rule_name": "My Rule",
        "target_entities": ["light.bedroom"],
        "mode": "fixed_state",
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


@asynccontextmanager
async def _setup_wired(hass: HomeAssistant, flags, mock_check):
    """Set up rule entry with async_check_missing_flag_entities mocked.

    Keeps the mock patch active for the duration of the async with block so
    listener callbacks fired after setup still hit the mock.
    """
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
            mock_check,
        ),
    ):
        await async_setup_entry(hass, entry)
        mock_check.reset_mock()
        yield entry


async def test_entity_registry_remove_triggers_check(hass: HomeAssistant) -> None:
    """Deleting a flag entity fires async_check_missing_flag_entities."""
    _register_entity(hass, "input_boolean.night")
    mock_check = AsyncMock()
    async with _setup_wired(
        hass, [{"entity": "input_boolean.night", "match_state": "on"}], mock_check
    ) as entry:
        er.async_get(hass).async_remove("input_boolean.night")
        await hass.async_block_till_done()

    mock_check.assert_awaited_once_with(hass, entry.entry_id)


async def test_entity_registry_create_triggers_check(hass: HomeAssistant) -> None:
    """Recreating a flag entity fires async_check_missing_flag_entities."""
    mock_check = AsyncMock()
    async with _setup_wired(
        hass, [{"entity": "input_boolean.night", "match_state": "on"}], mock_check
    ) as entry:
        _register_entity(hass, "input_boolean.night")
        await hass.async_block_till_done()

    mock_check.assert_awaited_once_with(hass, entry.entry_id)


async def test_unrelated_entity_does_not_trigger_check(hass: HomeAssistant) -> None:
    """Registry events for non-flag entities don't fire the check."""
    mock_check = AsyncMock()
    async with _setup_wired(
        hass, [{"entity": "input_boolean.night", "match_state": "on"}], mock_check
    ):
        _register_entity(hass, "input_boolean.unrelated")
        await hass.async_block_till_done()

    mock_check.assert_not_called()


async def test_listener_unsubscribed_after_entry_unload(hass: HomeAssistant) -> None:
    """Listener stops firing after config entry is unloaded."""
    _register_entity(hass, "input_boolean.night")
    mock_check = AsyncMock()

    async with _setup_wired(
        hass, [{"entity": "input_boolean.night", "match_state": "on"}], mock_check
    ) as entry:
        # Drain on_unload callbacks registered during setup (the listener unsub
        # lives here). _on_unload is accessed as a list, not via a private method
        # call — if HA renames it the test breaks loudly with AttributeError
        # rather than passing silently.
        if entry._on_unload:
            for cb in list(entry._on_unload):
                result = cb()
                if result is not None:
                    await result
            entry._on_unload.clear()

        mock_check.reset_mock()
        er.async_get(hass).async_remove("input_boolean.night")
        await hass.async_block_till_done()

    mock_check.assert_not_called()


# ===========================================================================
# Feature D: target-entity-missing repair issue (mirrors the flag checks)
# ===========================================================================


async def test_target_missing_entry_returns_early(hass: HomeAssistant) -> None:
    await async_check_missing_target_entities(hass, "nonexistent-entry-id")


async def test_no_targets_returns_early(hass: HomeAssistant) -> None:
    entry = _rule_entry(target_entities=[])
    entry.add_to_hass(hass)

    with patch(
        "custom_components.entity_guard.issue_helpers.async_create_issue"
    ) as mock_create:
        await async_check_missing_target_entities(hass, entry.entry_id)

    mock_create.assert_not_called()


async def test_all_targets_present_deletes_issue(hass: HomeAssistant) -> None:
    entry = _rule_entry(target_entities=["light.bedroom"])
    entry.add_to_hass(hass)
    _register_entity(hass, "light.bedroom")

    with (
        patch(
            "custom_components.entity_guard.issue_helpers.async_create_issue"
        ) as mock_create,
        patch(
            "custom_components.entity_guard.issue_helpers.async_delete_issue"
        ) as mock_delete,
    ):
        await async_check_missing_target_entities(hass, entry.entry_id)

    mock_create.assert_not_called()
    mock_delete.assert_called_once_with(
        hass, DOMAIN, f"{entry.entry_id}_missing_targets"
    )


async def test_missing_target_creates_issue(hass: HomeAssistant) -> None:
    entry = _rule_entry(target_entities=["light.bedroom"])
    entry.add_to_hass(hass)
    # light.bedroom not registered → missing

    with (
        patch(
            "custom_components.entity_guard.issue_helpers.async_create_issue"
        ) as mock_create,
        patch(
            "custom_components.entity_guard.issue_helpers.async_delete_issue"
        ) as mock_delete,
    ):
        await async_check_missing_target_entities(hass, entry.entry_id)

    mock_delete.assert_not_called()
    mock_create.assert_called_once()
    args, kwargs = mock_create.call_args
    issue_id = kwargs.get("issue_id") or args[2]
    assert issue_id == f"{entry.entry_id}_missing_targets"
    assert kwargs.get("translation_key") == ISSUE_TARGET_ENTITY_MISSING
    placeholders = kwargs.get("translation_placeholders", {})
    assert "light.bedroom" in placeholders["missing_entities"]
    assert placeholders["rule_name"] == "My Rule"


async def test_partial_targets_missing(hass: HomeAssistant) -> None:
    entry = _rule_entry(target_entities=["light.bedroom", "light.kitchen"])
    entry.add_to_hass(hass)
    _register_entity(hass, "light.bedroom")
    # light.kitchen not registered → missing

    with patch(
        "custom_components.entity_guard.issue_helpers.async_create_issue"
    ) as mock_create:
        await async_check_missing_target_entities(hass, entry.entry_id)

    mock_create.assert_called_once()
    placeholders = mock_create.call_args.kwargs["translation_placeholders"]
    assert "light.kitchen" in placeholders["missing_entities"]
    assert "light.bedroom" not in placeholders["missing_entities"]


async def test_target_parse_error_returns_early(hass: HomeAssistant) -> None:
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
        await async_check_missing_target_entities(hass, entry.entry_id)

    mock_create.assert_not_called()


# ---------------------------------------------------------------------------
# Feature D: listener handles REMOVE and RENAME (old_entity_id), for targets
# AND flags. The flag listener previously missed rename (new id not in set).
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _setup_wired_both(
    hass: HomeAssistant, flags, targets, mock_flag, mock_target
):
    """Set up a rule with BOTH entity checks mocked, so listener assertions can
    distinguish flag vs target firing."""
    from custom_components.entity_guard import async_setup_entry

    entry = _rule_entry(flags=flags, target_entities=targets)
    entry.add_to_hass(hass)

    with (
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock
        ),
        patch(
            "custom_components.entity_guard._async_install_card", new_callable=AsyncMock
        ),
        patch(
            "custom_components.entity_guard.RuleEngine.async_setup",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.entity_guard._async_ensure_hub", new_callable=AsyncMock
        ),
        patch(
            "custom_components.entity_guard.async_check_missing_flag_entities",
            mock_flag,
        ),
        patch(
            "custom_components.entity_guard.async_check_missing_target_entities",
            mock_target,
        ),
    ):
        await async_setup_entry(hass, entry)
        mock_flag.reset_mock()
        mock_target.reset_mock()
        yield entry


async def test_target_remove_triggers_check(hass: HomeAssistant) -> None:
    """Removing a TARGET entity fires the target check."""
    _register_entity(hass, "light.bedroom")
    mock_flag, mock_target = AsyncMock(), AsyncMock()
    async with _setup_wired_both(
        hass, [], ["light.bedroom"], mock_flag, mock_target
    ) as entry:
        er.async_get(hass).async_remove("light.bedroom")
        await hass.async_block_till_done()
    mock_target.assert_awaited_with(hass, entry.entry_id)


async def test_target_rename_triggers_check(hass: HomeAssistant) -> None:
    """Renaming a TARGET (action=update, old_entity_id in set) fires the check —
    the rename path the flag listener previously missed."""
    _register_entity(hass, "light.bedroom")
    mock_flag, mock_target = AsyncMock(), AsyncMock()
    async with _setup_wired_both(
        hass, [], ["light.bedroom"], mock_flag, mock_target
    ) as entry:
        er.async_get(hass).async_update_entity(
            "light.bedroom", new_entity_id="light.bedroom_renamed"
        )
        await hass.async_block_till_done()
    mock_target.assert_awaited_with(hass, entry.entry_id)


async def test_flag_rename_triggers_check(hass: HomeAssistant) -> None:
    """Renaming a FLAG entity now fires the check too (was the latent bug: the
    new id isn't in the watched set, so only old_entity_id matching catches it)."""
    _register_entity(hass, "input_boolean.night")
    _register_entity(hass, "light.bedroom")
    mock_flag, mock_target = AsyncMock(), AsyncMock()
    async with _setup_wired_both(
        hass,
        [{"entity": "input_boolean.night", "match_state": "on"}],
        ["light.bedroom"],
        mock_flag,
        mock_target,
    ) as entry:
        er.async_get(hass).async_update_entity(
            "input_boolean.night", new_entity_id="input_boolean.evening"
        )
        await hass.async_block_till_done()
    mock_flag.assert_awaited_with(hass, entry.entry_id)


async def test_noop_update_does_not_trigger_check(hass: HomeAssistant) -> None:
    """An update that is NOT a rename (no old_entity_id — e.g. icon change) must
    NOT re-run the checks, so unrelated registry churn is ignored."""
    _register_entity(hass, "light.bedroom")
    mock_flag, mock_target = AsyncMock(), AsyncMock()
    async with _setup_wired_both(hass, [], ["light.bedroom"], mock_flag, mock_target):
        er.async_get(hass).async_update_entity("light.bedroom", icon="mdi:lightbulb")
        await hass.async_block_till_done()
    mock_target.assert_not_called()
    mock_flag.assert_not_called()


async def test_state_only_target_not_flagged_missing(hass: HomeAssistant) -> None:
    """A target that exists only in the state machine (no registry entry) — e.g.
    a YAML group.* / template entity without a unique_id — enforces fine and must
    NOT be flagged missing. Registry-only checking would false-positive forever."""
    entry = _rule_entry(target_entities=["group.all_lights"])
    entry.add_to_hass(hass)
    # NOT registered; present only in states.
    hass.states.async_set("group.all_lights", "on")

    with (
        patch(
            "custom_components.entity_guard.issue_helpers.async_create_issue"
        ) as mock_create,
        patch(
            "custom_components.entity_guard.issue_helpers.async_delete_issue"
        ) as mock_delete,
    ):
        await async_check_missing_target_entities(hass, entry.entry_id)

    mock_create.assert_not_called()
    mock_delete.assert_called_once_with(
        hass, DOMAIN, f"{entry.entry_id}_missing_targets"
    )


async def test_state_only_flag_not_flagged_missing(hass: HomeAssistant) -> None:
    """Same for a flag entity present only in the state machine."""
    entry = _rule_entry(flags=[{"entity": "group.presence", "match_state": "on"}])
    entry.add_to_hass(hass)
    hass.states.async_set("group.presence", "on")

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


async def test_target_rename_detected_by_real_check(hass: HomeAssistant) -> None:
    """End-to-end middle link: after a REAL rename, the config still references the
    OLD id (HA does not rewrite config data on rename), and the REAL check (not
    mocked) detects that old id as missing and raises the repair issue with the
    old id in missing_entities. The listener-fires-on-rename plumbing is covered
    separately (test_target_rename_triggers_check); this proves the detection the
    feature actually promises."""
    reg = er.async_get(hass)
    ent = reg.async_get_or_create(
        "light", "test", "bedroom", suggested_object_id="bedroom"
    )
    old_id = ent.entity_id  # light.bedroom
    hass.states.async_set(old_id, "off")

    entry = _rule_entry(target_entities=[old_id])
    entry.add_to_hass(hass)

    # Real rename: registry id changes, config still points at old_id.
    reg.async_update_entity(old_id, new_entity_id="light.bedroom_renamed")
    hass.states.async_remove(old_id)
    await hass.async_block_till_done()

    with patch(
        "custom_components.entity_guard.issue_helpers.async_create_issue"
    ) as mock_create:
        await async_check_missing_target_entities(hass, entry.entry_id)

    mock_create.assert_called_once()
    args, kwargs = mock_create.call_args
    issue_id = kwargs.get("issue_id") or args[2]
    assert issue_id == f"{entry.entry_id}_missing_targets"
    assert old_id in kwargs.get("translation_placeholders", {}).get(
        "missing_entities", ""
    ), "the OLD (dead) id must be reported as missing"
