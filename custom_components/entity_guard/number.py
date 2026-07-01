"""Number platform for Entity Guard."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_DEBOUNCE_SECONDS,
    CONF_DELAY_SECONDS,
    CONF_ENTRY_TYPE,
    CONF_MAX_ENFORCEMENTS_PER_MINUTE,
    DEFAULT_DEBOUNCE_SECONDS,
    DEFAULT_DELAY_SECONDS,
    DEFAULT_MAX_ENFORCEMENTS_PER_MINUTE,
    DOMAIN,
    ENTRY_TYPE_RULE,
    MAX_DEBOUNCE_SECONDS,
    MAX_DELAY_SECONDS,
    MAX_RATE_LIMIT,
    MIN_DEBOUNCE_SECONDS,
    MIN_DELAY_SECONDS,
    MIN_RATE_LIMIT,
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
    """Set up Entity Guard number entities from a config entry."""
    if entry.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_RULE:
        return

    engine: RuleEngine = hass.data[DOMAIN]["engines"][entry.entry_id]

    async_add_entities(
        [
            EntityGuardDelaySecondsNumber(entry, engine),
            EntityGuardDebounceSecondsNumber(entry, engine),
            EntityGuardMaxEnforcementsNumber(entry, engine),
        ]
    )


class EntityGuardNumberBase(NumberEntity):
    """Base class for Entity Guard configurable numbers."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_step = 1
    _attr_available = False

    def __init__(
        self,
        entry: ConfigEntry,
        engine: RuleEngine,
        translation_key: str,
        suffix: str,
        config_key: str,
        default: float,
        min_value: float,
        max_value: float,
        unit: str,
    ) -> None:
        """Initialize the number entity."""
        self._entry = entry
        self._engine = engine
        self._config_key = config_key
        self._default = default
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{entry.entry_id}_{suffix}"
        self._attr_device_info = _device_info(entry)
        self._attr_native_min_value = min_value
        self._attr_native_max_value = max_value
        self._attr_native_unit_of_measurement = unit

    async def async_added_to_hass(self) -> None:
        """Subscribe to dispatcher updates."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_rule_update(self._engine.config.unique_id),
                self._handle_update,
            )
        )
        self._attr_available = True
        self.async_write_ha_state()

    @callback
    def _handle_update(self, *args: object) -> None:
        """Handle a dispatcher update."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> float:
        """Return the current value from engine config."""
        config = getattr(self._engine, "config", None)
        value = getattr(config, self._config_key, None)
        if value is None:
            value = self._entry.data.get(self._config_key, self._default)
        return float(value)

    async def async_set_native_value(self, value: float) -> None:
        """Apply a new value to the running engine without persisting to config entry.

        Writes are applied live via setattr so they take effect immediately. The
        canonical value lives in entry.data (set via the options flow); writing to
        data/options here would trigger _async_update_listener → full entry reload,
        tearing down and rebuilding all five platforms for a trivial slider change.
        """
        coerced = int(value)

        config = getattr(self._engine, "config", None)
        if config is not None:  # pragma: no branch
            try:
                setattr(config, self._config_key, coerced)
            except Exception:  # pragma: no cover - defensive
                _LOGGER.debug("Failed to update engine config %s", self._config_key)

        async_dispatcher_send(
            self.hass, signal_rule_update(self._engine.config.unique_id)
        )


class EntityGuardDelaySecondsNumber(EntityGuardNumberBase):
    """Configurable enforcement delay in seconds."""

    def __init__(self, entry: ConfigEntry, engine: RuleEngine) -> None:
        """Initialize the delay seconds number."""
        super().__init__(
            entry,
            engine,
            translation_key="delay_seconds",
            suffix="delay_seconds",
            config_key=CONF_DELAY_SECONDS,
            default=DEFAULT_DELAY_SECONDS,
            min_value=MIN_DELAY_SECONDS,
            max_value=MAX_DELAY_SECONDS,
            unit="s",
        )


class EntityGuardDebounceSecondsNumber(EntityGuardNumberBase):
    """Configurable debounce window in seconds."""

    def __init__(self, entry: ConfigEntry, engine: RuleEngine) -> None:
        """Initialize the debounce seconds number."""
        super().__init__(
            entry,
            engine,
            translation_key="debounce_seconds",
            suffix="debounce_seconds",
            config_key=CONF_DEBOUNCE_SECONDS,
            default=DEFAULT_DEBOUNCE_SECONDS,
            min_value=MIN_DEBOUNCE_SECONDS,
            max_value=MAX_DEBOUNCE_SECONDS,
            unit="s",
        )


class EntityGuardMaxEnforcementsNumber(EntityGuardNumberBase):
    """Configurable max enforcements per minute."""

    def __init__(self, entry: ConfigEntry, engine: RuleEngine) -> None:
        """Initialize the rate-limit number."""
        super().__init__(
            entry,
            engine,
            translation_key="max_enforcements_per_minute",
            suffix="max_enforcements_per_minute",
            config_key=CONF_MAX_ENFORCEMENTS_PER_MINUTE,
            default=DEFAULT_MAX_ENFORCEMENTS_PER_MINUTE,
            min_value=MIN_RATE_LIMIT,
            max_value=MAX_RATE_LIMIT,
            unit="/min",
        )
