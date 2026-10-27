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
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)

from .const import (
    CONF_ATTRIBUTE,
    CONF_ENTRY_TYPE,
    CONF_MODE,
    CONF_SOURCE_ENTITY,
    DOMAIN,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_RULE,
    MODE_DYNAMIC_STATE,
)
from .issue_helpers import async_check_missing_flag_entities, missing_flags_issue_id
from .models import parse_rule_config
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


# Config-entry schema version → async_migrate_entry below. v1 stored the rule
# `mode` as "state"/"attribute"/"follow"; v2 renames to the fixed/dynamic axis:
#   state->fixed_state, attribute->fixed_attribute, follow(+_entity)->dynamic_state.
# The pre-release follow mode's optional attribute add-on is DROPPED on migration
# (dynamic_state is state-only; dynamic_attribute is a separate, fresh-only mode).
# follow_entity + its attribute add-on never shipped, so no production rule carries
# one — only dev/smoke entries, recreated. ConfigFlow.VERSION must match this target.
_MODE_V1_TO_V2 = {
    "state": "fixed_state",
    "attribute": "fixed_attribute",
    "follow": "dynamic_state",
    "follow_entity": "dynamic_state",
}
# Modes whose migration also drops the pre-release attribute add-on.
_DROP_ATTRIBUTE_ON_MIGRATE = frozenset({"follow", "follow_entity"})


