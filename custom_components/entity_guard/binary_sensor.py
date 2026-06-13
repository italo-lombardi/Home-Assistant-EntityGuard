"""Binary sensor platform for Entity Guard."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_ENTRY_TYPE,
    DOMAIN,
    ENTRY_TYPE_RULE,
    signal_master,
    signal_rule_update,
)

if TYPE_CHECKING:  # pragma: no cover
    from .rule_engine import RuleEngine

_LOGGER = logging.getLogger(__name__)


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    """Return device info for a rule entry."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Entity Guard",
        model="Rule",
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Entity Guard binary sensors from a config entry."""
    if entry.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_RULE:
        return

    engine: RuleEngine = hass.data[DOMAIN]["engines"][entry.entry_id]

    async_add_entities(
        [
            EntityGuardArmedSensor(entry, engine),
            EntityGuardActiveSensor(entry, engine),
            EntityGuardInCooldownSensor(entry, engine),
        ]
    )


class EntityGuardBinarySensor(BinarySensorEntity):
    """Base class for Entity Guard binary sensors."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
        self,
        entry: ConfigEntry,
        engine: RuleEngine,
        translation_key: str,
        suffix: str,
    ) -> None:
        """Initialize the binary sensor."""
        self._entry = entry
        self._engine = engine
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{entry.entry_id}_{suffix}"
        self._attr_device_info = _device_info(entry)

    async def async_added_to_hass(self) -> None:
        """Subscribe to dispatcher updates."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_rule_update(self._engine.config.unique_id),
                self._handle_update,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal_master(), self._handle_update)
        )
        self.async_write_ha_state()

    @callback
    def _handle_update(self, *args: object) -> None:
        """Handle a dispatcher update."""
        self.async_write_ha_state()


class EntityGuardArmedSensor(EntityGuardBinarySensor):
    """Binary sensor indicating the rule is armed and watching."""

    def __init__(self, entry: ConfigEntry, engine: RuleEngine) -> None:
        """Initialize the armed sensor."""
        super().__init__(entry, engine, "armed", "armed")

    @property
    def is_on(self) -> bool:
        """Return True if the rule is armed."""
        return bool(self._engine.is_armed())


class EntityGuardActiveSensor(EntityGuardBinarySensor):
    """Binary sensor indicating the rule is currently enforcing."""

    def __init__(self, entry: ConfigEntry, engine: RuleEngine) -> None:
        """Initialize the active sensor."""
        super().__init__(entry, engine, "active", "active")

    @property
    def is_on(self) -> bool:
        """Return True if enforcement is in flight."""
        return bool(self._engine.is_active())


class EntityGuardInCooldownSensor(EntityGuardBinarySensor):
    """Binary sensor indicating the rule is in post-enforcement cooldown."""

    def __init__(self, entry: ConfigEntry, engine: RuleEngine) -> None:
        """Initialize the cooldown sensor."""
        super().__init__(entry, engine, "in_cooldown", "in_cooldown")

    @property
    def is_on(self) -> bool:
        """Return True if any bound entity is in cooldown."""
        return bool(self._engine.is_in_cooldown())
