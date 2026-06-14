"""Entity Guard integration."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.components.recorder import get_instance as recorder_get_instance
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.issue_registry import async_delete_issue

from .const import (
    CONF_ENTRY_TYPE,
    DOMAIN,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_RULE,
)
from .models import parse_rule_config
from .issue_helpers import async_check_missing_flag_entities, missing_flags_issue_id
from .rule_engine import RuleEngine, signal_for_rule, signal_master_update
from .services import async_register_services, async_unload_services
from .storage import EntityGuardStore

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

__all__ = ["signal_for_rule", "signal_master_update"]

_LOGGER = logging.getLogger(__name__)

PLATFORMS_HUB: list[Platform] = [Platform.SWITCH]
PLATFORMS_RULE: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
]

CARD_FILENAME = "entity-guard-card.js"
CARD_URL = f"/entity_guard/{CARD_FILENAME}"
_CARD_INSTALLED_KEY = "_card_installed"


def _get_version() -> str:
    """Get integration version from manifest."""
    manifest = Path(__file__).parent / "manifest.json"
    with manifest.open() as f:
        return json.load(f).get("version", "0.0.0")


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Entity Guard integration."""
    _LOGGER.debug("async_setup invoked")
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault("engines", {})
    hass.data[DOMAIN].setdefault("hub_master_enabled", True)
    hass.data[DOMAIN].setdefault("hub", None)
    if "storage" not in hass.data[DOMAIN]:
        store = EntityGuardStore(hass)
        await store.async_load()
        hass.data[DOMAIN]["storage"] = store
        _LOGGER.debug("Storage initialized")
    await async_register_services(hass)
    _LOGGER.info("Entity Guard integration loaded")
    return True


def _master_enabled_getter(hass: HomeAssistant):
    """Return a callable resolving the current master switch state."""

    def _get() -> bool:
        domain_data = hass.data.get(DOMAIN, {})
        hub = domain_data.get("hub")
        if hub is not None:
            return bool(getattr(hub, "enabled", True))
        if "hub_master_enabled" in domain_data:
            return bool(domain_data["hub_master_enabled"])
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_HUB:
                return bool(entry.options.get("master_enabled", True))
        return True

    return _get


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Entity Guard from a config entry."""
    entry_type = entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_RULE)
    _LOGGER.debug(
        "async_setup_entry start: entry_id=%s type=%s title=%s",
        entry.entry_id,
        entry_type,
        entry.title,
    )
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault("engines", {})
    hass.data[DOMAIN].setdefault("hub_master_enabled", True)
    hass.data[DOMAIN].setdefault("hub", None)
    if "storage" not in hass.data[DOMAIN]:
        store = EntityGuardStore(hass)
        await store.async_load()
        hass.data[DOMAIN]["storage"] = store
        _LOGGER.debug("Storage lazy-initialized in setup_entry")

    if entry_type == ENTRY_TYPE_HUB:
        _LOGGER.info("Setting up hub entry %s", entry.entry_id)
        hass.data[DOMAIN]["hub_master_enabled"] = bool(
            entry.options.get("master_enabled", True)
        )
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS_HUB)
        _LOGGER.debug("Hub platforms forwarded: %s", PLATFORMS_HUB)
    else:
        _LOGGER.info("Setting up rule entry %s (%s)", entry.entry_id, entry.title)
        config = parse_rule_config(entry)
        store = hass.data[DOMAIN]["storage"]
        engine = RuleEngine(hass, config, store, _master_enabled_getter(hass))
        try:
            await engine.async_setup()
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Engine setup failed for rule entry %s; entry will not load",
                entry.entry_id,
            )
            return False
        # Restore persisted per-rule enabled state (written by panic_stop).
        if not entry.options.get("enabled", True):
            engine.set_enabled(False)
        hass.data[DOMAIN]["engines"][entry.entry_id] = engine
        _LOGGER.debug(
            "Rule engine ready: rule_id=%s targets=%s mode=%s",
            config.unique_id,
            config.target_entities,
            config.mode,
        )
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS_RULE)

        # Defer flag-entity check until HA is fully started so dependent integrations
        # have populated their states; otherwise we'd raise false-positive missing-flag
        # repair issues on every restart.
        async def _deferred_flag_check(_event=None) -> None:
            await async_check_missing_flag_entities(hass, entry.entry_id)

        if hass.is_running:
            await _deferred_flag_check()
        else:
            _flag_check_done = False

            async def _deferred_flag_check_once(_event=None) -> None:
                nonlocal _flag_check_done
                _flag_check_done = True
                await async_check_missing_flag_entities(hass, entry.entry_id)

            unsub = hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, _deferred_flag_check_once
            )

            @callback
            def _cancel_flag_check_listener() -> None:  # pragma: no cover
                if not _flag_check_done:
                    try:
                        unsub()
                    except Exception:  # noqa: BLE001
                        pass

            entry.async_on_unload(_cancel_flag_check_listener)

        # flag_entity_ids is computed once at setup. Options edits that reload the
        # entry re-invoke async_setup_entry, so the frozenset is rebuilt on reload.
        flag_entity_ids = frozenset(f.entity for f in config.flags)

        @callback
        def _on_entity_registry_updated(event) -> None:
            entity_id = event.data.get("entity_id")
            _LOGGER.debug(
                "entity_registry_updated: action=%s entity_id=%s flag_ids=%s",
                event.data.get("action"),
                entity_id,
                flag_entity_ids,
            )
            if entity_id not in flag_entity_ids:
                return
            hass.async_create_task(
                async_check_missing_flag_entities(hass, entry.entry_id)
            )

        if flag_entity_ids:
            unsub_reg = hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED, _on_entity_registry_updated
            )
            entry.async_on_unload(unsub_reg)

        # Sync device-registry name to entry.title (handles renames).
        device_reg = dr.async_get(hass)
        device = device_reg.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
        if device is not None and device.name != entry.title:
            device_reg.async_update_device(device.id, name=entry.title)
        # Reload entry on options-save so engine picks up edits + entities rename.
        entry.async_on_unload(entry.add_update_listener(_async_update_listener))
        # Recreate hub if missing — covers user deleting hub while rules still exist.
        await _async_ensure_hub(hass)

    await async_register_services(hass)
    await _async_install_card(hass)
    _LOGGER.debug("async_setup_entry complete: entry_id=%s", entry.entry_id)

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload rule entry when its data changes (rename, mode edits, etc.)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    entry_type = entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_RULE)
    platforms = PLATFORMS_HUB if entry_type == ENTRY_TYPE_HUB else PLATFORMS_RULE
    _LOGGER.debug("Unloading entry %s (type=%s)", entry.entry_id, entry_type)

    unload_ok = await hass.config_entries.async_unload_platforms(entry, platforms)

    if unload_ok and entry_type == ENTRY_TYPE_RULE:
        engines = hass.data.get(DOMAIN, {}).get("engines", {})
        engine = engines.pop(entry.entry_id, None)
        if engine is not None:
            await engine.async_unload()
            _LOGGER.debug("Engine unloaded for %s", entry.entry_id)

    remaining_engines = hass.data.get(DOMAIN, {}).get("engines", {})
    if not remaining_engines:
        remaining_entries = [
            e
            for e in hass.config_entries.async_entries(DOMAIN)
            if e.entry_id != entry.entry_id
        ]
        if not remaining_entries:
            async_unload_services(hass)

    _LOGGER.info("Entry %s unloaded (ok=%s)", entry.entry_id, unload_ok)

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Block hub removal while rules still exist; recreate hub immediately if forced."""
    if entry.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_HUB:
        _LOGGER.debug("Removed rule entry %s; clearing statistics", entry.entry_id)
        async_delete_issue(hass, DOMAIN, missing_flags_issue_id(entry.entry_id))
        ent_reg = er.async_get(hass)
        statistic_ids = [
            entity.entity_id
            for entity in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
            if entity.entity_id.startswith("sensor.")
        ]
        if not statistic_ids:
            _LOGGER.debug("No sensor statistics found for rule %s", entry.entry_id)
            return
        recorder_get_instance(hass).async_clear_statistics(statistic_ids)
        _LOGGER.debug(
            "Cleared %d statistic(s) for rule %s", len(statistic_ids), entry.entry_id
        )
        return
    rule_entries = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.entry_id != entry.entry_id
        and e.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_RULE) == ENTRY_TYPE_RULE
    ]
    if rule_entries:
        _LOGGER.warning(
            "Hub removed while %d rule(s) still configured — recreating hub",
            len(rule_entries),
        )
        await _async_ensure_hub(hass)
    else:
        _LOGGER.info("Hub removed; no rules remain")


