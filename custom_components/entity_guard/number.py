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
from homeassistant.helpers.event import async_call_later

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
    signal_rule_update,
)

if TYPE_CHECKING:  # pragma: no cover
    from .rule_engine import RuleEngine

_LOGGER = logging.getLogger(__name__)

# Slider writes to entry.options are debounced and coalesced per config entry so a
# drag (many rapid set_value calls) collapses to one async_update_entry, and two
# different sliders moved within the window persist together.
_NUMBER_WRITE_DEBOUNCE_SECONDS = 2
_PENDING_WRITES_KEY = "pending_number_writes"


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
        self.async_on_remove(self._cancel_pending_write)
        self._attr_available = True
        self.async_write_ha_state()

    @callback
    def _cancel_pending_write(self) -> None:
        """Cancel a pending debounced options write for this entry (on unload).

        Prevents a late flush from calling async_update_entry on a torn-down entry.
        """
        pending = self.hass.data.get(DOMAIN, {}).get(_PENDING_WRITES_KEY, {})
        state = pending.pop(self._entry.entry_id, None)
        if state is not None and state["cancel"] is not None:
            state["cancel"]()

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
        """Apply a new value live and persist it to the config entry options.

        The value is applied to the running engine immediately via setattr (so the
        change takes effect and the UI reflects it at once) and, on a short debounce,
        written to entry.options via async_update_entry. Persisting to options — which
        parse_rule_config merges over data — is what makes the value survive a reload
        or restart. Writing on every keystroke would reload the entry repeatedly; the
        debounce coalesces a slider drag (and concurrent sliders) into one write.
        """
        coerced = max(0, int(value))

        config = getattr(self._engine, "config", None)
        if config is not None:  # pragma: no branch
            try:
                setattr(config, self._config_key, coerced)
            except Exception:  # pragma: no cover - defensive
                _LOGGER.debug("Failed to update engine config %s", self._config_key)

        async_dispatcher_send(
            self.hass, signal_rule_update(self._engine.config.unique_id)
        )
        self._schedule_options_write(self._config_key, coerced)

    def _schedule_options_write(self, key: str, value: int) -> None:
        """Queue a coalesced, debounced write of `key`=`value` to entry.options."""
        pending = self.hass.data.setdefault(DOMAIN, {}).setdefault(
            _PENDING_WRITES_KEY, {}
        )
        state = pending.setdefault(self._entry.entry_id, {"values": {}, "cancel": None})
        state["values"][key] = value
        if state["cancel"] is not None:
            state["cancel"]()

        entry = self._entry

        @callback
        def _flush(_now: object) -> None:
            entry_pending = self.hass.data.get(DOMAIN, {}).get(_PENDING_WRITES_KEY, {})
            queued = entry_pending.pop(entry.entry_id, None)
            if not queued or not queued["values"]:
                return
            # Read options fresh at flush time so concurrent slider writes don't
            # clobber each other with a stale snapshot captured at call time.
            self.hass.config_entries.async_update_entry(
                entry, options={**(entry.options or {}), **queued["values"]}
            )

        state["cancel"] = async_call_later(
            self.hass, _NUMBER_WRITE_DEBOUNCE_SECONDS, _flush
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
            # 0 disables rate limiting (matches the options flow). The engine treats
            # 0 as "no limit"; MIN_RATE_LIMIT applies only to the flow's own field.
            min_value=0,
            max_value=MAX_RATE_LIMIT,
            unit="/min",
        )
