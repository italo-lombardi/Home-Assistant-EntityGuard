"""Sensor platform for Entity Guard."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ENTRY_TYPE,
    DOMAIN,
    ENTRY_TYPE_RULE,
    STATUS_VALUES,
    entry_has_safety_target,
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
    """Set up Entity Guard sensors from a config entry."""
    if entry.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_RULE:
        return

    engine: RuleEngine = hass.data[DOMAIN]["engines"][entry.entry_id]

    sensors: list[SensorEntity] = [
        EntityGuardStatusSensor(entry, engine),
        EntityGuardLastEnforcedSensor(entry, engine),
        EntityGuardEnforcementCountTodaySensor(entry, engine),
        EntityGuardEnforcementCountTotalSensor(entry, engine),
        EntityGuardCooldownRemainingSensor(entry, engine),
        EntityGuardSuppressedUntilSensor(entry, engine),
        EntityGuardRuleIdSensor(entry, engine),
    ]

    if entry_has_safety_target(entry):
        sensors.append(EntityGuardSafetyStatusSensor(entry, engine))

    async_add_entities(sensors)


class EntityGuardSensor(SensorEntity):
    """Base class for Entity Guard sensors."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
        self,
        entry: ConfigEntry,
        engine: RuleEngine,
        translation_key: str,
        suffix: str,
    ) -> None:
        """Initialize the sensor."""
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


class EntityGuardStatusSensor(EntityGuardSensor):
    """Sensor exposing current rule status."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUS_VALUES

    def __init__(self, entry: ConfigEntry, engine: RuleEngine) -> None:
        """Initialize the status sensor."""
        super().__init__(entry, engine, "status", "status")

    @property
    def native_value(self) -> str:
        """Return current status string."""
        return self._engine.current_status()

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Return suppression metadata, target entities, and flag conditions."""
        flags = []
        for flag in self._engine.config.flags or []:
            state_obj = self.hass.states.get(flag.entity) if self.hass else None
            current = state_obj.state if state_obj is not None else None
            flags.append(
                {
                    "entity": flag.entity,
                    "required": flag.match_state,
                    "current": current,
                    "matches": current == flag.match_state,
                }
            )
        return {
            "suppression_reason": self._engine.state.suppression_reason,
            "target_entities": list(self._engine.config.target_entities or []),
            "target_state": self._engine.config.target_state,
            "trigger_states": list(self._engine.config.trigger_states or []),
            "consecutive_errors": int(self._engine.state.consecutive_errors or 0),
            "last_error": self._engine.state.last_error,
            "flags": flags,
        }


class EntityGuardLastEnforcedSensor(EntityGuardSensor):
    """Sensor exposing the last enforcement timestamp."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry: ConfigEntry, engine: RuleEngine) -> None:
        """Initialize the last enforced sensor."""
        super().__init__(entry, engine, "last_enforced", "last_enforced")

    @property
    def native_value(self) -> datetime | None:
        """Return last enforcement timestamp."""
        return self._engine.state.last_enforced


class EntityGuardEnforcementCountTodaySensor(EntityGuardSensor):
    """Sensor exposing today's enforcement count."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ConfigEntry, engine: RuleEngine) -> None:
        """Initialize the today counter sensor."""
        super().__init__(
            entry, engine, "enforcement_count_today", "enforcement_count_today"
        )

    @property
    def native_value(self) -> int:
        """Return today's enforcement count."""
        return int(self._engine.state.enforcement_count_today or 0)


class EntityGuardEnforcementCountTotalSensor(EntityGuardSensor):
    """Sensor exposing total enforcement count."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, engine: RuleEngine) -> None:
        """Initialize the total counter sensor."""
        super().__init__(
            entry, engine, "enforcement_count_total", "enforcement_count_total"
        )

    @property
    def native_value(self) -> int:
        """Return total enforcement count."""
        return int(self._engine.state.enforcement_count_total or 0)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Return counter start metadata.

        `counter_since` is when the current total-counter window opened —
        either rule creation (first setup) or the last Clear History reset.
        `counter_days` is a convenience derived value for card templates.
        """
        since = self._engine.state.counter_total_since
        if since is None:
            return {"counter_since": None, "counter_days": None}
        # Defensive: if a persisted value came back naive (e.g. externally
        # edited blob), coerce to UTC so subtraction with dt_util.now() never
        # raises. Normal writes always emit tz-aware isoformat strings.
        if since.tzinfo is None:
            since = since.replace(tzinfo=dt_util.UTC)
        return {
            "counter_since": since.isoformat(),
            "counter_days": max(0, (dt_util.now() - since).days),
        }


class EntityGuardCooldownRemainingSensor(EntityGuardSensor):
    """Sensor exposing remaining cooldown time in seconds."""

    _attr_native_unit_of_measurement = "s"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, engine: RuleEngine) -> None:
        """Initialize the cooldown remaining sensor."""
        super().__init__(entry, engine, "cooldown_remaining", "cooldown_remaining")

    @property
    def native_value(self) -> int:
        """Return remaining cooldown seconds."""
        return int(self._engine.cooldown_remaining_seconds() or 0)


class EntityGuardSafetyStatusSensor(EntityGuardSensor):
    """Sensor reporting safety acknowledgement state."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["acknowledged", "not_acknowledged"]
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, engine: RuleEngine) -> None:
        """Initialize the safety status sensor."""
        super().__init__(entry, engine, "safety_status", "safety_status")

    @property
    def native_value(self) -> str:
        """Return acknowledgement state."""
        config = getattr(self._engine, "config", None)
        acknowledged = bool(getattr(config, "safety_acknowledged", False))
        return "acknowledged" if acknowledged else "not_acknowledged"


class EntityGuardSuppressedUntilSensor(EntityGuardSensor):
    """Sensor exposing the suppression end timestamp."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, engine: RuleEngine) -> None:
        """Initialize the suppressed until sensor."""
        super().__init__(entry, engine, "suppressed_until", "suppressed_until")

    @property
    def native_value(self) -> datetime | None:
        """Return suppression end timestamp."""
        return self._engine.state.suppressed_until

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Return suppression metadata."""
        return {"reason": self._engine.state.suppression_reason}


class EntityGuardRuleIdSensor(EntityGuardSensor):
    """Diagnostic sensor exposing the rule's config entry ID."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, entry: ConfigEntry, engine: RuleEngine) -> None:
        """Initialize the rule ID sensor."""
        super().__init__(entry, engine, "rule_id", "rule_id")

    @property
    def native_value(self) -> str:
        """Return the config entry ID (stable rule identifier)."""
        return self._entry.entry_id
