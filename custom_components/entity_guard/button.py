"""Button platform for Entity Guard."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_RULE

if TYPE_CHECKING:  # pragma: no cover
    from .rule_engine import RuleEngine


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
    """Set up Entity Guard buttons from a config entry."""
    if entry.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_RULE:
        return

    engine: RuleEngine = hass.data[DOMAIN]["engines"][entry.entry_id]

    async_add_entities(
        [
            EntityGuardResetButton(entry, engine),
            EntityGuardTestEnforceButton(entry, engine),
            EntityGuardClearSuppressionButton(entry, engine),
            EntityGuardClearHistoryButton(entry, engine),
        ]
    )


class EntityGuardButtonBase(ButtonEntity):
    """Base class for Entity Guard buttons."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        entry: ConfigEntry,
        engine: RuleEngine,
        translation_key: str,
        suffix: str,
    ) -> None:
        """Initialize the button."""
        self._entry = entry
        self._engine = engine
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{entry.entry_id}_{suffix}"
        self._attr_device_info = _device_info(entry)


class EntityGuardResetButton(EntityGuardButtonBase):
    """Button that clears active cooldowns for the rule."""

    def __init__(self, entry: ConfigEntry, engine: RuleEngine) -> None:
        """Initialize the reset button."""
        super().__init__(entry, engine, "reset", "reset")

    async def async_press(self) -> None:
        """Handle press: clear cooldowns via the engine."""
        await self._engine.async_reset_cooldowns()


class EntityGuardTestEnforceButton(EntityGuardButtonBase):
    """Button that triggers a test enforcement."""

    def __init__(self, entry: ConfigEntry, engine: RuleEngine) -> None:
        """Initialize the test enforce button."""
        super().__init__(entry, engine, "test_enforce", "test_enforce")

    async def async_press(self) -> None:
        """Handle press: call test enforcement on the engine."""
        await self._engine.async_test_enforce()


class EntityGuardClearSuppressionButton(EntityGuardButtonBase):
    """Button that clears active suppression for the rule."""

    def __init__(self, entry: ConfigEntry, engine: RuleEngine) -> None:
        """Initialize the clear suppression button."""
        super().__init__(entry, engine, "clear_suppression", "clear_suppression")

    async def async_press(self) -> None:
        """Handle press: unsuppress the rule via the engine."""
        await self._engine.async_unsuppress()


class EntityGuardClearHistoryButton(EntityGuardButtonBase):
    """Button that resets enforcement counters and history for the rule."""

    def __init__(self, entry: ConfigEntry, engine: RuleEngine) -> None:
        """Initialize the clear history button."""
        super().__init__(entry, engine, "clear_history", "clear_history")

    async def async_press(self) -> None:
        """Handle press: zero counters and clear cooldowns via the engine."""
        await self._engine.async_clear_history()
