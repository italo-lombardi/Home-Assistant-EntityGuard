"""Config and options flow for the Entity Guard integration."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_IMPORT,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    ATTRIBUTES_BY_DOMAIN,
    CONF_ADD_DEBOUNCE,
    CONF_ADD_FLAGS,
    CONF_ATTRIBUTE,
    CONF_CUSTOM_RATE_LIMIT,
    CONF_DEBOUNCE_ENABLED,
    CONF_DEBOUNCE_SECONDS,
    CONF_DELAY_SECONDS,
    CONF_ENTRY_TYPE,
    CONF_FLAG_ENTITY,
    CONF_FLAG_MATCH_STATE,
    CONF_FLAGS,
    CONF_MAX_ENFORCEMENTS_PER_MINUTE,
    CONF_MODE,
    CONF_OPERATOR,
    CONF_RATE_LIMIT_ENABLED,
    CONF_RULE_ID,
    CONF_RULE_NAME,
    CONF_SAFETY_ACKNOWLEDGED,
    CONF_TARGET_ENTITIES,
    CONF_TARGET_STATE,
    CONF_TARGET_VALUE,
    CONF_THRESHOLD,
    CONF_TRIGGER_STATES,
    DEFAULT_DEBOUNCE_ENABLED,
    DEFAULT_DEBOUNCE_SECONDS,
    DEFAULT_DELAY_SECONDS,
    DEFAULT_LOOP_SUPPRESS_MINUTES,
    DEFAULT_MAX_ENFORCEMENTS_PER_MINUTE,
    DEFAULT_TARGET_STATE,
    DEFAULT_TRIGGER_STATES,
    DOMAIN,
    DOMAIN_STATE_OPTIONS,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_RULE,
    FALLBACK_STATE_OPTIONS,
    FORBIDDEN_STATES,
    MAX_DEBOUNCE_SECONDS,
    MAX_DELAY_SECONDS,
    MAX_RATE_LIMIT,
    MIN_DEBOUNCE_SECONDS,
    MIN_DELAY_SECONDS,
    MIN_RATE_LIMIT,
    MODE_ATTRIBUTE,
    MODE_STATE,
    SAFETY_DOMAINS,
    SUPPORTED_ATTRIBUTES,
    SUPPORTED_OPERATORS,
    has_safety_target as _has_safety_target,
)

_LOGGER = logging.getLogger(__name__)


def _current_state_hint(hass: Any, entity_id: str | None) -> str:
    """Return 'entity_id is currently: <state>' or empty when no entity given."""
    if not entity_id or hass is None:
        return ""
    state = hass.states.get(entity_id)
    if state is None:
        return f"{entity_id} is currently: (unknown — entity not found)"
    return f"{entity_id} is currently: {state.state}"


def _rule_name_taken(
    hass_entries: list[ConfigEntry], name: str, ignore_entry_id: str | None = None
) -> bool:
    """Return True if another rule entry already owns ``name``."""
    lowered = name.strip().lower()
    for entry in hass_entries:
        if entry.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_RULE:
            continue
        if ignore_entry_id is not None and entry.entry_id == ignore_entry_id:
            continue
        if str(entry.data.get(CONF_RULE_NAME, entry.title)).strip().lower() == lowered:
            return True
    return False


def _build_summary(data: dict[str, Any], hass: Any = None) -> str:
    """Build the markdown preview shown before saving.

    ``hass`` is optional; when supplied, target entity ids are decorated with
    their friendly names from the state machine.
    """

    def _label(entity_id: str) -> str:
        if hass is None:
            return f"`{entity_id}`"
        state = hass.states.get(entity_id)
        friendly = state.attributes.get("friendly_name") if state else None
        if friendly and friendly != entity_id:
            return f"{friendly} (`{entity_id}`)"
        return f"`{entity_id}`"

    lines: list[str] = []
    lines.append(f"**Name:** {data.get(CONF_RULE_NAME, '')}")

    targets = data.get(CONF_TARGET_ENTITIES, [])
    if len(targets) == 1:
        lines.append(f"**Target:** {_label(targets[0])}")
    else:
        lines.append("**Targets:**")
        lines.extend(f"  - {_label(t)}" for t in targets)

    mode = data.get(CONF_MODE)
    if mode == MODE_STATE:
        triggers = ", ".join(f"`{t}`" for t in data.get(CONF_TRIGGER_STATES, []))
        target_state = data.get(CONF_TARGET_STATE, "")
        lines.append(f"**Rule:** when state is {triggers} → force `{target_state}`")
    elif mode == MODE_ATTRIBUTE:
        lines.append(
            f"**Rule:** when `{data.get(CONF_ATTRIBUTE)}` "
            f"{data.get(CONF_OPERATOR)} `{data.get(CONF_THRESHOLD)}` "
            f"→ clamp to `{data.get(CONF_TARGET_VALUE)}`"
        )

    delay = data.get(CONF_DELAY_SECONDS, 0)
    lines.append(f"**Delay:** {delay}s" + (" (immediate)" if delay == 0 else ""))

    flags = data.get(CONF_FLAGS, [])
    if flags:
        lines.append("**Conditions (all must match):**")
        lines.extend(
            f"  - {_label(f[CONF_FLAG_ENTITY])} = `{f[CONF_FLAG_MATCH_STATE]}`"
            for f in flags
        )
    else:
        lines.append("**Conditions:** none")

    if data.get(CONF_DEBOUNCE_ENABLED):
        lines.append(f"**Debounce:** {data.get(CONF_DEBOUNCE_SECONDS)}s")
    else:
        lines.append("**Debounce:** disabled")

    rate = data.get(CONF_MAX_ENFORCEMENTS_PER_MINUTE)
    if rate and rate > 0:
        lines.append(
            f"**Loop protection:** auto-suppress for "
            f"{DEFAULT_LOOP_SUPPRESS_MINUTES} min above {rate}/min"
        )
    else:
        lines.append("**Loop protection:** disabled")

    if data.get(CONF_SAFETY_ACKNOWLEDGED):
        lines.append("**Safety:** acknowledged")

    return "\n\n".join(lines)


def _delay_selector() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=MIN_DELAY_SECONDS,
            max=MAX_DELAY_SECONDS,
            step=1,
            unit_of_measurement="seconds",
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _coerce_delay(raw: Any) -> int | None:
    """Cast a delay-seconds form value to a valid bounded int. Returns None on bad input."""
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return None
    if value < MIN_DELAY_SECONDS or value > MAX_DELAY_SECONDS:
        return None
    return value


def _debounce_selector() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=MIN_DEBOUNCE_SECONDS,
            max=MAX_DEBOUNCE_SECONDS,
            step=1,
            unit_of_measurement="seconds",
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _rate_selector() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=MIN_RATE_LIMIT,
            max=MAX_RATE_LIMIT,
            step=1,
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _number_selector() -> selector.NumberSelector:
    # Threshold/target_value: unbounded numeric input — engine clamps domain-specifically.
    return selector.NumberSelector(
        selector.NumberSelectorConfig(mode=selector.NumberSelectorMode.BOX, step="any")
    )


def _states_for_entities(entities: list[str]) -> list[str]:
    """Union of typical state values across the domains of the given entities.

    Order: preserves first-seen domain ordering, dedupes across domains. Falls back
    to ['on','off'] if no recognized domain. Custom states still allowed via
    custom_value=True on the selector.
    """
    seen: dict[str, None] = {}
    for entity_id in entities:
        domain = entity_id.split(".", 1)[0]
        for state in DOMAIN_STATE_OPTIONS.get(domain, []):
            seen.setdefault(state, None)
    if not seen:
        return list(FALLBACK_STATE_OPTIONS)
    return list(seen.keys())


def _attributes_for_entities(entities: list[str]) -> list[str]:
    """Union of supported attributes across the domains of the given entities.

    Falls back to the full SUPPORTED_ATTRIBUTES list when no chosen entity
    belongs to a domain with a known clamp service — keeps the form usable
    even if the user picks a domain we don't have a service mapping for.
    """
    seen: dict[str, None] = {}
    for entity_id in entities:
        domain = entity_id.split(".", 1)[0]
        for attr in ATTRIBUTES_BY_DOMAIN.get(domain, []):
            seen.setdefault(attr, None)
    if not seen:
        return list(SUPPORTED_ATTRIBUTES)
    return list(seen.keys())


def _trigger_states_selector(options: list[str]) -> selector.SelectSelector:
    """Multi-select trigger-states selector. custom_value lets users add unlisted states."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options or list(FALLBACK_STATE_OPTIONS),
            multiple=True,
            custom_value=True,
        )
    )


