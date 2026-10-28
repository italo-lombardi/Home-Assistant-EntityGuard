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
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    ATTRIBUTE_DISPLAY_NAMES,
    ATTRIBUTES_BY_DOMAIN,
    COLOR_ATTRIBUTES,
    CONF_ADD_DEBOUNCE,
    CONF_ADD_FLAGS,
    CONF_ATTRIBUTE,
    CONF_CUSTOM_RATE_LIMIT,
    CONF_DEBOUNCE_ENABLED,
    CONF_DEBOUNCE_SECONDS,
    CONF_DELAY_OFF_SECONDS,
    CONF_DELAY_ON_SECONDS,
    CONF_DELAY_SECONDS,
    CONF_ENTRY_TYPE,
    CONF_FLAG_ENTITY,
    CONF_FLAG_MATCH_STATE,
    CONF_FLAGS,
    CONF_FOLLOW_INVERT,
    CONF_MAX_ENFORCEMENTS_PER_MINUTE,
    CONF_MODE,
    CONF_OPERATOR,
    CONF_RATE_LIMIT_ENABLED,
    CONF_RULE_ID,
    CONF_RULE_NAME,
    CONF_SAFETY_ACKNOWLEDGED,
    CONF_SOURCE_ATTRIBUTE,
    CONF_SOURCE_ENTITY,
    CONF_SOURCE_VALUE_FROM,
    CONF_TARGET_ENTITIES,
    CONF_TARGET_STATE,
    CONF_TARGET_VALUE,
    CONF_THRESHOLD,
    CONF_TRIGGER_STATES,
    CONF_VERIFY_ENABLED,
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
    MAX_COLOR_TEMP_KELVIN,
    MAX_DEBOUNCE_SECONDS,
    MAX_DELAY_SECONDS,
    MAX_RATE_LIMIT,
    MIN_COLOR_TEMP_KELVIN,
    MIN_DEBOUNCE_SECONDS,
    MIN_DELAY_SECONDS,
    MIN_RATE_LIMIT,
    MODE_DYNAMIC_ATTRIBUTE,
    MODE_DYNAMIC_STATE,
    MODE_FIXED_ATTRIBUTE,
    MODE_FIXED_STATE,
    NUMERIC_ATTRIBUTES,
    SAFETY_DOMAINS,
    SOURCE_VALUE_FROM_ATTRIBUTE,
    SOURCE_VALUE_FROM_STATE,
    SUPPORTED_OPERATORS,
    has_safety_target,
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
        # Read the merged view: a rename is persisted to entry.options (the runtime
        # source of truth), leaving the original name in entry.data. Reading data
        # alone would compare against the stale pre-rename name.
        merged = {**entry.data, **(entry.options or {})}
        if str(merged.get(CONF_RULE_NAME, entry.title)).strip().lower() == lowered:
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
    if mode == MODE_FIXED_STATE:
        triggers = ", ".join(f"`{t}`" for t in data.get(CONF_TRIGGER_STATES, []))
        target_state = data.get(CONF_TARGET_STATE, "")
        lines.append(f"**Rule:** when state is {triggers} → force `{target_state}`")
    elif mode == MODE_DYNAMIC_STATE:
        direction = "invert" if data.get(CONF_FOLLOW_INVERT) else "mirror"
        source = _label(data.get(CONF_SOURCE_ENTITY, ""))
        if data.get(CONF_SOURCE_VALUE_FROM) == SOURCE_VALUE_FROM_ATTRIBUTE:
            src = f"{source}.{data.get(CONF_SOURCE_ATTRIBUTE, '')}"
        else:
            src = source
        lines.append(f"**Rule:** dynamic state — follows {src} ({direction})")
    elif mode == MODE_DYNAMIC_ATTRIBUTE:
        target_attr = data.get(CONF_ATTRIBUTE, "")
        source = _label(data.get(CONF_SOURCE_ENTITY, ""))
        if data.get(CONF_SOURCE_VALUE_FROM) == SOURCE_VALUE_FROM_ATTRIBUTE:
            src_val = f"{source}.{data.get(CONF_SOURCE_ATTRIBUTE, '')}"
        else:
            src_val = f"{source}.state"
        lines.append(f"**Rule:** dynamic attribute — `{target_attr}` follows {src_val}")
    elif mode == MODE_FIXED_ATTRIBUTE:
        attribute = data.get(CONF_ATTRIBUTE)
        if _is_color_attribute(attribute):
            lines.append(
                f"**Rule:** when `{attribute}` differs from "
                f"`{data.get(CONF_TARGET_VALUE)}` → enforce match"
            )
        else:
            lines.append(
                f"**Rule:** when `{attribute}` "
                f"{data.get(CONF_OPERATOR)} `{data.get(CONF_THRESHOLD)}` "
                f"→ clamp to `{data.get(CONF_TARGET_VALUE)}`"
            )

    if mode == MODE_DYNAMIC_STATE:
        # Per-direction delays. Fall back to the shared delay when a direction is
        # unset (mirrors RuleConfig.effective_delay).
        shared = data.get(CONF_DELAY_SECONDS, 0)
        delay_on = data.get(CONF_DELAY_ON_SECONDS, shared)
        delay_off = data.get(CONF_DELAY_OFF_SECONDS, shared)
        lines.append(
            f"**Delay (on):** {delay_on}s" + (" (immediate)" if delay_on == 0 else "")
        )
        lines.append(
            f"**Delay (off):** {delay_off}s"
            + (" (immediate)" if delay_off == 0 else "")
        )
    else:
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

    if data.get(CONF_VERIFY_ENABLED):
        lines.append("**Verify:** re-check after enforcing; retry if not reached")

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


