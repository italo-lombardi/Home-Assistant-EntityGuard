"""Extra coverage tests for __init__.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant

from custom_components.entity_guard import (
    _async_ensure_hub,
    _async_install_card,
    _async_register_lovelace_resource,
    _async_update_listener,
    _get_version,
    _master_enabled_getter,
    async_remove_entry,
    async_setup,
    async_setup_entry,
)
from custom_components.entity_guard.const import (
    DOMAIN,
)


# ---------------------------------------------------------------------------
# _get_version
# ---------------------------------------------------------------------------


def test_get_version_returns_string():
    v = _get_version()
    assert isinstance(v, str)
    assert v != "0.0.0"


# ---------------------------------------------------------------------------
# async_setup
# ---------------------------------------------------------------------------


async def test_async_setup_initializes_data(hass: HomeAssistant):
    with patch(
        "custom_components.entity_guard.async_register_services",
        new_callable=AsyncMock,
    ):
        result = await async_setup(hass, {})
    assert result is True
    assert "engines" in hass.data[DOMAIN]
    assert "hub_master_enabled" in hass.data[DOMAIN]
    assert "storage" in hass.data[DOMAIN]


async def test_async_setup_idempotent(hass: HomeAssistant):
    with patch(
        "custom_components.entity_guard.async_register_services",
        new_callable=AsyncMock,
    ):
        await async_setup(hass, {})
        store = hass.data[DOMAIN]["storage"]
        await async_setup(hass, {})
    assert hass.data[DOMAIN]["storage"] is store


# ---------------------------------------------------------------------------
# _master_enabled_getter
# ---------------------------------------------------------------------------


async def test_master_getter_uses_hub_object(hass: HomeAssistant):
    hub = MagicMock()
    hub.enabled = False
    hass.data[DOMAIN] = {"hub": hub}
    assert _master_enabled_getter(hass)() is False


async def test_master_getter_falls_back_to_flag(hass: HomeAssistant):
    hass.data[DOMAIN] = {"hub": None, "hub_master_enabled": False}
    assert _master_enabled_getter(hass)() is False


async def test_master_getter_falls_back_to_entry(hass: HomeAssistant, hub_entry):
    hub_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(hub_entry, options={"master_enabled": False})
    hass.data[DOMAIN] = {}
    assert _master_enabled_getter(hass)() is False


async def test_master_getter_default_true(hass: HomeAssistant):
    hass.data[DOMAIN] = {}
    assert _master_enabled_getter(hass)() is True


# ---------------------------------------------------------------------------
# async_setup_entry — engine setup failure
# ---------------------------------------------------------------------------


async def test_setup_rule_entry_engine_failure(hass: HomeAssistant, rule_entry):
    rule_entry.add_to_hass(hass)
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
            side_effect=RuntimeError("boom"),
        ),
    ):
        result = await async_setup_entry(hass, rule_entry)
    assert result is False


async def test_update_listener_reloads_entry(hass: HomeAssistant, rule_entry):
    rule_entry.add_to_hass(hass)
    with patch.object(
        hass.config_entries, "async_reload", new_callable=AsyncMock
    ) as mock_reload:
        await _async_update_listener(hass, rule_entry)
    mock_reload.assert_awaited_once_with(rule_entry.entry_id)


# ---------------------------------------------------------------------------
# device-name sync on setup
# ---------------------------------------------------------------------------


async def test_setup_rule_entry_syncs_device_name(hass: HomeAssistant, rule_entry):
    """Setup updates stale device-registry name to entry.title."""
    from homeassistant.helpers import device_registry as dr

    rule_entry.add_to_hass(hass)
    device_reg = dr.async_get(hass)
    # Pre-create device with stale name
    device_reg.async_get_or_create(
        config_entry_id=rule_entry.entry_id,
        identifiers={(DOMAIN, rule_entry.entry_id)},
        name="Old Name",
    )
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
    ):
        result = await async_setup_entry(hass, rule_entry)
    assert result is True
    device = device_reg.async_get_device(identifiers={(DOMAIN, rule_entry.entry_id)})
    assert device.name == rule_entry.title


# ---------------------------------------------------------------------------
# async_remove_entry
# ---------------------------------------------------------------------------


async def test_async_remove_rule_entry(hass: HomeAssistant, rule_entry):
    rule_entry.add_to_hass(hass)
    await async_remove_entry(hass, rule_entry)


async def test_async_remove_hub_no_rules(hass: HomeAssistant, hub_entry):
    hub_entry.add_to_hass(hass)
    await async_remove_entry(hass, hub_entry)


async def test_async_remove_hub_with_rules_recreates(
    hass: HomeAssistant, hub_entry, rule_entry
):
    hub_entry.add_to_hass(hass)
    rule_entry.add_to_hass(hass)
    with patch(
        "custom_components.entity_guard._async_ensure_hub", new_callable=AsyncMock
    ) as mock_ensure:
        await async_remove_entry(hass, hub_entry)
    mock_ensure.assert_called_once()


# ---------------------------------------------------------------------------
# _async_ensure_hub
# ---------------------------------------------------------------------------


async def test_ensure_hub_skips_when_present(hass: HomeAssistant, hub_entry):
    hub_entry.add_to_hass(hass)
    with patch.object(hass.config_entries.flow, "async_init") as mock_init:
        await _async_ensure_hub(hass)
    mock_init.assert_not_called()


async def test_ensure_hub_creates_when_missing(hass: HomeAssistant):
    with patch.object(
        hass.config_entries.flow, "async_init", new_callable=AsyncMock
    ) as mock_init:
        await _async_ensure_hub(hass)
        await hass.async_block_till_done()
    mock_init.assert_called()


async def test_ensure_hub_swallows_flow_exception(hass: HomeAssistant):
    with patch.object(
        hass.config_entries.flow,
        "async_init",
        new_callable=AsyncMock,
        side_effect=RuntimeError("nope"),
    ):
        await _async_ensure_hub(hass)
        await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# _async_install_card
# ---------------------------------------------------------------------------


async def test_install_card_skips_when_already_installed(hass: HomeAssistant):
    hass.data.setdefault(DOMAIN, {})["_card_installed"] = True
    await _async_install_card(hass)


async def test_install_card_warn_when_no_file(hass: HomeAssistant):
    hass.data.setdefault(DOMAIN, {})
    with patch.object(Path, "exists", return_value=False):
        await _async_install_card(hass)
    assert not hass.data[DOMAIN].get("_card_installed")


async def test_install_card_static_path_already_registered(hass: HomeAssistant):
    hass.data.setdefault(DOMAIN, {})
    fake_http = MagicMock()
    fake_http.async_register_static_paths = AsyncMock(side_effect=ValueError("already"))
    hass.http = fake_http
    with patch(
        "custom_components.entity_guard._async_register_lovelace_resource",
        new_callable=AsyncMock,
    ):
        await _async_install_card(hass)
    assert hass.data[DOMAIN].get("_card_installed") is True


# ---------------------------------------------------------------------------
# _async_register_lovelace_resource
# ---------------------------------------------------------------------------


async def test_register_resource_no_lovelace(hass: HomeAssistant):
    # No `lovelace` key in hass.data → logs and returns
    await _async_register_lovelace_resource(hass, "1.0.0")


async def test_register_resource_creates_new(hass: HomeAssistant):
    resources = MagicMock()
    resources.loaded = True
    resources.async_items = MagicMock(return_value=[])
    resources.async_create_item = AsyncMock()
    lovelace = MagicMock()
    lovelace.resources = resources
    hass.data["lovelace"] = lovelace

    await _async_register_lovelace_resource(hass, "1.0.0")
    resources.async_create_item.assert_called_once()


async def test_register_resource_loads_first(hass: HomeAssistant):
    resources = MagicMock()
    resources.loaded = False
    resources.async_load = AsyncMock()
    resources.async_items = MagicMock(return_value=[])
    resources.async_create_item = AsyncMock()
    lovelace = MagicMock()
    lovelace.resources = resources
    hass.data["lovelace"] = lovelace

    await _async_register_lovelace_resource(hass, "1.0.0")
    resources.async_load.assert_awaited()


async def test_register_resource_existing_matches(hass: HomeAssistant):
    expected_url = "/entity_guard/entity-guard-card.js?automatically-added&1.0.0"
    resources = MagicMock()
    resources.loaded = True
    resources.async_items = MagicMock(return_value=[{"id": "x", "url": expected_url}])
    resources.async_create_item = AsyncMock()
    lovelace = MagicMock()
    lovelace.resources = resources
    hass.data["lovelace"] = lovelace

    await _async_register_lovelace_resource(hass, "1.0.0")
    resources.async_create_item.assert_not_called()


# ---------------------------------------------------------------------------
# Coverage gaps
# ---------------------------------------------------------------------------


async def test_setup_rule_entry_startup_not_running_registers_listener(
    hass: HomeAssistant, rule_entry
):
    """When HA is not running, deferred flag check registers a startup listener."""

    rule_entry.add_to_hass(hass)

    with patch.object(
        type(hass), "is_running", new_callable=lambda: property(lambda self: False)
    ):
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
            ) as mock_check,
        ):
            result = await async_setup_entry(hass, rule_entry)

    assert result is True
    mock_check.assert_not_called()  # deferred, not called yet


async def test_unload_rule_entry_with_engine(hass: HomeAssistant, rule_entry) -> None:
    """Unloading a rule entry with a live engine calls engine.async_unload."""
    from custom_components.entity_guard import async_unload_entry

    rule_entry.add_to_hass(hass)
    mock_engine = MagicMock()
    mock_engine.async_unload = AsyncMock()
    hass.data.setdefault(DOMAIN, {}).setdefault("engines", {})[rule_entry.entry_id] = (
        mock_engine
    )

    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        new_callable=AsyncMock,
        return_value=True,
    ):
        result = await async_unload_entry(hass, rule_entry)

    assert result is True
    mock_engine.async_unload.assert_awaited_once()


async def test_register_resource_deletes_duplicate(hass: HomeAssistant):
    """When multiple resources match card filename, extras are deleted."""
    from homeassistant.components.lovelace.resources import ResourceStorageCollection

    resources = MagicMock(spec=ResourceStorageCollection)
    resources.loaded = True
    resources.async_items = MagicMock(
        return_value=[
            {"id": "1", "url": "/entity_guard/entity-guard-card.js?v1"},
            {"id": "2", "url": "/entity_guard/entity-guard-card.js?v2"},
        ]
    )
    resources.async_delete_item = AsyncMock()
    resources.async_update_item = AsyncMock()
    lovelace = MagicMock()
    lovelace.resources = resources
    hass.data["lovelace"] = lovelace

    await _async_register_lovelace_resource(hass, "2.0.0")
    resources.async_delete_item.assert_awaited_once_with("2")


async def test_register_resource_updates_stale_url(hass: HomeAssistant):
    """When existing resource URL is stale, it is updated to new version."""
    from homeassistant.components.lovelace.resources import ResourceStorageCollection

    old_url = "/entity_guard/entity-guard-card.js?automatically-added&0.9.0"
    resources = MagicMock(spec=ResourceStorageCollection)
    resources.loaded = True
    resources.async_items = MagicMock(return_value=[{"id": "x", "url": old_url}])
    resources.async_update_item = AsyncMock()
    lovelace = MagicMock()
    lovelace.resources = resources
    hass.data["lovelace"] = lovelace

    await _async_register_lovelace_resource(hass, "1.0.0")
    resources.async_update_item.assert_awaited_once()
    new_url = resources.async_update_item.call_args[0][1]["url"]
    assert "1.0.0" in new_url