def _migrated_no_source_issue_id(entry_id: str) -> str:
    """Repair-issue id for a follow rule migrated to dynamic_state with no source."""
    return f"migrated_follow_no_source_{entry_id}"


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate a config entry to the current schema version.

    v1 → v2: rename the rule `mode` value. Remaps in BOTH entry.data and
    entry.options (the runtime mode is resolved options-over-data, so a live edit
    may sit in either). A pre-release follow(+_entity) rule becomes dynamic_state
    and its CONF_ATTRIBUTE add-on is dropped (dynamic_state is state-only; this
    never shipped, dev/smoke only). Unknown/missing mode values pass through
    unchanged so a hub entry (no mode) or an unexpected value never raises.
    Idempotent: an entry already at the current version is left untouched.
    """
    if entry.version >= 2:
        return True

    def _remap(mapping: dict) -> dict:
        mode = mapping.get(CONF_MODE)
        if mode not in _MODE_V1_TO_V2:
            return dict(mapping)
        out = {**mapping, CONF_MODE: _MODE_V1_TO_V2[mode]}
        if mode in _DROP_ATTRIBUTE_ON_MIGRATE:
            out.pop(CONF_ATTRIBUTE, None)
        return out

    new_data = _remap(dict(entry.data))
    new_options = _remap(dict(entry.options or {}))
    hass.config_entries.async_update_entry(
        entry, data=new_data, options=new_options, version=2
    )
    _LOGGER.info(
        "Migrated entry %s to schema v2 (mode rename): data mode=%s options mode=%s",
        entry.entry_id,
        new_data.get(CONF_MODE),
        new_options.get(CONF_MODE),
    )
    # A pre-release `follow` rule migrates to dynamic_state, which REQUIRES a
    # source_entity. v1 follow rules never stored one under this key, so the
    # migrated rule would be a silent no-op (never enforces, no error). Surface
    # a repair issue so the user recreates it rather than wondering why it is
    # dead. Only the follow→dynamic path can land here lacking a source.
    migrated_mode = new_options.get(CONF_MODE) or new_data.get(CONF_MODE)
    has_source = bool(
        new_options.get(CONF_SOURCE_ENTITY) or new_data.get(CONF_SOURCE_ENTITY)
    )
    if migrated_mode == MODE_DYNAMIC_STATE and not has_source:
        async_create_issue(
            hass,
            DOMAIN,
            _migrated_no_source_issue_id(entry.entry_id),
            is_fixable=False,
            severity=IssueSeverity.WARNING,
            translation_key="migrated_follow_no_source",
            translation_placeholders={"rule": entry.title or entry.entry_id},
        )
    return True


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
    # async_process_integration_platforms skips custom components (dot in name),
    # so register the logbook descriptor directly into logbook's external_events.
    _register_logbook_descriptors(hass)
    _LOGGER.info("Entity Guard integration loaded")
    return True


@callback
def _register_logbook_descriptors(hass: HomeAssistant) -> None:
    """Register Entity Guard logbook event descriptors.

    HA's integration platform mechanism skips custom components, so we wire
    directly into logbook's external_events dict after logbook is set up.
    """
    from homeassistant.components.logbook.const import DOMAIN as LOGBOOK_DOMAIN

    from .logbook import async_describe_events

    logbook_config = hass.data.get(LOGBOOK_DOMAIN)
    if logbook_config is None:
        _LOGGER.warning(
            "Logbook not loaded — Entity Guard events won't appear in logbook"
        )
        return
    external_events = logbook_config.external_events
    registered_keys: list[str] = []

    @callback
    def _async_describe_event(domain: str, event_name: str, describe_callback) -> None:
        external_events[event_name] = (domain, describe_callback)
        registered_keys.append(event_name)

    async_describe_events(hass, _async_describe_event)
    _LOGGER.debug("Registered Entity Guard logbook descriptors: %s", registered_keys)


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
        engine = RuleEngine(hass, config, store, _master_enabled_getter(hass), entry)
        try:
            await engine.async_setup()
        except Exception:
            _LOGGER.exception(
                "Engine setup failed for rule entry %s; entry will not load",
                entry.entry_id,
            )
            return False
        # Per-rule enabled state is restored from the Store inside engine.async_setup
        # (blob_to_runtime). It is the single owner — the switch and panic_stop both
        # persist through the engine/Store, so no second restore from options is needed.
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
                    except Exception:
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

        # Reload entry on options-save so engine picks up edits + entities rename.
        entry.async_on_unload(entry.add_update_listener(_async_update_listener))
        # Recreate hub if missing — covers user deleting hub while rules still exist.
        await _async_ensure_hub(hass)

    await async_register_services(hass)
    await _async_install_card(hass)
    _LOGGER.debug("async_setup_entry complete: entry_id=%s", entry.entry_id)

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload rule entry when its data changes (rename, mode edits, etc.).

    Slider (number.*) writes persist to options but apply live already, so they
    mark the entry to skip the reload — reloading would needlessly rebuild every
    platform, possibly tearing down a rule mid-enforcement.

    Safe only because HA fires update listeners eagerly/inline: async_update_entry
    schedules each listener via hass.async_create_task with eager_start=True, which
    runs the coroutine synchronously to its first await. The skip-check + discard
    below sit before any await, so a flush's mark is consumed by that flush's own
    listener within the same async_update_entry call — a competing update on the
    same entry_id can't run in between and inherit the mark. If HA ever switches to
    non-eager listener dispatch, that guarantee breaks and the mark needs a
    per-update token instead of a bare entry_id set.
    """
    from .number import SKIP_RELOAD_KEY

    skip = hass.data.get(DOMAIN, {}).get(SKIP_RELOAD_KEY)
    if skip is not None and entry.entry_id in skip:
        skip.discard(entry.entry_id)
        return
    # If the user fixed a migrated-without-source rule by adding a source entity,
    # clear the repair issue raised at migration (it is never otherwise deleted
    # until the rule is removed).
    if entry.options.get(CONF_SOURCE_ENTITY) or entry.data.get(CONF_SOURCE_ENTITY):
        async_delete_issue(hass, DOMAIN, _migrated_no_source_issue_id(entry.entry_id))
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    entry_type = entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_RULE)
    platforms = PLATFORMS_HUB if entry_type == ENTRY_TYPE_HUB else PLATFORMS_RULE
    _LOGGER.debug("Unloading entry %s (type=%s)", entry.entry_id, entry_type)

    unload_ok = await hass.config_entries.async_unload_platforms(entry, platforms)

    if entry_type == ENTRY_TYPE_RULE:
        engines = hass.data.get(DOMAIN, {}).get("engines", {})
        engine = engines.pop(entry.entry_id, None)
        if engine is not None:
            await engine.async_unload()
            _LOGGER.debug("Engine unloaded for %s", entry.entry_id)

    remaining_engines = hass.data.get(DOMAIN, {}).get("engines", {})
    if not remaining_engines:
        remaining_rule_entries = [
            e
            for e in hass.config_entries.async_entries(DOMAIN)
            if e.entry_id != entry.entry_id
            and e.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_RULE) == ENTRY_TYPE_RULE
        ]
        if not remaining_rule_entries:
            async_unload_services(hass)

    _LOGGER.info("Entry %s unloaded (ok=%s)", entry.entry_id, unload_ok)

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Block hub removal while rules still exist; recreate hub immediately if forced."""
    if entry.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_HUB:
        _LOGGER.debug("Removed rule entry %s; clearing statistics", entry.entry_id)
        async_delete_issue(hass, DOMAIN, missing_flags_issue_id(entry.entry_id))
        async_delete_issue(hass, DOMAIN, _migrated_no_source_issue_id(entry.entry_id))
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
        except Exception:
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
    except (RuntimeError, ValueError) as err:
        # A duplicate registration (card already served, e.g. after reload) is
        # expected and benign; anything else (bad path, http not ready) would
        # silently 404 the card, so surface it rather than swallow at DEBUG.
        if "already" in str(err).lower():
            _LOGGER.debug("Static path %s already registered", CARD_URL)
        else:
            _LOGGER.warning("Failed to register card static path %s: %s", CARD_URL, err)

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