def _rgb_color_selector() -> selector.ColorRGBSelector:
    return selector.ColorRGBSelector()


def _color_temp_kelvin_selector() -> selector.ColorTempSelector:
    return selector.ColorTempSelector(
        selector.ColorTempSelectorConfig(
            unit=selector.ColorTempSelectorUnit.KELVIN,
            min=MIN_COLOR_TEMP_KELVIN,
            max=MAX_COLOR_TEMP_KELVIN,
        )
    )


def _is_color_attribute(attribute: Any) -> bool:
    """Return True when the attribute uses match-style color enforcement."""
    return attribute in COLOR_ATTRIBUTES


def _coerce_rgb_color(raw: Any) -> list[int] | None:
    """Validate and normalize an RGB selector value."""
    try:
        return _rgb_color_selector()(raw)
    except vol.Invalid:
        return None


def _coerce_color_temp_kelvin(raw: Any) -> int | None:
    """Validate and normalize a Kelvin selector value."""
    try:
        return int(_color_temp_kelvin_selector()(raw))
    except vol.Invalid:
        return None


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

    Falls back to numeric-only attributes when no chosen entity belongs to a
    domain with a known service mapping.
    """
    seen: dict[str, None] = {}
    for entity_id in entities:
        domain = entity_id.split(".", 1)[0]
        for attr in ATTRIBUTES_BY_DOMAIN.get(domain, []):
            seen.setdefault(attr, None)
    if not seen:
        return list(NUMERIC_ATTRIBUTES)
    return list(seen.keys())


def _attribute_schema(
    current_attr: str | None,
    *,
    target_value_default: Any = None,
    threshold_default: Any = 0,
    operator_default: str | None = None,
    delay_default: int = DEFAULT_DELAY_SECONDS,
) -> vol.Schema:
    """Build the attribute-mode form schema for numeric or color attributes."""
    schema: dict[Any, Any] = {}

    if current_attr == ATTR_RGB_COLOR:
        rgb_default = (
            target_value_default
            if isinstance(target_value_default, (list, tuple))
            and len(target_value_default) == 3
            else [255, 255, 255]
        )
        schema[vol.Required(CONF_TARGET_VALUE, default=rgb_default)] = (
            _rgb_color_selector()
        )
    elif current_attr == ATTR_COLOR_TEMP_KELVIN:
        kelvin_default = (
            target_value_default
            if isinstance(target_value_default, int)
            and not isinstance(target_value_default, bool)
            else 2700
        )
        schema[vol.Required(CONF_TARGET_VALUE, default=kelvin_default)] = (
            _color_temp_kelvin_selector()
        )
    else:
        schema[
            vol.Required(
                CONF_OPERATOR, default=operator_default or SUPPORTED_OPERATORS[0]
            )
        ] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=SUPPORTED_OPERATORS,
                translation_key="operator",
            )
        )
        schema[vol.Required(CONF_THRESHOLD, default=threshold_default)] = (
            _number_selector()
        )
        schema[
            vol.Required(
                CONF_TARGET_VALUE,
                default=target_value_default if target_value_default is not None else 0,
            )
        ] = _number_selector()

    schema[vol.Required(CONF_DELAY_SECONDS, default=delay_default)] = _delay_selector()
    return vol.Schema(schema)


def _attribute_choice_schema(
    attr_options: list[str], current_attr: str | None = None
) -> vol.Schema:
    """Build the attribute-selection form schema."""
    default_attr = current_attr or (attr_options[0] if attr_options else None)
    return vol.Schema(
        {
            vol.Required(CONF_ATTRIBUTE, default=default_attr): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=attr_options,
                    translation_key="attribute",
                )
            )
        }
    )


# Sentinel option for dynamic_attribute's "read the source's state" choice. A valid
# slug (no leading/trailing underscore — Hassfest requirement).
SOURCE_VALUE_FROM_OPTIONS = [SOURCE_VALUE_FROM_STATE, SOURCE_VALUE_FROM_ATTRIBUTE]


def _source_selector(exclude_targets: list[str] | None) -> selector.EntitySelector:
    """Single-entity source selector, excluding the rule's target entities.

    Excluding the targets (SPEC §9) stops a target being picked as its own source;
    the submit-time validators still guard the pair for hand-edited options.
    """
    cfg: dict[str, Any] = {}
    if exclude_targets:
        cfg["exclude_entities"] = exclude_targets
    return selector.EntitySelector(selector.EntitySelectorConfig(**cfg))


def _source_value_from_field() -> dict[Any, Any]:
    """Shared first-step field: where to read the live value (state|attribute).

    The attribute NAME is not collected here — when the user picks 'attribute',
    the flow advances to a second screen that lists the source's live attributes
    to follow (_source_attribute_pick_schema)."""
    return {
        vol.Required(
            CONF_SOURCE_VALUE_FROM, default=SOURCE_VALUE_FROM_STATE
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=SOURCE_VALUE_FROM_OPTIONS,
                translation_key="source_value_from",
            )
        ),
    }


# Attributes HA puts on most entities that are noise in a "pick a value to follow"
# list — hide them so the picker shows meaningful source values only.
_NOISE_SOURCE_ATTRIBUTES = frozenset(
    {
        "friendly_name",
        "supported_features",
        "icon",
        "entity_picture",
        "device_class",
        "attribution",
        "assumed_state",
        "restored",
        # Capability / enumeration attributes: describe what the entity CAN do,
        # not a current value to follow. (User flagged supported_color_modes.)
        "supported_color_modes",
        "color_mode",
        "effect_list",
        "min_color_temp_kelvin",
        "max_color_temp_kelvin",
        "min_mireds",
        "max_mireds",
    }
)

# Prefixes/suffixes marking capability/bound attrs (not followable current values).
_NOISE_ATTR_PREFIXES = ("supported_", "min_", "max_")
_NOISE_ATTR_SUFFIXES = ("_list",)


def _is_noise_attribute(name: str) -> bool:
    """True for capability/enumeration/bound attrs that aren't a followable value."""
    if name in _NOISE_SOURCE_ATTRIBUTES:
        return True
    return name.startswith(_NOISE_ATTR_PREFIXES) or name.endswith(_NOISE_ATTR_SUFFIXES)