def _target_state_selector(options: list[str]) -> selector.SelectSelector:
    """Single-select target-state selector with custom_value fallback."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options or list(FALLBACK_STATE_OPTIONS),
            multiple=False,
            custom_value=True,
        )
    )


class EntityGuardConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle config flow for Entity Guard."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._rule_data: dict[str, Any] = {}
        self._flags: list[dict[str, str]] = []
        self._add_debounce: bool = False
        self._custom_rate_limit: bool = False

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """User-initiated entry: go straight into rule creation. Hub auto-created on first rule save."""
        return await self.async_step_rule(user_input)

    # ------------------------------------------------------------------ Hub (import-only)

    async def async_step_import(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Internal: create the singleton hub entry. Triggered when first rule is saved."""
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_HUB:
                _LOGGER.debug("Hub import aborted: hub already exists")
                return self.async_abort(reason="single_instance_allowed")
        await self.async_set_unique_id(f"{DOMAIN}_hub")
        self._abort_if_unique_id_configured()
        _LOGGER.info("Creating hub config entry via import flow")
        return self.async_create_entry(
            title="Entity Guard Hub",
            data={CONF_ENTRY_TYPE: ENTRY_TYPE_HUB},
        )

    # ------------------------------------------------------------------ Rule basics

    async def async_step_rule(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Rule basics: name, target entities, mode."""
        errors: dict[str, str] = {}

        if user_input is not None:
            name = str(user_input[CONF_RULE_NAME]).strip()
            entities = user_input.get(CONF_TARGET_ENTITIES, [])

            if not name:
                errors[CONF_RULE_NAME] = "empty_rule_name"
            elif _rule_name_taken(self.hass.config_entries.async_entries(DOMAIN), name):
                _LOGGER.warning("Duplicate rule name attempted: %s", name)
                errors[CONF_RULE_NAME] = "name_already_exists"
            elif not entities:
                errors[CONF_TARGET_ENTITIES] = "empty_target_entities"

            if not errors:
                # UUID created here, frozen for the lifetime of the entry.
                rule_uuid = str(uuid.uuid4())
                await self.async_set_unique_id(rule_uuid)
                self._abort_if_unique_id_configured()
                _LOGGER.debug(
                    "Rule basics validated: name=%s mode=%s entities=%s uuid=%s",
                    name,
                    user_input[CONF_MODE],
                    entities,
                    rule_uuid,
                )

                self._rule_data = {
                    CONF_ENTRY_TYPE: ENTRY_TYPE_RULE,
                    CONF_RULE_ID: rule_uuid,
                    CONF_RULE_NAME: name,
                    CONF_TARGET_ENTITIES: entities,
                    CONF_MODE: user_input[CONF_MODE],
                    CONF_FLAGS: [],
                }
                if user_input[CONF_MODE] == MODE_STATE:
                    return await self.async_step_state()
                return await self.async_step_attribute()

        schema = vol.Schema(
            {
                vol.Required(CONF_RULE_NAME): str,
                vol.Required(CONF_TARGET_ENTITIES): selector.EntitySelector(
                    selector.EntitySelectorConfig(multiple=True)
                ),
                vol.Required(CONF_MODE, default=MODE_STATE): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[MODE_STATE, MODE_ATTRIBUTE],
                        translation_key="mode",
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="rule", data_schema=schema, errors=errors)

    # ------------------------------------------------------------------ State mode

    async def async_step_state(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """State-mode step: trigger states, target state, delay."""
        errors: dict[str, str] = {}

        if user_input is not None:
            triggers = [t.strip() for t in user_input.get(CONF_TRIGGER_STATES, [])]
            target = str(user_input.get(CONF_TARGET_STATE, "")).strip()
            delay = _coerce_delay(user_input.get(CONF_DELAY_SECONDS))
            if not triggers:
                errors[CONF_TRIGGER_STATES] = "empty_trigger_states"
            elif any(t.lower() in FORBIDDEN_STATES for t in triggers):
                errors[CONF_TRIGGER_STATES] = "forbidden_state"
            elif not target:
                errors[CONF_TARGET_STATE] = "empty_target_state"
            elif target.lower() in FORBIDDEN_STATES:
                errors[CONF_TARGET_STATE] = "forbidden_state"
            elif target.lower() in [t.lower() for t in triggers]:
                errors[CONF_TARGET_STATE] = "target_in_triggers"
            elif delay is None:
                errors[CONF_DELAY_SECONDS] = "invalid_delay"
            else:
                self._rule_data[CONF_TRIGGER_STATES] = triggers
                self._rule_data[CONF_TARGET_STATE] = target
                self._rule_data[CONF_DELAY_SECONDS] = delay
                return await self.async_step_extras()

        state_options = _states_for_entities(
            self._rule_data.get(CONF_TARGET_ENTITIES, [])
        )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_TRIGGER_STATES, default=DEFAULT_TRIGGER_STATES
                ): _trigger_states_selector(state_options),
                vol.Required(
                    CONF_TARGET_STATE, default=DEFAULT_TARGET_STATE
                ): _target_state_selector(state_options),
                vol.Required(
                    CONF_DELAY_SECONDS, default=DEFAULT_DELAY_SECONDS
                ): _delay_selector(),
            }
        )
        return self.async_show_form(step_id="state", data_schema=schema, errors=errors)

    # ------------------------------------------------------------------ Attribute mode

    async def async_step_attribute(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Attribute-mode step: attribute, operator, threshold, target value, delay."""
        errors: dict[str, str] = {}

        if user_input is not None:
            delay = _coerce_delay(user_input.get(CONF_DELAY_SECONDS))
            try:
                threshold = float(user_input[CONF_THRESHOLD])
                target_value = float(user_input[CONF_TARGET_VALUE])
            except (
                TypeError,
                ValueError,
            ):  # pragma: no cover — vol.Coerce(float) in schema prevents this
                errors["base"] = "invalid_threshold"
            else:
                if delay is None:
                    errors[CONF_DELAY_SECONDS] = "invalid_delay"
                else:
                    self._rule_data[CONF_ATTRIBUTE] = user_input[CONF_ATTRIBUTE]
                    self._rule_data[CONF_OPERATOR] = user_input[CONF_OPERATOR]
                    self._rule_data[CONF_THRESHOLD] = threshold
                    self._rule_data[CONF_TARGET_VALUE] = target_value
                    self._rule_data[CONF_DELAY_SECONDS] = delay
                    return await self.async_step_extras()

        attr_options = _attributes_for_entities(
            self._rule_data.get(CONF_TARGET_ENTITIES, [])
        )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ATTRIBUTE, default=attr_options[0] if attr_options else None
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=attr_options,
                        translation_key="attribute",
                    )
                ),
                vol.Required(
                    CONF_OPERATOR, default=SUPPORTED_OPERATORS[0]
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=SUPPORTED_OPERATORS,
                        translation_key="operator",
                    )
                ),
                vol.Required(CONF_THRESHOLD): _number_selector(),
                vol.Required(CONF_TARGET_VALUE): _number_selector(),
                vol.Required(
                    CONF_DELAY_SECONDS, default=DEFAULT_DELAY_SECONDS
                ): _delay_selector(),
            }
        )
        return self.async_show_form(
            step_id="attribute", data_schema=schema, errors=errors
        )

    # ------------------------------------------------------------------ Options toggles

    async def async_step_extras(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Three opt-in toggles, sorted A-Z by visible label."""
        if user_input is not None:
            self._add_debounce = bool(user_input.get(CONF_ADD_DEBOUNCE, False))
            self._custom_rate_limit = bool(
                user_input.get(CONF_CUSTOM_RATE_LIMIT, False)
            )
            _LOGGER.debug(
                "Extras chosen: flags=%s debounce=%s custom_rate=%s",
                user_input.get(CONF_ADD_FLAGS, False),
                self._add_debounce,
                self._custom_rate_limit,
            )
            if user_input.get(CONF_ADD_FLAGS, False):
                return await self.async_step_flags()
            self._rule_data[CONF_FLAGS] = self._flags
            return await self._after_flags()

        # Order alphabetically by English label:
        # "Add extra conditions" → "Customize loop protection" → "Enable debounce".
        schema = vol.Schema(
            {
                vol.Required(CONF_ADD_FLAGS, default=False): selector.BooleanSelector(),
                vol.Required(
                    CONF_CUSTOM_RATE_LIMIT, default=False
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_ADD_DEBOUNCE, default=False
                ): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="extras", data_schema=schema)

    # ------------------------------------------------------------------ Flags (repeating)

    async def async_step_flags(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Optional repeating step to collect flag conditions (AND'd)."""
        errors: dict[str, str] = {}
        last_entity: str | None = None

        if user_input is not None:
            entity = user_input.get(CONF_FLAG_ENTITY)
            match_state = user_input.get(CONF_FLAG_MATCH_STATE, "")
            add_another = user_input.get("add_another", False)
            last_entity = entity

            # Both fields must be set together; an empty pair just skips ahead.
            if entity and str(match_state).strip():
                self._flags.append(
                    {
                        CONF_FLAG_ENTITY: entity,
                        CONF_FLAG_MATCH_STATE: str(match_state).strip(),
                    }
                )
            elif entity or str(match_state).strip():  # pragma: no branch
                errors["base"] = "incomplete_flag"

            if not errors and not add_another:
                self._rule_data[CONF_FLAGS] = self._flags
                return await self._after_flags()

        schema = vol.Schema(
            {
                vol.Optional(CONF_FLAG_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig()
                ),
                vol.Optional(CONF_FLAG_MATCH_STATE, default=""): str,
                vol.Optional("add_another", default=False): selector.BooleanSelector(),
            }
        )
        description_placeholders = {
            "count": str(len(self._flags)),
            "summary": ", ".join(
                f"{f[CONF_FLAG_ENTITY]}={f[CONF_FLAG_MATCH_STATE]}" for f in self._flags
            )
            or "(none)",
            "current_state": _current_state_hint(self.hass, last_entity),
        }
        return self.async_show_form(
            step_id="flags",
            data_schema=schema,
            errors=errors,
            description_placeholders=description_placeholders,
        )

    async def _after_flags(self) -> ConfigFlowResult:
        """Route after flags step: skip advanced entirely when no toggle was set."""
        if not self._add_debounce:
            self._rule_data[CONF_DEBOUNCE_ENABLED] = False
            self._rule_data[CONF_DEBOUNCE_SECONDS] = DEFAULT_DEBOUNCE_SECONDS
        if not self._custom_rate_limit:
            self._rule_data[CONF_MAX_ENFORCEMENTS_PER_MINUTE] = (
                DEFAULT_MAX_ENFORCEMENTS_PER_MINUTE
            )
        if self._add_debounce or self._custom_rate_limit:
            return await self.async_step_advanced()
        return await self._after_advanced()

    async def _after_advanced(self) -> ConfigFlowResult:
        """Common branch after advanced (or when advanced is skipped)."""
        if _has_safety_target(self._rule_data[CONF_TARGET_ENTITIES]):
            return await self.async_step_safety()
        return await self.async_step_preview()

    # ------------------------------------------------------------------ Advanced

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Advanced step: shown only when debounce or custom rate-limit was opted into."""
        errors: dict[str, str] = {}

        if user_input is not None:
            if self._add_debounce:
                self._rule_data[CONF_DEBOUNCE_ENABLED] = True
                self._rule_data[CONF_DEBOUNCE_SECONDS] = int(
                    user_input[CONF_DEBOUNCE_SECONDS]
                )
            if self._custom_rate_limit:
                if user_input.get(CONF_RATE_LIMIT_ENABLED, True):
                    raw = user_input.get(CONF_MAX_ENFORCEMENTS_PER_MINUTE)
                    try:
                        rate = int(float(raw))
                    except (
                        TypeError,
                        ValueError,
                    ):  # pragma: no cover — schema coerces to number
                        rate = -1
                    if (
                        rate < MIN_RATE_LIMIT or rate > MAX_RATE_LIMIT
                    ):  # pragma: no cover — selector clamps to MIN/MAX
                        errors[CONF_MAX_ENFORCEMENTS_PER_MINUTE] = "invalid_rate"
                    else:
                        self._rule_data[CONF_MAX_ENFORCEMENTS_PER_MINUTE] = rate
                else:
                    # Sentinel 0 disables loop protection for this rule.
                    self._rule_data[CONF_MAX_ENFORCEMENTS_PER_MINUTE] = 0
            if not errors:  # pragma: no branch
                return await self._after_advanced()

        fields: dict[Any, Any] = {}
        if self._add_debounce:
            fields[
                vol.Required(CONF_DEBOUNCE_SECONDS, default=DEFAULT_DEBOUNCE_SECONDS)
            ] = _debounce_selector()
        if self._custom_rate_limit:
            fields[vol.Required(CONF_RATE_LIMIT_ENABLED, default=True)] = (
                selector.BooleanSelector()
            )
            fields[
                vol.Optional(
                    CONF_MAX_ENFORCEMENTS_PER_MINUTE,
                    default=DEFAULT_MAX_ENFORCEMENTS_PER_MINUTE,
                )
            ] = _rate_selector()
        return self.async_show_form(
            step_id="advanced",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={
                "default_rate": str(DEFAULT_MAX_ENFORCEMENTS_PER_MINUTE)
            },
        )

    # ------------------------------------------------------------------ Safety ack

    async def async_step_safety(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Safety acknowledgment for cover/lock/climate targets."""
        errors: dict[str, str] = {}

        if user_input is not None:
            if not user_input.get(CONF_SAFETY_ACKNOWLEDGED, False):
                errors[CONF_SAFETY_ACKNOWLEDGED] = "safety_not_acknowledged"
            else:
                self._rule_data[CONF_SAFETY_ACKNOWLEDGED] = True
                return await self.async_step_preview()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SAFETY_ACKNOWLEDGED, default=False
                ): selector.BooleanSelector(),
            }
        )
        safety_targets = [
            e
            for e in self._rule_data.get(CONF_TARGET_ENTITIES, [])
            if e.split(".", 1)[0] in SAFETY_DOMAINS
        ]
        return self.async_show_form(
            step_id="safety",
            data_schema=schema,
            errors=errors,
            description_placeholders={"entities": ", ".join(safety_targets)},
        )

    # ------------------------------------------------------------------ Preview

    async def async_step_preview(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Final confirmation that creates the rule entry. Submit button = create."""
        if user_input is not None:
            _LOGGER.info(
                "Creating rule '%s' (id=%s, mode=%s, targets=%s)",
                self._rule_data.get(CONF_RULE_NAME),
                self._rule_data.get(CONF_RULE_ID),
                self._rule_data.get(CONF_MODE),
                self._rule_data.get(CONF_TARGET_ENTITIES),
            )
            _LOGGER.debug("Rule full payload: %s", self._rule_data)
            await self._async_ensure_hub()
            return self.async_create_entry(
                title=self._rule_data[CONF_RULE_NAME],
                data=self._rule_data,
            )

        return self.async_show_form(
            step_id="preview",
            data_schema=vol.Schema({}),
            description_placeholders={
                "summary": _build_summary(self._rule_data, self.hass)
            },
        )

    async def _async_ensure_hub(self) -> None:
        """Trigger hub creation if no hub entry exists yet."""
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_HUB:
                _LOGGER.debug("Hub already exists; skipping ensure")
                return
        _LOGGER.info("Hub missing during rule create — triggering import flow")
        self.hass.async_create_task(
            self.hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_IMPORT},
                data={},
            )
        )

    # ------------------------------------------------------------------ Options entry

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> EntityGuardOptionsFlow:
        """Return the options flow handler for a rule entry."""
        return EntityGuardOptionsFlow()


class EntityGuardOptionsFlow(OptionsFlow):
    """Options flow for editing an existing rule entry."""

    def __init__(self) -> None:
        """Initialize."""
        self._working: dict[str, Any] = {}

    # ------------------------------------------------------------------ Init / menu

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Top-level options menu — Hub entries are read-only."""
        if self.config_entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_HUB:
            return self.async_abort(reason="hub_no_options")

        # Snapshot existing data once; each sub-step mutates ``_working`` and saves.
        if not self._working:  # pragma: no branch
            self._working = dict(self.config_entry.data)
            self._working.setdefault(CONF_FLAGS, [])

        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "edit_basics",
                "edit_mode",
                "edit_entities",
                "edit_flags",
                "edit_advanced",
            ],
            description_placeholders={
                "summary": _build_summary(self._working, self.hass)
            },
        )

    # ------------------------------------------------------------------ Persist helper

    def _save(self) -> ConfigFlowResult:
        """Write working copy back without touching unique_id / rule_id."""
        # rule_id and unique_id are immutable; merge into stored data only.
        self._working[CONF_RULE_ID] = self.config_entry.data.get(CONF_RULE_ID)
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data=self._working,
            title=self._working.get(CONF_RULE_NAME, self.config_entry.title),
        )
        return self.async_create_entry(title="", data={})

    # ------------------------------------------------------------------ Basics

    async def async_step_edit_basics(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit name."""
        errors: dict[str, str] = {}

        if user_input is not None:
            name = str(user_input[CONF_RULE_NAME]).strip()
            if not name:
                errors[CONF_RULE_NAME] = "empty_rule_name"
            elif _rule_name_taken(
                self.hass.config_entries.async_entries(DOMAIN),
                name,
                ignore_entry_id=self.config_entry.entry_id,
            ):
                errors[CONF_RULE_NAME] = "name_already_exists"
            else:
                self._working[CONF_RULE_NAME] = name
                return self._save()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_RULE_NAME, default=self._working.get(CONF_RULE_NAME, "")
                ): str,
            }
        )
        return self.async_show_form(
            step_id="edit_basics", data_schema=schema, errors=errors
        )

    # ------------------------------------------------------------------ Entities

    async def async_step_edit_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit target entities (add/remove)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            entities = user_input.get(CONF_TARGET_ENTITIES, [])
            if not entities:
                errors[CONF_TARGET_ENTITIES] = "empty_target_entities"
            else:
                self._working[CONF_TARGET_ENTITIES] = entities
                # New safety domain present? Force re-ack if not already given.
                if _has_safety_target(entities) and not self._working.get(
                    CONF_SAFETY_ACKNOWLEDGED, False
                ):
                    return await self.async_step_edit_safety()
                return self._save()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_TARGET_ENTITIES,
                    default=self._working.get(CONF_TARGET_ENTITIES, []),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(multiple=True)
                ),
            }
        )
        return self.async_show_form(
            step_id="edit_entities", data_schema=schema, errors=errors
        )

    async def async_step_edit_safety(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-acknowledge safety after adding cover/lock/climate targets."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_SAFETY_ACKNOWLEDGED):
                errors[CONF_SAFETY_ACKNOWLEDGED] = "safety_not_acknowledged"
            else:
                self._working[CONF_SAFETY_ACKNOWLEDGED] = True
                return self._save()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SAFETY_ACKNOWLEDGED, default=False
                ): selector.BooleanSelector()
            }
        )
        return self.async_show_form(
            step_id="edit_safety", data_schema=schema, errors=errors
        )

    # ------------------------------------------------------------------ Mode-specific

    async def async_step_edit_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Dispatch to the correct mode-specific edit step."""
        if self._working.get(CONF_MODE) == MODE_STATE:
            return await self.async_step_edit_state()
        return await self.async_step_edit_attribute()

    async def async_step_edit_state(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit state-mode parameters."""
        errors: dict[str, str] = {}

        if user_input is not None:
            triggers = [t.strip() for t in user_input.get(CONF_TRIGGER_STATES, [])]
            target = str(user_input.get(CONF_TARGET_STATE, "")).strip()
            delay = _coerce_delay(user_input.get(CONF_DELAY_SECONDS))
            if not triggers:
                errors[CONF_TRIGGER_STATES] = "empty_trigger_states"
            elif any(t.lower() in FORBIDDEN_STATES for t in triggers):
                errors[CONF_TRIGGER_STATES] = "forbidden_state"
            elif not target:
                errors[CONF_TARGET_STATE] = "empty_target_state"
            elif target.lower() in FORBIDDEN_STATES:
                errors[CONF_TARGET_STATE] = "forbidden_state"
            elif target.lower() in [t.lower() for t in triggers]:
                errors[CONF_TARGET_STATE] = "target_in_triggers"
            elif delay is None:
                errors[CONF_DELAY_SECONDS] = "invalid_delay"
            else:
                self._working[CONF_TRIGGER_STATES] = triggers
                self._working[CONF_TARGET_STATE] = target
                self._working[CONF_DELAY_SECONDS] = delay
                return self._save()

        state_options = _states_for_entities(
            self._working.get(CONF_TARGET_ENTITIES, [])
        )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_TRIGGER_STATES,
                    default=self._working.get(
                        CONF_TRIGGER_STATES, DEFAULT_TRIGGER_STATES
                    ),
                ): _trigger_states_selector(state_options),
                vol.Required(
                    CONF_TARGET_STATE,
                    default=self._working.get(CONF_TARGET_STATE, DEFAULT_TARGET_STATE),
                ): _target_state_selector(state_options),
                vol.Required(
                    CONF_DELAY_SECONDS,
                    default=self._working.get(
                        CONF_DELAY_SECONDS, DEFAULT_DELAY_SECONDS
                    ),
                ): _delay_selector(),
            }
        )
        return self.async_show_form(
            step_id="edit_state", data_schema=schema, errors=errors
        )

    async def async_step_edit_attribute(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit attribute-mode parameters."""
        errors: dict[str, str] = {}

        if user_input is not None:
            delay = _coerce_delay(user_input.get(CONF_DELAY_SECONDS))
            try:
                threshold = float(user_input[CONF_THRESHOLD])
                target_value = float(user_input[CONF_TARGET_VALUE])
            except (
                TypeError,
                ValueError,
            ):  # pragma: no cover — vol.Coerce(float) in schema prevents this
                errors["base"] = "invalid_threshold"
            else:
                if delay is None:
                    errors[CONF_DELAY_SECONDS] = "invalid_delay"
                else:
                    self._working[CONF_ATTRIBUTE] = user_input[CONF_ATTRIBUTE]
                    self._working[CONF_OPERATOR] = user_input[CONF_OPERATOR]
                    self._working[CONF_THRESHOLD] = threshold
                    self._working[CONF_TARGET_VALUE] = target_value
                    self._working[CONF_DELAY_SECONDS] = delay
                    return self._save()

        attr_options = _attributes_for_entities(
            self._working.get(CONF_TARGET_ENTITIES, [])
        )
        # Preserve the stored attribute even if it's not in the filtered list
        # (e.g. user changed targets and an old selection no longer matches).
        current_attr = self._working.get(
            CONF_ATTRIBUTE, attr_options[0] if attr_options else None
        )
        if current_attr not in attr_options:
            attr_options = [current_attr, *attr_options]
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ATTRIBUTE,
                    default=current_attr,
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=attr_options, translation_key="attribute"
                    )
                ),
                vol.Required(
                    CONF_OPERATOR,
                    default=self._working.get(CONF_OPERATOR, SUPPORTED_OPERATORS[0]),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=SUPPORTED_OPERATORS, translation_key="operator"
                    )
                ),
                vol.Required(
                    CONF_THRESHOLD, default=self._working.get(CONF_THRESHOLD, 0)
                ): _number_selector(),
                vol.Required(
                    CONF_TARGET_VALUE, default=self._working.get(CONF_TARGET_VALUE, 0)
                ): _number_selector(),
                vol.Required(
                    CONF_DELAY_SECONDS,
                    default=self._working.get(
                        CONF_DELAY_SECONDS, DEFAULT_DELAY_SECONDS
                    ),
                ): _delay_selector(),
            }
        )
        return self.async_show_form(
            step_id="edit_attribute", data_schema=schema, errors=errors
        )

    # ------------------------------------------------------------------ Flags

    async def async_step_edit_flags(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit flag list — pass JSON-friendly compact representation."""
        errors: dict[str, str] = {}
        last_entity: str | None = None

        if user_input is not None:
            action = user_input.get("action", "save")
            entity = user_input.get(CONF_FLAG_ENTITY)
            match_state = str(user_input.get(CONF_FLAG_MATCH_STATE, "")).strip()
            last_entity = entity

            match action:
                case "clear":
                    _LOGGER.debug("Flag action: clear all conditions")
                    self._working[CONF_FLAGS] = []
                    return self._save()

                case "replace":
                    if not entity or not match_state:
                        errors["base"] = "incomplete_flag"
                    else:
                        _LOGGER.debug(
                            "Flag action: replace with single condition %s=%s",
                            entity,
                            match_state,
                        )
                        self._working[CONF_FLAGS] = [
                            {
                                CONF_FLAG_ENTITY: entity,
                                CONF_FLAG_MATCH_STATE: match_state,
                            }
                        ]
                        return self._save()

                case "add":
                    if not entity or not match_state:
                        errors["base"] = "incomplete_flag"
                    else:
                        existing = list(self._working.get(CONF_FLAGS, []))
                        existing.append(
                            {
                                CONF_FLAG_ENTITY: entity,
                                CONF_FLAG_MATCH_STATE: match_state,
                            }
                        )
                        _LOGGER.debug(
                            "Flag action: add condition %s=%s (total: %d)",
                            entity,
                            match_state,
                            len(existing),
                        )
                        self._working[CONF_FLAGS] = existing
                        return self._save()

                case _:
                    _LOGGER.debug("Flag action: save without changes")
                    return self._save()

        flags = self._working.get(CONF_FLAGS, [])
        summary = (
            ", ".join(
                f"{f[CONF_FLAG_ENTITY]}={f[CONF_FLAG_MATCH_STATE]}" for f in flags
            )
            or "(none)"
        )

        schema = vol.Schema(
            {
                vol.Optional(CONF_FLAG_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig()
                ),
                vol.Optional(CONF_FLAG_MATCH_STATE, default=""): str,
                vol.Required("action", default="save"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=["save", "add", "replace", "clear"],
                        translation_key="flag_action",
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="edit_flags",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "summary": summary,
                "current_state": _current_state_hint(self.hass, last_entity),
            },
        )

    # ------------------------------------------------------------------ Advanced

    async def async_step_edit_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit debounce + rate limit."""
        if user_input is not None:
            self._working[CONF_DEBOUNCE_ENABLED] = bool(
                user_input[CONF_DEBOUNCE_ENABLED]
            )
            self._working[CONF_DEBOUNCE_SECONDS] = int(
                user_input[CONF_DEBOUNCE_SECONDS]
            )
            self._working[CONF_MAX_ENFORCEMENTS_PER_MINUTE] = int(
                user_input[CONF_MAX_ENFORCEMENTS_PER_MINUTE]
            )
            return self._save()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_DEBOUNCE_ENABLED,
                    default=self._working.get(
                        CONF_DEBOUNCE_ENABLED, DEFAULT_DEBOUNCE_ENABLED
                    ),
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_DEBOUNCE_SECONDS,
                    default=self._working.get(
                        CONF_DEBOUNCE_SECONDS, DEFAULT_DEBOUNCE_SECONDS
                    ),
                ): _debounce_selector(),
                vol.Required(
                    CONF_MAX_ENFORCEMENTS_PER_MINUTE,
                    default=self._working.get(
                        CONF_MAX_ENFORCEMENTS_PER_MINUTE,
                        DEFAULT_MAX_ENFORCEMENTS_PER_MINUTE,
                    ),
                ): _rate_selector(),
            }
        )
        return self.async_show_form(step_id="edit_advanced", data_schema=schema)
