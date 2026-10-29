"""Binary sensor platform for Entity Guard."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_ENTRY_TYPE,
    DOMAIN,
    ENTRY_TYPE_RULE,
    MODE_FIXED_STATE,
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

    entities: list[EntityGuardBinarySensor] = [
        EntityGuardArmedSensor(entry, engine),
        EntityGuardActiveSensor(entry, engine),
        EntityGuardInCooldownSensor(entry, engine),
        EntityGuardPendingSensor(entry, engine),
        EntityGuardRecentlyEnforcedSensor(entry, engine),
    ]

    # The verify-confirmed twin exists only while verify is enabled. On verify
    # ON->OFF we DISABLE its registry entry (not remove it) so an automation
    # listening to it sees an orderly "entity disabled" instead of a silently
    # dead trigger; OFF->ON re-enables. Reconcile here, on every (re)setup —
    # the options flow edits verify_enabled and reloads the entry, re-running us.
    # The unreachable failure twin shares the same verify-only lifecycle.
    ent_reg = er.async_get(hass)
    for uid_suffix, sensor_cls in (
        ("recently_enforced_verified", EntityGuardRecentlyEnforcedVerifiedSensor),
        ("recently_enforced_unreachable", EntityGuardRecentlyEnforcedUnreachableSensor),
    ):
        uid = f"{entry.entry_id}_{uid_suffix}"
        existing = ent_reg.async_get_entity_id("binary_sensor", DOMAIN, uid)
        if engine.config.verify_enabled:
            if existing is not None and ent_reg.async_get(existing).disabled_by is (
                er.RegistryEntryDisabler.INTEGRATION
            ):
                # Was disabled by a prior verify-off; re-enable before (re-)adding.
                ent_reg.async_update_entity(existing, disabled_by=None)
            entities.append(sensor_cls(entry, engine))
        elif existing is not None:
            # Verify is off but the entity was created in a prior verify-on run:
            # keep the registry row and disable it (reversible), do not add it live.
            if ent_reg.async_get(existing).disabled_by is None:
                ent_reg.async_update_entity(
                    existing, disabled_by=er.RegistryEntryDisabler.INTEGRATION
                )
        # verify off + never created -> add nothing (created only on verify-on).

    async_add_entities(entities)


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


class EntityGuardPendingSensor(EntityGuardBinarySensor):
    """Binary sensor indicating a delayed enforcement is pending."""

    def __init__(self, entry: ConfigEntry, engine: RuleEngine) -> None:
        """Initialize the pending sensor."""
        super().__init__(entry, engine, "pending", "pending")

    @property
    def is_on(self) -> bool:
        """Return True if a delayed enforcement is queued."""
        return bool(self._engine.is_pending())


class EntityGuardRecentlyEnforcedBase(EntityGuardBinarySensor):
    """Base for the recently-enforced pulse sensors.

    Exposes rule context (name, targets, desired state, delay) as attributes so
    automations can build notifications from any of the three pulse sensors —
    recently_enforced, recently_enforced_verified, recently_enforced_unreachable.
    """

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Expose rule context for use in automation templates."""
        cfg = self._engine.config
        entities = list(cfg.target_entities or [])
        names = []
        if self.hass:
            for eid in entities:
                state = self.hass.states.get(eid)
                names.append(
                    state.attributes.get("friendly_name", eid) if state else eid
                )
        else:
            names = list(entities)
        return {
            "rule_name": self._entry.title,
            "target_entities": entities,
            "target_entity_names": names,
            "target": cfg.target_state
            if cfg.mode == MODE_FIXED_STATE
            else cfg.target_value,
            "delay_seconds": cfg.delay_seconds,
        }


class EntityGuardRecentlyEnforcedSensor(EntityGuardRecentlyEnforcedBase):
    """Binary sensor that stays ON for 30 seconds after any enforcement."""

    def __init__(self, entry: ConfigEntry, engine: RuleEngine) -> None:
        """Initialize the recently enforced sensor."""
        super().__init__(entry, engine, "recently_enforced", "recently_enforced")

    @property
    def is_on(self) -> bool:
        """Return True if enforcement fired within the last 30 seconds."""
        return bool(self._engine.is_recently_enforced())


class EntityGuardRecentlyEnforcedVerifiedSensor(EntityGuardRecentlyEnforcedBase):
    """ON for 30 seconds after a verify-CONFIRMED enforcement.

    Created only while verify is enabled (see async_setup_entry). The clean
    automation gate: unlike recently_enforced (which pulses on dispatch, before
    the verify re-read), this pulses only once the target is confirmed at desired.
    """

    def __init__(self, entry: ConfigEntry, engine: RuleEngine) -> None:
        """Initialize the recently-enforced-verified sensor."""
        super().__init__(
            entry,
            engine,
            "recently_enforced_verified",
            "recently_enforced_verified",
        )

    @property
    def is_on(self) -> bool:
        """Return True if a verify-confirmed enforcement fired in the last 30s."""
        return bool(self._engine.is_recently_enforced_verified())


class EntityGuardRecentlyEnforcedUnreachableSensor(EntityGuardRecentlyEnforcedBase):
    """ON for 30 seconds after a verify-EXHAUSTED enforcement failure.

    Created only while verify is enabled (see async_setup_entry). The failure
    twin of recently_enforced_verified: pulses when the service call did not
    raise but the target never reached desired after the corrective retries —
    a silent non-compliance detectable only when verify is on.
    """

    def __init__(self, entry: ConfigEntry, engine: RuleEngine) -> None:
        """Initialize the recently-enforced-unreachable sensor."""
        super().__init__(
            entry,
            engine,
            "recently_enforced_unreachable",
            "recently_enforced_unreachable",
        )

    @property
    def is_on(self) -> bool:
        """Return True if a verify-exhausted failure fired in the last 30s."""
        return bool(self._engine.is_recently_enforced_unreachable())