def _prettify_attr_name(name: str) -> str:
    """Human label for an attribute key. Known attributes use the SAME display name
    as the target-attribute dropdown (ATTRIBUTE_DISPLAY_NAMES, mirroring
    selector.attribute.options); live/unknown attrs fall back to a prettified key,
    e.g. 'supported_color_modes' → 'Supported color modes'."""
    return ATTRIBUTE_DISPLAY_NAMES.get(name) or name.replace("_", " ").capitalize()


def _friendly_name(hass: Any, entity_id: str | None) -> str:
    """Friendly name for an entity_id, falling back to the id (or '' if unset)."""
    if not entity_id:
        return ""
    if hass is None:
        return entity_id
    state = hass.states.get(entity_id)
    if state is None:
        return entity_id
    return state.attributes.get("friendly_name") or entity_id


def _source_attribute_options(
    hass: Any, source: str | None, current: str | None = None
) -> list[Any]:
    """Live attribute options for the pick step: one per meaningful source
    attribute. The option VALUE is the raw attribute key (what gets stored/read);
    the LABEL is the beautified name only — no current-value suffix, since off
    entities report None-valued attributes that read as noise ('Brightness (None)').
    Noise/capability attributes are hidden.

    ``current`` (the stored source_attribute, on edit) is injected as a synthetic
    option when it is not among the live attributes — e.g. the source is offline
    and reports none. This preserves the stored value in the LIST without
    custom_value (LIST and custom_value conflict in HA — the latter forces a
    dropdown), so an offline-source edit keeps the attribute selectable/preselected
    (SPEC/-89 edge #4). Empty result → the pick step shows source_has_no_attributes."""
    state = hass.states.get(source) if source and hass is not None else None
    options = (
        [
            selector.SelectOptionDict(value=name, label=_prettify_attr_name(name))
            for name in state.attributes
            if not _is_noise_attribute(name)
        ]
        if state is not None
        else []
    )
    if current and not any(o["value"] == current for o in options):
        options.append(
            selector.SelectOptionDict(value=current, label=_prettify_attr_name(current))
        )
    return options


def _source_attribute_pick_schema(
    options: list[Any], current: str | None
) -> vol.Schema:
    """Second-step schema: pick one of the source's live attributes. Options carry
    beautified SelectOptionDict labels, mode=LIST (radio, all options visible —
    consistent with the fixed/dynamic target-attribute pickers and the mode
    selector). Each radio shows its label ('Brightness').

    No custom_value: HA ignores mode=LIST when custom_value is set (forces a
    dropdown), so the offline-edit case is handled by injecting the stored
    attribute as a synthetic option in _source_attribute_options instead."""
    return vol.Schema(
        {
            vol.Required(
                CONF_SOURCE_ATTRIBUTE,
                **({"default": current} if current else {}),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    mode=selector.SelectSelectorMode.LIST,
                )
            )
        }
    )


def _dynamic_state_schema(
    *,
    exclude_targets: list[str] | None = None,
    source_default: str | None = None,
    invert_default: bool = False,
    delay_on_default: int = DEFAULT_DELAY_SECONDS,
    delay_off_default: int = DEFAULT_DELAY_SECONDS,
) -> vol.Schema:
    """dynamic_state first-step schema: source + invert + value-from + per-direction
    delays. One delay for driving the target ON, one for OFF (dynamic_state only).
    When value-from is attribute, the flow advances to the attribute pick step."""
    return vol.Schema(
        {
            vol.Required(
                CONF_SOURCE_ENTITY,
                **({"default": source_default} if source_default else {}),
            ): _source_selector(exclude_targets),
            vol.Required(
                CONF_FOLLOW_INVERT, default=invert_default
            ): selector.BooleanSelector(),
            **_source_value_from_field(),
            vol.Required(
                CONF_DELAY_ON_SECONDS, default=delay_on_default
            ): _delay_selector(),
            vol.Required(
                CONF_DELAY_OFF_SECONDS, default=delay_off_default
            ): _delay_selector(),
        }
    )


def _dynamic_attribute_schema(
    attr_options: list[str],
    *,
    exclude_targets: list[str] | None = None,
    source_default: str | None = None,
    target_attribute_default: str | None = None,
    delay_default: int = DEFAULT_DELAY_SECONDS,
) -> vol.Schema:
    """dynamic_attribute first-step schema: target attribute + delay + source +
    value-from. When value-from is attribute, the flow advances to the attribute
    pick step. Raw copy — no operator/threshold/target_value.

    Field order (user-requested): target attribute, delay, source entity, read
    value from. HA renders fields in schema insertion order.
    """
    target_default = (
        target_attribute_default
        if target_attribute_default in attr_options
        else (attr_options[0] if attr_options else None)
    )
    schema: dict[Any, Any] = {
        vol.Required(CONF_ATTRIBUTE, default=target_default): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=attr_options,
                translation_key="attribute",
                mode=selector.SelectSelectorMode.LIST,
            )
        ),
        vol.Required(CONF_DELAY_SECONDS, default=delay_default): _delay_selector(),
        vol.Required(
            CONF_SOURCE_ENTITY,
            **({"default": source_default} if source_default else {}),
        ): _source_selector(exclude_targets),
        **_source_value_from_field(),
    }
    return vol.Schema(schema)


