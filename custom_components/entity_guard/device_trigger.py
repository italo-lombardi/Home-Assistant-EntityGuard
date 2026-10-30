"""Device-automation triggers for Entity Guard.

Exposes each rule's events — enforced / skipped / loop-detected / verified /
unreachable / cancelled / suppressed — as visual device triggers in the Home Assistant
automation editor, so a user can build "When [rule] enforced → notify me"
without hand-typing an `event` trigger.

Wiring nuance: a rule's DEVICE identifier is (DOMAIN, entry_id), but the EVENTS
carry `rule_id = config.unique_id` (a separate uuid4, not entry_id). So a
trigger resolves device_id → the (DOMAIN, entry_id) identifier → the config
entry → entry.unique_id, and filters the event on that `rule_id`.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.components.device_automation import (
    DEVICE_TRIGGER_BASE_SCHEMA,
    DeviceNotFound,
)
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_EVENT,
    CONF_PLATFORM,
    CONF_TYPE,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import (
    DOMAIN,
    EVENT_CANCELLED,
    EVENT_ENFORCED,
    EVENT_LOOP_DETECTED,
    EVENT_SKIPPED,
    EVENT_SUPPRESSED,
    EVENT_UNREACHABLE,
    EVENT_VERIFIED,
)

# Trigger type (shown in the UI, keyed in strings.json device_automation.trigger_type)
# → the event it fires on.
TRIGGER_TYPE_EVENT = {
    "enforced": EVENT_ENFORCED,
    "skipped": EVENT_SKIPPED,
    "loop_detected": EVENT_LOOP_DETECTED,
    "verified": EVENT_VERIFIED,
    "unreachable": EVENT_UNREACHABLE,
    "cancelled": EVENT_CANCELLED,
    "suppressed": EVENT_SUPPRESSED,
}
TRIGGER_TYPES = set(TRIGGER_TYPE_EVENT)

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_DOMAIN): DOMAIN,
        vol.Required(CONF_DEVICE_ID): str,
        vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES),
    }
)


def _rule_id_for_device(hass: HomeAssistant, device_id: str) -> str | None:
    """Resolve a device_id to the rule's event `rule_id` (its config-entry
    unique_id), or None if the device isn't an Entity Guard rule (e.g. the hub).

    The device identifier is (DOMAIN, entry_id); the events filter on
    entry.unique_id, which differs from entry_id — hence the two-step lookup.
    """
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    for domain, ident in device.identifiers:
        if domain != DOMAIN:
            continue
        entry = hass.config_entries.async_get_entry(ident)
        # The hub device's identifier is (DOMAIN, "hub") — not a real entry id, so
        # async_get_entry returns None and the hub offers no rule triggers.
        if entry is None:
            return None
        return entry.unique_id
    return None


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, str]]:
    """List the device triggers for an Entity Guard rule device."""
    # Only rule devices offer triggers; the hub (and an unknown device) offer none.
    if _rule_id_for_device(hass, device_id) is None:
        return []
    return [
        {
            CONF_PLATFORM: "device",
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: device_id,
            CONF_TYPE: trigger_type,
        }
        for trigger_type in TRIGGER_TYPES
    ]


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach a device trigger: translate it to an event trigger filtered to this
    rule's events."""
    rule_id = _rule_id_for_device(hass, config[CONF_DEVICE_ID])
    if rule_id is None:
        raise DeviceNotFound(
            f"Device ID {config[CONF_DEVICE_ID]} is not an Entity Guard rule"
        )
    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            event_trigger.CONF_PLATFORM: CONF_EVENT,
            event_trigger.CONF_EVENT_TYPE: TRIGGER_TYPE_EVENT[config[CONF_TYPE]],
            event_trigger.CONF_EVENT_DATA: {"rule_id": rule_id},
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )
