"""Services for Entity Guard."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    CONF_DURATION_MINUTES,
    CONF_ENTRY_TYPE,
    CONF_RULE_ID,
    DOMAIN,
    ENTRY_TYPE_HUB,
    SERVICE_CLEAR_HISTORY,
    SERVICE_LIST_RULES,
    SERVICE_PANIC_STOP,
    SERVICE_SUPPRESS,
    SERVICE_UNSUPPRESS,
)
from .rule_engine import signal_master_update

_LOGGER = logging.getLogger(__name__)

PANIC_STOP_DURATION_MINUTES = 60

SUPPRESS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_RULE_ID): cv.string,
        vol.Required(CONF_DURATION_MINUTES): vol.All(
            cv.positive_int, vol.Range(min=1, max=1440)
        ),
    }
)

UNSUPPRESS_SCHEMA = vol.Schema({vol.Required(CONF_RULE_ID): cv.string})

CLEAR_HISTORY_SCHEMA = vol.Schema({vol.Required(CONF_RULE_ID): cv.string})

LIST_RULES_SCHEMA = vol.Schema({})

PANIC_STOP_SCHEMA = vol.Schema({})


def _iter_engines(hass: HomeAssistant) -> list[Any]:
    """Return all rule engines."""
    return list(hass.data.get(DOMAIN, {}).get("engines", {}).values())


def _resolve_engine(hass: HomeAssistant, rule_id: str) -> Any:
    """Resolve an engine by unique_id or rule name."""
    for engine in _iter_engines(hass):
        config = engine.config
        if config.unique_id == rule_id or config.name == rule_id:
            return engine
    raise ServiceValidationError(
        f"No Entity Guard rule found matching '{rule_id}'",
        translation_domain=DOMAIN,
        translation_key="rule_not_found",
        translation_placeholders={"rule_id": rule_id},
    )


async def async_register_services(hass: HomeAssistant) -> None:
    """Register Entity Guard services."""

    async def handle_suppress(call: ServiceCall) -> None:
        rule_id: str = call.data[CONF_RULE_ID]
        duration_minutes: int = call.data[CONF_DURATION_MINUTES]
        engine = _resolve_engine(hass, rule_id)
        try:
            await engine.async_suppress(
                duration_minutes=duration_minutes,
                user_id=call.context.user_id,
            )
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(
                f"Failed to suppress rule '{rule_id}': {err}"
            ) from err
        _LOGGER.info(
            "Suppressed rule %s for %d minute(s)",
            engine.config.name,
            duration_minutes,
        )

    async def handle_unsuppress(call: ServiceCall) -> None:
        rule_id: str = call.data[CONF_RULE_ID]
        engine = _resolve_engine(hass, rule_id)
        try:
            await engine.async_unsuppress()
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(
                f"Failed to unsuppress rule '{rule_id}': {err}"
            ) from err
        _LOGGER.info("Unsuppressed rule %s", engine.config.name)

    async def handle_clear_history(call: ServiceCall) -> None:
        rule_id: str = call.data[CONF_RULE_ID]
        engine = _resolve_engine(hass, rule_id)
        try:
            await engine.async_clear_history()
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(
                f"Failed to clear history for rule '{rule_id}': {err}"
            ) from err
        _LOGGER.info("Cleared history for rule %s", engine.config.name)

    async def handle_list_rules(call: ServiceCall) -> ServiceResponse:
        rules: list[dict[str, Any]] = []
        for engine in _iter_engines(hass):
            config = engine.config
            rules.append(
                {
                    "rule_id": getattr(config, "unique_id", None),
                    "name": getattr(config, "name", None),
                    "target_entities": list(
                        getattr(config, "target_entities", []) or []
                    ),
                    "mode": getattr(config, "mode", None),
                    "status": engine.current_status(),
                    "enabled": engine.state.enabled,
                    "suppressed_until": (
                        engine.state.suppressed_until.isoformat()
                        if engine.state.suppressed_until is not None
                        else None
                    ),
                }
            )
        return {"rules": rules}

    async def handle_panic_stop(call: ServiceCall) -> None:
        engines = _iter_engines(hass)
        for engine in engines:
            try:
                engine.set_enabled(False)
                await engine.async_reset_cooldowns()
                await engine.async_suppress(
                    duration_minutes=PANIC_STOP_DURATION_MINUTES,
                    user_id=call.context.user_id,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Panic stop partial failure on rule '%s': %s",
                    getattr(engine.config, "name", "?"),
                    err,
                )
        # Persist per-rule disabled state to config entry options.
        for entry_id, eng in list(hass.data.get(DOMAIN, {}).get("engines", {}).items()):
            rule_entry = hass.config_entries.async_get_entry(entry_id)
            if rule_entry is not None:
                hass.config_entries.async_update_entry(
                    rule_entry, options={**rule_entry.options, "enabled": False}
                )

        hass.data.setdefault(DOMAIN, {})["hub_master_enabled"] = False
        for _hub_entry in hass.config_entries.async_entries(DOMAIN):
            if _hub_entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_HUB:
                hass.config_entries.async_update_entry(
                    _hub_entry, options={**_hub_entry.options, "master_enabled": False}
                )
                break
        async_dispatcher_send(hass, signal_master_update())
        _LOGGER.warning(
            "Entity Guard panic stop: disabled %d rule(s) and suppressed for %d min",
            len(engines),
            PANIC_STOP_DURATION_MINUTES,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SUPPRESS):
        hass.services.async_register(
            DOMAIN, SERVICE_SUPPRESS, handle_suppress, schema=SUPPRESS_SCHEMA
        )
    if not hass.services.has_service(DOMAIN, SERVICE_UNSUPPRESS):
        hass.services.async_register(
            DOMAIN, SERVICE_UNSUPPRESS, handle_unsuppress, schema=UNSUPPRESS_SCHEMA
        )
    if not hass.services.has_service(DOMAIN, SERVICE_CLEAR_HISTORY):
        hass.services.async_register(
            DOMAIN,
            SERVICE_CLEAR_HISTORY,
            handle_clear_history,
            schema=CLEAR_HISTORY_SCHEMA,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_LIST_RULES):
        hass.services.async_register(
            DOMAIN,
            SERVICE_LIST_RULES,
            handle_list_rules,
            schema=LIST_RULES_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_PANIC_STOP):
        hass.services.async_register(
            DOMAIN, SERVICE_PANIC_STOP, handle_panic_stop, schema=PANIC_STOP_SCHEMA
        )


@callback
def async_unload_services(hass: HomeAssistant) -> None:
    """Remove Entity Guard services."""
    for service in (
        SERVICE_SUPPRESS,
        SERVICE_UNSUPPRESS,
        SERVICE_CLEAR_HISTORY,
        SERVICE_LIST_RULES,
        SERVICE_PANIC_STOP,
    ):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