def _validate_source_target(source: str | None, targets: list[str]) -> dict[str, str]:
    """Shared source checks for both dynamic modes: source set + not a target."""
    if not source:
        return {CONF_SOURCE_ENTITY: "empty_source_entity"}
    if source in targets:
        return {CONF_SOURCE_ENTITY: "source_in_targets"}
    return {}


def _validate_dynamic_state(source: str | None, targets: list[str]) -> dict[str, str]:
    """Validate dynamic_state first step: source set + not a target. The attribute
    name (when reading from an attribute) is collected on the pick step."""
    return _validate_source_target(source, targets)


def _validate_dynamic_attribute(
    source: str | None,
    targets: list[str],
    target_attribute: str | None,
) -> dict[str, str]:
    """Validate dynamic_attribute first step (structural only — the live value is
    not type-checked at config time; that degrades as a clean enforce skip).

    - source set + not a target.
    - target_attribute valid for at least one target's domain.

    The source attribute name is collected on the pick step when reading from an
    attribute.
    """
    errors = _validate_source_target(source, targets)
    if target_attribute and target_attribute not in _attributes_for_entities(targets):
        errors[CONF_ATTRIBUTE] = "attribute_invalid_for_target"
    return errors


def _validate_source_attribute_pick(source_attribute: str | None) -> dict[str, str]:
    """Validate the attribute pick step: a source attribute must be chosen."""
    if not (source_attribute and source_attribute.strip()):
        return {CONF_SOURCE_ATTRIBUTE: "source_attribute_required"}
    return {}


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

    VERSION = 2

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
                if user_input[CONF_MODE] == MODE_FIXED_STATE:
                    return await self.async_step_state()
                if user_input[CONF_MODE] == MODE_DYNAMIC_STATE:
                    return await self.async_step_dynamic_state_params()
                if user_input[CONF_MODE] == MODE_DYNAMIC_ATTRIBUTE:
                    return await self.async_step_dynamic_attribute_params()
                return await self.async_step_attribute()

        schema = vol.Schema(
            {
                vol.Required(CONF_RULE_NAME): str,
                vol.Required(CONF_TARGET_ENTITIES): selector.EntitySelector(
                    selector.EntitySelectorConfig(multiple=True)
                ),
                vol.Required(
                    CONF_MODE, default=MODE_FIXED_STATE
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            MODE_FIXED_STATE,
                            MODE_FIXED_ATTRIBUTE,
                            MODE_DYNAMIC_STATE,
                            MODE_DYNAMIC_ATTRIBUTE,
                        ],
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
        """Attribute-mode step: choose which attribute to enforce."""
        attr_options = _attributes_for_entities(
            self._rule_data.get(CONF_TARGET_ENTITIES, [])
        )
        current_attr = self._rule_data.get(
            CONF_ATTRIBUTE, attr_options[0] if attr_options else None
        )

        if user_input is not None:
            self._rule_data[CONF_ATTRIBUTE] = user_input[CONF_ATTRIBUTE]
            return await self.async_step_attribute_params()

        return self.async_show_form(
            step_id="attribute",
            data_schema=_attribute_choice_schema(attr_options, current_attr),
        )

    async def async_step_attribute_params(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Attribute-mode step: configure params for the chosen attribute."""
        errors: dict[str, str] = {}
        attr_options = _attributes_for_entities(
            self._rule_data.get(CONF_TARGET_ENTITIES, [])
        )
        selected_attr = self._rule_data.get(
            CONF_ATTRIBUTE, attr_options[0] if attr_options else None
        )
        self._rule_data[CONF_ATTRIBUTE] = selected_attr

        if user_input is not None:
            delay = _coerce_delay(user_input.get(CONF_DELAY_SECONDS))
            if delay is None:
                errors[CONF_DELAY_SECONDS] = "invalid_delay"
            elif selected_attr == ATTR_RGB_COLOR:
                target_value = _coerce_rgb_color(user_input.get(CONF_TARGET_VALUE))
                if target_value is None:
                    errors[CONF_TARGET_VALUE] = "invalid_rgb_color"
                else:
                    self._rule_data[CONF_OPERATOR] = None
                    self._rule_data[CONF_THRESHOLD] = None
                    self._rule_data[CONF_TARGET_VALUE] = target_value
                    self._rule_data[CONF_DELAY_SECONDS] = delay
                    return await self.async_step_extras()
            elif selected_attr == ATTR_COLOR_TEMP_KELVIN:
                target_value = _coerce_color_temp_kelvin(
                    user_input.get(CONF_TARGET_VALUE)
                )
                if target_value is None:
                    errors[CONF_TARGET_VALUE] = "invalid_color_temp_kelvin"
                else:
                    self._rule_data[CONF_OPERATOR] = None
                    self._rule_data[CONF_THRESHOLD] = None
                    self._rule_data[CONF_TARGET_VALUE] = target_value
                    self._rule_data[CONF_DELAY_SECONDS] = delay
                    return await self.async_step_extras()
            else:
                try:
                    threshold = float(user_input[CONF_THRESHOLD])
                    target_value = float(user_input[CONF_TARGET_VALUE])
                except (
                    TypeError,
                    ValueError,
                ):  # pragma: no cover — vol.Coerce(float) in schema prevents this
                    errors["base"] = "invalid_threshold"
                else:
                    self._rule_data[CONF_OPERATOR] = user_input[CONF_OPERATOR]
                    self._rule_data[CONF_THRESHOLD] = threshold
                    self._rule_data[CONF_TARGET_VALUE] = target_value
                    self._rule_data[CONF_DELAY_SECONDS] = delay
                    return await self.async_step_extras()

        return self.async_show_form(
            step_id="attribute_params",
            data_schema=_attribute_schema(
                selected_attr,
                target_value_default=self._rule_data.get(CONF_TARGET_VALUE),
                threshold_default=self._rule_data.get(CONF_THRESHOLD, 0),
                operator_default=self._rule_data.get(
                    CONF_OPERATOR, SUPPORTED_OPERATORS[0]
                ),
                delay_default=self._rule_data.get(
                    CONF_DELAY_SECONDS, DEFAULT_DELAY_SECONDS
                ),
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------ Dynamic state

    async def async_step_dynamic_state_params(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """dynamic_state first step: source + invert + value-from.

        Target on/off follows the source's state, or (value_from == attribute) a
        source attribute reduced to on/off, then XOR invert. Picking 'attribute'
        advances to the live attribute pick step.
        """
        errors: dict[str, str] = {}
        targets = self._rule_data.get(CONF_TARGET_ENTITIES, [])

        if user_input is not None:
            source = user_input.get(CONF_SOURCE_ENTITY)
            delay_on = _coerce_delay(user_input.get(CONF_DELAY_ON_SECONDS))
            delay_off = _coerce_delay(user_input.get(CONF_DELAY_OFF_SECONDS))
            errors = _validate_dynamic_state(source, targets)
            if delay_on is None:
                errors[CONF_DELAY_ON_SECONDS] = "invalid_delay"
            if delay_off is None:
                errors[CONF_DELAY_OFF_SECONDS] = "invalid_delay"
            if not errors:
                self._rule_data[CONF_SOURCE_ENTITY] = source
                self._rule_data[CONF_FOLLOW_INVERT] = bool(
                    user_input.get(CONF_FOLLOW_INVERT, False)
                )
                self._rule_data[CONF_DELAY_ON_SECONDS] = delay_on
                self._rule_data[CONF_DELAY_OFF_SECONDS] = delay_off
                self._rule_data[CONF_DELAY_SECONDS] = delay_on
                value_from = user_input.get(
                    CONF_SOURCE_VALUE_FROM, SOURCE_VALUE_FROM_STATE
                )
                self._rule_data[CONF_SOURCE_VALUE_FROM] = value_from
                if value_from == SOURCE_VALUE_FROM_ATTRIBUTE:
                    return await self.async_step_source_attribute_pick()
                self._rule_data[CONF_SOURCE_ATTRIBUTE] = None
                return await self.async_step_extras()

        return self.async_show_form(
            step_id="dynamic_state_params",
            data_schema=_dynamic_state_schema(
                exclude_targets=targets,
                source_default=self._rule_data.get(CONF_SOURCE_ENTITY),
                invert_default=bool(self._rule_data.get(CONF_FOLLOW_INVERT, False)),
                delay_on_default=self._rule_data.get(
                    CONF_DELAY_ON_SECONDS,
                    self._rule_data.get(CONF_DELAY_SECONDS, DEFAULT_DELAY_SECONDS),
                ),
                delay_off_default=self._rule_data.get(
                    CONF_DELAY_OFF_SECONDS,
                    self._rule_data.get(CONF_DELAY_SECONDS, DEFAULT_DELAY_SECONDS),
                ),
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------ Dynamic attribute

    async def async_step_dynamic_attribute_params(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """dynamic_attribute first step: source + value-from + target attribute.
        Picking 'attribute' advances to the live attribute pick step."""
        errors: dict[str, str] = {}
        targets = self._rule_data.get(CONF_TARGET_ENTITIES, [])
        attr_options = _attributes_for_entities(targets)

        if user_input is not None:
            source = user_input.get(CONF_SOURCE_ENTITY)
            target_attribute = user_input.get(CONF_ATTRIBUTE)
            delay = _coerce_delay(user_input.get(CONF_DELAY_SECONDS))
            errors = _validate_dynamic_attribute(source, targets, target_attribute)
            if delay is None:
                errors[CONF_DELAY_SECONDS] = "invalid_delay"
            if not errors:
                self._rule_data[CONF_SOURCE_ENTITY] = source
                self._rule_data[CONF_ATTRIBUTE] = target_attribute
                self._rule_data[CONF_DELAY_SECONDS] = delay
                value_from = user_input.get(
                    CONF_SOURCE_VALUE_FROM, SOURCE_VALUE_FROM_STATE
                )
                self._rule_data[CONF_SOURCE_VALUE_FROM] = value_from
                # Raw copy: no operator/threshold/target_value in dynamic_attribute.
                if value_from == SOURCE_VALUE_FROM_ATTRIBUTE:
                    return await self.async_step_source_attribute_pick()
                self._rule_data[CONF_SOURCE_ATTRIBUTE] = None
                return await self.async_step_extras()

        return self.async_show_form(
            step_id="dynamic_attribute_params",
            data_schema=_dynamic_attribute_schema(
                attr_options,
                exclude_targets=targets,
                source_default=self._rule_data.get(CONF_SOURCE_ENTITY),
                target_attribute_default=self._rule_data.get(CONF_ATTRIBUTE),
                delay_default=self._rule_data.get(
                    CONF_DELAY_SECONDS, DEFAULT_DELAY_SECONDS
                ),
            ),
            errors=errors,
        )

    # ------------------------------------------------ Source attribute pick (shared)

    async def async_step_source_attribute_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Second screen (both dynamic modes, value_from == attribute): pick one of
        the source entity's live attributes (beautified names)."""
        errors: dict[str, str] = {}
        source = self._rule_data.get(CONF_SOURCE_ENTITY)
        current = self._rule_data.get(CONF_SOURCE_ATTRIBUTE)
        options = _source_attribute_options(self.hass, source, current)

        if user_input is not None:
            source_attribute = (user_input.get(CONF_SOURCE_ATTRIBUTE) or "").strip()
            errors = _validate_source_attribute_pick(source_attribute)
            if not errors:
                self._rule_data[CONF_SOURCE_ATTRIBUTE] = source_attribute
                return await self.async_step_extras()
        elif not options:
            # No live attributes and no stored attribute to inject — nothing to
            # show in the radio list; surface the hint instead of an empty list.
            errors["base"] = "source_has_no_attributes"

        return self.async_show_form(
            step_id="source_attribute_pick",
            data_schema=_source_attribute_pick_schema(options, current),
            errors=errors,
            description_placeholders={"source": _friendly_name(self.hass, source)},
        )

    # ------------------------------------------------------------------ Options toggles

    async def async_step_extras(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Opt-in toggles, sorted A-Z by visible label. The verify toggle is shown
        only for state modes (fixed_state / dynamic_state) — verify-after-enforce is
        state-only in v1, so it would be a dead toggle on an attribute rule."""
        verify_applicable = self._rule_data.get(CONF_MODE) in (
            MODE_FIXED_STATE,
            MODE_DYNAMIC_STATE,
        )
        if user_input is not None:
            self._add_debounce = bool(user_input.get(CONF_ADD_DEBOUNCE, False))
            self._custom_rate_limit = bool(
                user_input.get(CONF_CUSTOM_RATE_LIMIT, False)
            )
            # Only honor the verify toggle when it was actually shown; on an
            # attribute rule it is absent and verify stays off.
            self._rule_data[CONF_VERIFY_ENABLED] = verify_applicable and bool(
                user_input.get(CONF_VERIFY_ENABLED, False)
            )
            _LOGGER.debug(
                "Extras chosen: flags=%s debounce=%s custom_rate=%s verify=%s",
                user_input.get(CONF_ADD_FLAGS, False),
                self._add_debounce,
                self._custom_rate_limit,
                self._rule_data[CONF_VERIFY_ENABLED],
            )
            if user_input.get(CONF_ADD_FLAGS, False):
                return await self.async_step_flags()
            self._rule_data[CONF_FLAGS] = self._flags
            return await self._after_flags()

        # Order alphabetically by English label:
        # "Add extra conditions" → "Customize loop protection" → "Enable debounce"
        # → "Verify enforcement worked" (verify shown only for state modes).
        fields: dict[Any, Any] = {
            vol.Required(CONF_ADD_FLAGS, default=False): selector.BooleanSelector(),
            vol.Required(
                CONF_CUSTOM_RATE_LIMIT, default=False
            ): selector.BooleanSelector(),
            vol.Required(CONF_ADD_DEBOUNCE, default=False): selector.BooleanSelector(),
        }
        if verify_applicable:
            fields[vol.Required(CONF_VERIFY_ENABLED, default=False)] = (
                selector.BooleanSelector()
            )
        return self.async_show_form(step_id="extras", data_schema=vol.Schema(fields))

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
        if has_safety_target(self._rule_data[CONF_TARGET_ENTITIES]):
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

        # Snapshot existing config once; each sub-step mutates ``_working`` and saves.
        # Seed from the merged {**data, **options} view — the same merge
        # parse_rule_config uses (models.py) — so the flow shows and edits exactly
        # what the engine runs: the values the number sliders and the debounce switch
        # persisted to entry.options, not the stale creation-time data values.
        if not self._working:  # pragma: no branch
            self._working = {
                **self.config_entry.data,
                **(self.config_entry.options or {}),
            }
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
        """Persist edits to entry.options — the runtime source of truth.

        parse_rule_config merges {**data, **options} with options winning; the number
        sliders and the debounce switch also write to options. Writing the merged
        working copy here keeps a single owner (options) and preserves any slider/
        switch value the user did not edit.

        entry.data is left untouched, so structural keys (entry_type, rule_id) and
        the original name survive there for the readers that need them.

        entry.title must be updated explicitly: async_create_entry(title="") in an
        options flow sets entry.options to the returned data but does NOT touch
        entry.title. Without this call a rename would update the engine (which reads
        options) but leave the device name, entity friendly names, and repair-issue
        text stale until manually changed.

        Both title AND options are written in this single async_update_entry so the
        update listener fires at most once. The options-flow manager then calls
        async_update_entry(options=<same dict>) again as it finalizes the flow, but
        async_update_entry is a no-op (returns False, fires no listener) when the
        options are unchanged — so a rename reloads the entry exactly once, not twice.
        """
        options = dict(self._working)
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            title=options.get(CONF_RULE_NAME, self.config_entry.title),
            options=options,
        )
        return self.async_create_entry(title="", data=options)

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
                if has_safety_target(entities) and not self._working.get(
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
        mode = self._working.get(CONF_MODE)
        if mode == MODE_FIXED_STATE:
            return await self.async_step_edit_state()
        if mode == MODE_DYNAMIC_STATE:
            return await self.async_step_edit_dynamic_state_params()
        if mode == MODE_DYNAMIC_ATTRIBUTE:
            return await self.async_step_edit_dynamic_attribute_params()
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

    async def async_step_edit_dynamic_state_params(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit dynamic_state first step: source + invert + value-from."""
        errors: dict[str, str] = {}
        targets = self._working.get(CONF_TARGET_ENTITIES, [])

        if user_input is not None:
            source = user_input.get(CONF_SOURCE_ENTITY)
            delay_on = _coerce_delay(user_input.get(CONF_DELAY_ON_SECONDS))
            delay_off = _coerce_delay(user_input.get(CONF_DELAY_OFF_SECONDS))
            errors = _validate_dynamic_state(source, targets)
            if delay_on is None:
                errors[CONF_DELAY_ON_SECONDS] = "invalid_delay"
            if delay_off is None:
                errors[CONF_DELAY_OFF_SECONDS] = "invalid_delay"
            if not errors:
                self._working[CONF_SOURCE_ENTITY] = source
                self._working[CONF_FOLLOW_INVERT] = bool(
                    user_input.get(CONF_FOLLOW_INVERT, False)
                )
                self._working[CONF_DELAY_ON_SECONDS] = delay_on
                self._working[CONF_DELAY_OFF_SECONDS] = delay_off
                self._working[CONF_DELAY_SECONDS] = delay_on
                value_from = user_input.get(
                    CONF_SOURCE_VALUE_FROM, SOURCE_VALUE_FROM_STATE
                )
                self._working[CONF_SOURCE_VALUE_FROM] = value_from
                if value_from == SOURCE_VALUE_FROM_ATTRIBUTE:
                    return await self.async_step_edit_source_attribute_pick()
                self._working[CONF_SOURCE_ATTRIBUTE] = None
                return self._save()

        return self.async_show_form(
            step_id="edit_dynamic_state_params",
            data_schema=_dynamic_state_schema(
                exclude_targets=targets,
                source_default=self._working.get(CONF_SOURCE_ENTITY),
                invert_default=bool(self._working.get(CONF_FOLLOW_INVERT, False)),
                delay_on_default=self._working.get(
                    CONF_DELAY_ON_SECONDS,
                    self._working.get(CONF_DELAY_SECONDS, DEFAULT_DELAY_SECONDS),
                ),
                delay_off_default=self._working.get(
                    CONF_DELAY_OFF_SECONDS,
                    self._working.get(CONF_DELAY_SECONDS, DEFAULT_DELAY_SECONDS),
                ),
            ),
            errors=errors,
        )

    async def async_step_edit_dynamic_attribute_params(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit dynamic_attribute first step: source + value-from + target attr."""
        errors: dict[str, str] = {}
        targets = self._working.get(CONF_TARGET_ENTITIES, [])
        attr_options = _attributes_for_entities(targets)

        if user_input is not None:
            source = user_input.get(CONF_SOURCE_ENTITY)
            target_attribute = user_input.get(CONF_ATTRIBUTE)
            delay = _coerce_delay(user_input.get(CONF_DELAY_SECONDS))
            errors = _validate_dynamic_attribute(source, targets, target_attribute)
            if delay is None:
                errors[CONF_DELAY_SECONDS] = "invalid_delay"
            if not errors:
                self._working[CONF_SOURCE_ENTITY] = source
                self._working[CONF_ATTRIBUTE] = target_attribute
                self._working[CONF_DELAY_SECONDS] = delay
                value_from = user_input.get(
                    CONF_SOURCE_VALUE_FROM, SOURCE_VALUE_FROM_STATE
                )
                self._working[CONF_SOURCE_VALUE_FROM] = value_from
                if value_from == SOURCE_VALUE_FROM_ATTRIBUTE:
                    return await self.async_step_edit_source_attribute_pick()
                self._working[CONF_SOURCE_ATTRIBUTE] = None
                return self._save()

        return self.async_show_form(
            step_id="edit_dynamic_attribute_params",
            data_schema=_dynamic_attribute_schema(
                attr_options,
                exclude_targets=targets,
                source_default=self._working.get(CONF_SOURCE_ENTITY),
                target_attribute_default=self._working.get(CONF_ATTRIBUTE),
                delay_default=self._working.get(
                    CONF_DELAY_SECONDS, DEFAULT_DELAY_SECONDS
                ),
            ),
            errors=errors,
        )

    async def async_step_edit_source_attribute_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit second screen (both dynamic modes, value_from == attribute): pick
        one of the source's live attributes (beautified names)."""
        errors: dict[str, str] = {}
        source = self._working.get(CONF_SOURCE_ENTITY)
        current = self._working.get(CONF_SOURCE_ATTRIBUTE)
        options = _source_attribute_options(self.hass, source, current)

        if user_input is not None:
            source_attribute = (user_input.get(CONF_SOURCE_ATTRIBUTE) or "").strip()
            errors = _validate_source_attribute_pick(source_attribute)
            if not errors:
                self._working[CONF_SOURCE_ATTRIBUTE] = source_attribute
                return self._save()
        elif not options:
            errors["base"] = "source_has_no_attributes"

        return self.async_show_form(
            step_id="edit_source_attribute_pick",
            data_schema=_source_attribute_pick_schema(options, current),
            errors=errors,
            description_placeholders={"source": _friendly_name(self.hass, source)},
        )

    async def async_step_edit_attribute(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit attribute mode: choose which attribute to enforce."""

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
        if user_input is not None:
            self._working[CONF_ATTRIBUTE] = user_input[CONF_ATTRIBUTE]
            return await self.async_step_edit_attribute_params()

        return self.async_show_form(
            step_id="edit_attribute",
            data_schema=_attribute_choice_schema(
                attr_options,
                current_attr,
            ),
        )

    async def async_step_edit_attribute_params(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit attribute mode: parameters for the chosen attribute."""
        errors: dict[str, str] = {}
        attr_options = _attributes_for_entities(
            self._working.get(CONF_TARGET_ENTITIES, [])
        )
        selected_attr = self._working.get(
            CONF_ATTRIBUTE, attr_options[0] if attr_options else None
        )
        self._working[CONF_ATTRIBUTE] = selected_attr

        if user_input is not None:
            delay = _coerce_delay(user_input.get(CONF_DELAY_SECONDS))
            if delay is None:
                errors[CONF_DELAY_SECONDS] = "invalid_delay"
            elif selected_attr == ATTR_RGB_COLOR:
                target_value = _coerce_rgb_color(user_input.get(CONF_TARGET_VALUE))
                if target_value is None:
                    errors[CONF_TARGET_VALUE] = "invalid_rgb_color"
                else:
                    self._working[CONF_OPERATOR] = None
                    self._working[CONF_THRESHOLD] = None
                    self._working[CONF_TARGET_VALUE] = target_value
                    self._working[CONF_DELAY_SECONDS] = delay
                    return self._save()
            elif selected_attr == ATTR_COLOR_TEMP_KELVIN:
                target_value = _coerce_color_temp_kelvin(
                    user_input.get(CONF_TARGET_VALUE)
                )
                if target_value is None:
                    errors[CONF_TARGET_VALUE] = "invalid_color_temp_kelvin"
                else:
                    self._working[CONF_OPERATOR] = None
                    self._working[CONF_THRESHOLD] = None
                    self._working[CONF_TARGET_VALUE] = target_value
                    self._working[CONF_DELAY_SECONDS] = delay
                    return self._save()
            else:
                try:
                    threshold = float(user_input[CONF_THRESHOLD])
                    target_value = float(user_input[CONF_TARGET_VALUE])
                except (
                    TypeError,
                    ValueError,
                ):  # pragma: no cover — vol.Coerce(float) in schema prevents this
                    errors["base"] = "invalid_threshold"
                else:
                    self._working[CONF_OPERATOR] = user_input[CONF_OPERATOR]
                    self._working[CONF_THRESHOLD] = threshold
                    self._working[CONF_TARGET_VALUE] = target_value
                    self._working[CONF_DELAY_SECONDS] = delay
                    return self._save()

        return self.async_show_form(
            step_id="edit_attribute_params",
            data_schema=_attribute_schema(
                selected_attr,
                target_value_default=self._working.get(CONF_TARGET_VALUE),
                threshold_default=self._working.get(CONF_THRESHOLD, 0),
                operator_default=self._working.get(
                    CONF_OPERATOR, SUPPORTED_OPERATORS[0]
                ),
                delay_default=self._working.get(
                    CONF_DELAY_SECONDS, DEFAULT_DELAY_SECONDS
                ),
            ),
            errors=errors,
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
        """Edit debounce + rate limit (+ verify for state modes)."""
        verify_applicable = self._working.get(CONF_MODE) in (
            MODE_FIXED_STATE,
            MODE_DYNAMIC_STATE,
        )
        if user_input is not None:
            self._working[CONF_DEBOUNCE_ENABLED] = bool(
                user_input[CONF_DEBOUNCE_ENABLED]
            )
            self._working[CONF_DEBOUNCE_SECONDS] = int(
                user_input[CONF_DEBOUNCE_SECONDS]
            )
            # 0 disables loop protection (the engine and parse_rule_config honor it,
            # and the slider can reach 0). The numeric field's own min is
            # MIN_RATE_LIMIT (1), so a dedicated toggle expresses the disabled state —
            # mirroring the create flow — instead of clamping 0 back up to 1.
            if user_input.get(CONF_RATE_LIMIT_ENABLED, True):
                self._working[CONF_MAX_ENFORCEMENTS_PER_MINUTE] = int(
                    user_input[CONF_MAX_ENFORCEMENTS_PER_MINUTE]
                )
            else:
                self._working[CONF_MAX_ENFORCEMENTS_PER_MINUTE] = 0
            # Verify toggle only when shown (state modes); untouched on attribute rules.
            if verify_applicable:
                self._working[CONF_VERIFY_ENABLED] = bool(
                    user_input.get(CONF_VERIFY_ENABLED, False)
                )
            return self._save()

        current_rate = self._working.get(
            CONF_MAX_ENFORCEMENTS_PER_MINUTE, DEFAULT_MAX_ENFORCEMENTS_PER_MINUTE
        )
        fields: dict[Any, Any] = {
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
                CONF_RATE_LIMIT_ENABLED,
                default=current_rate not in (None, 0),
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_MAX_ENFORCEMENTS_PER_MINUTE,
                # Never seed below the selector min; 0 (disabled) is carried by
                # the toggle above, so show the default rate when disabled.
                default=current_rate
                if current_rate
                else DEFAULT_MAX_ENFORCEMENTS_PER_MINUTE,
            ): _rate_selector(),
        }
        if verify_applicable:
            fields[
                vol.Required(
                    CONF_VERIFY_ENABLED,
                    default=self._working.get(CONF_VERIFY_ENABLED, False),
                )
            ] = selector.BooleanSelector()
        return self.async_show_form(
            step_id="edit_advanced", data_schema=vol.Schema(fields)
        )