async def _async_ensure_hub(hass: HomeAssistant) -> None:
    """Spawn hub entry if missing. Idempotent; safe to call repeatedly."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_HUB:
            _LOGGER.debug("Hub already exists: %s", entry.entry_id)
            return
    _LOGGER.info("Hub missing — spawning import flow to create it")

    async def _create_hub() -> None:
        try:
            await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_IMPORT},
                data={},
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to auto-create hub entry")

    hass.async_create_task(_create_hub())


async def _async_install_card(hass: HomeAssistant) -> None:
    """Serve card JS from component dir and register as Lovelace resource."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(_CARD_INSTALLED_KEY):
        return

    source = Path(__file__).parent / "frontend" / CARD_FILENAME
    if not source.exists():
        _LOGGER.warning("Card JS not found at %s", source)
        return

    version = await hass.async_add_executor_job(_get_version)

    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL, str(source), True)]
        )
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Static path %s already registered", CARD_URL)

    await _async_register_lovelace_resource(hass, version)
    hass.data[DOMAIN][_CARD_INSTALLED_KEY] = True


async def _async_register_lovelace_resource(hass: HomeAssistant, version: str) -> None:
    """Register card as Lovelace resource (best-effort)."""
    resource_url = f"{CARD_URL}?automatically-added&{version}"

    try:
        resources = hass.data["lovelace"].resources
    except (KeyError, AttributeError):
        _LOGGER.info(
            "Could not auto-register Lovelace resource. "
            "Add manually: url: %s?%s, type: module",
            CARD_URL,
            version,
        )
        return

    if not resources.loaded:
        await resources.async_load()
        resources.loaded = True

    existing = [r for r in resources.async_items() if CARD_FILENAME in r.get("url", "")]

    if not existing:
        if getattr(resources, "async_create_item", None):
            await resources.async_create_item(
                {"res_type": "module", "url": resource_url}
            )
            _LOGGER.info("Registered %s as Lovelace resource", resource_url)
        elif getattr(resources, "data", None) and getattr(  # pragma: no cover
            resources.data, "append", None
        ):
            resources.data.append(
                {"type": "module", "url": resource_url}
            )  # pragma: no cover
        return

    for r in existing[1:]:
        if isinstance(resources, ResourceStorageCollection):
            await resources.async_delete_item(r["id"])
            _LOGGER.info("Removed duplicate Lovelace resource %s", r["url"])

    first = existing[0]
    if first.get("url") != resource_url:
        if isinstance(resources, ResourceStorageCollection):
            await resources.async_update_item(
                first["id"], {"res_type": "module", "url": resource_url}
            )
            _LOGGER.info("Updated Lovelace resource to %s", resource_url)
        else:  # pragma: no cover — non-ResourceStorageCollection path not used in real HA
            first["url"] = resource_url
