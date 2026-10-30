"""Integration-level triggers for Entity Guard.

Exposes each rule event — enforced / skipped / loop-detected / verified /
unreachable / cancelled / suppressed — as a trigger in Home Assistant's generic trigger
picker (not device-bound). Each trigger takes an OPTIONAL `config_entry_id`
(the rule): set it to fire only for that rule, omit it to fire for ANY rule.
The any-rule variant is the capability device triggers (device_trigger.py)
cannot offer — a device trigger is bound to one rule's device.

Wiring nuance (same as device_trigger.py): events carry
`rule_id = entry.unique_id`, which is NOT the config entry_id. So when the user
scopes a trigger to a rule by its config entry, we resolve entry → unique_id
and filter the bus event on that `rule_id`.
"""

from __future__ import annotations

from typing import Any, override

import voluptuous as vol
from homeassistant.const import ATTR_ENTITY_ID, CONF_OPTIONS
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.automation import move_top_level_schema_fields_to_options
from homeassistant.helpers.trigger import (
    Trigger,
    TriggerActionRunner,
    TriggerConfig,
    TriggerNotTriggeredReporter,
)
from homeassistant.helpers.typing import ConfigType

# ponytail: floor is 2026.7.0 (manifest.json), so these four symbols resolve at
# the top level with no version guard — the exact release where the single-API
# shape landed. No importorskip, no try/except fallback.
from .device_trigger import TRIGGER_TYPE_EVENT

CONF_CONFIG_ENTRY_ID = "config_entry_id"

# relative platform type == submodule name == this file's trigger key prefix
_OPTIONS_SCHEMA = vol.Schema({vol.Optional(CONF_CONFIG_ENTRY_ID): cv.string})

_CONFIG_SCHEMA = vol.Schema({vol.Required(CONF_OPTIONS, default=dict): _OPTIONS_SCHEMA})


def _rule_id_for_entry(hass: HomeAssistant, entry_id: str) -> str | None:
    """Resolve a config entry_id to the rule's event `rule_id` (unique_id)."""
    entry = hass.config_entries.async_get_entry(entry_id)
    return entry.unique_id if entry else None


class _EventTrigger(Trigger):
    """Fire on one Entity Guard bus event, optionally scoped to one rule.

    Concrete per-event subclasses set `_event_type`; the generic machinery is
    shared. Mirrors the zwave_js pattern of one class per trigger type.
    """

    _event_type: str

    _options: dict[str, Any]

    @classmethod
    @override
    async def async_validate_complete_config(
        cls, hass: HomeAssistant, complete_config: ConfigType
    ) -> ConfigType:
        """Allow `config_entry_id` at the top level, fold it into options."""
        complete_config = move_top_level_schema_fields_to_options(
            complete_config, {vol.Optional(CONF_CONFIG_ENTRY_ID): cv.string}
        )
        return await super().async_validate_complete_config(hass, complete_config)

    @classmethod
    @override
    async def async_validate_config(
        cls, hass: HomeAssistant, config: ConfigType
    ) -> ConfigType:
        """Validate config; if a rule is named, assert its entry exists."""
        config = _CONFIG_SCHEMA(config)
        options = config[CONF_OPTIONS]
        if CONF_CONFIG_ENTRY_ID in options:
            entry_id = options[CONF_CONFIG_ENTRY_ID]
            if hass.config_entries.async_get_entry(entry_id) is None:
                raise vol.Invalid(f"Config entry '{entry_id}' not found")
        return config

    def __init__(self, hass: HomeAssistant, config: TriggerConfig) -> None:
        """Initialize trigger."""
        super().__init__(hass, config)
        self._options = config.options or {}

    @override
    async def async_attach_runner(
        self,
        run_action: TriggerActionRunner,
        did_not_trigger: TriggerNotTriggeredReporter | None = None,
    ) -> CALLBACK_TYPE:
        """Listen for the event, filter on the optional rule, run the action."""
        entry_id = self._options.get(CONF_CONFIG_ENTRY_ID)
        scoped = entry_id is not None
        # Validated to exist in async_validate_config, but the entry may have been
        # removed between validation and attach. A scoped trigger whose rule is
        # gone must match NOTHING — never silently widen to any-rule.
        rule_id = _rule_id_for_entry(self._hass, entry_id) if scoped else None

        @callback
        def _handle(event: Event) -> None:
            # Scoped trigger: fire only for the resolved rule. If the entry was
            # removed after validation (rule_id is None) match NOTHING — and
            # guard against a payload with no rule_id so None != None can't slip
            # a malformed event through.
            if scoped and (rule_id is None or event.data.get("rule_id") != rule_id):
                return
            description = (
                f"Entity Guard event {self._event_type} fired for rule "
                f"{event.data.get('rule_name', event.data.get('rule_id'))}"
            )
            run_action(
                {
                    "rule_id": event.data.get("rule_id"),
                    "rule_name": event.data.get("rule_name"),
                    ATTR_ENTITY_ID: event.data.get(ATTR_ENTITY_ID),
                    "event_type": self._event_type,
                    "event_data": event.data,
                },
                description,
                event.context,
            )

        return self._hass.bus.async_listen(self._event_type, _handle)


def _make(event_type: str) -> type[_EventTrigger]:
    """Build a concrete trigger class bound to one event type."""

    class _Bound(_EventTrigger):
        _event_type = event_type

    return _Bound


# trigger key (== strings.json `triggers.<key>`) → concrete Trigger class.
_TRIGGERS: dict[str, type[Trigger]] = {
    trigger_type: _make(event_type)
    for trigger_type, event_type in TRIGGER_TYPE_EVENT.items()
}


async def async_get_triggers(hass: HomeAssistant) -> dict[str, type[Trigger]]:
    """Return Entity Guard's integration-level triggers."""
    return _TRIGGERS
