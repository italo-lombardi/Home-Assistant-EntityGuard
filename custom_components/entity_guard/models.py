"""Data models for the Entity Guard integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    CONF_ATTRIBUTE,
    CONF_DEBOUNCE_ENABLED,
    CONF_DEBOUNCE_SECONDS,
    CONF_DELAY_OFF_SECONDS,
    CONF_DELAY_ON_SECONDS,
    CONF_DELAY_SECONDS,
    CONF_FLAG_ENTITY,
    CONF_FLAG_MATCH_STATE,
    CONF_FLAGS,
    CONF_FOLLOW_INVERT,
    CONF_MAX_ENFORCEMENTS_PER_MINUTE,
    CONF_MODE,
    CONF_OPERATOR,
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
    DEFAULT_DEBOUNCE_ENABLED,
    DEFAULT_DEBOUNCE_SECONDS,
    DEFAULT_DELAY_SECONDS,
    DEFAULT_MAX_ENFORCEMENTS_PER_MINUTE,
    DEFAULT_TARGET_STATE,
    DEFAULT_TRIGGER_STATES,
    MAX_COLOR_TEMP_KELVIN,
    MAX_DEBOUNCE_SECONDS,
    MAX_DELAY_SECONDS,
    MAX_RATE_LIMIT,
    MIN_COLOR_TEMP_KELVIN,
    MIN_DEBOUNCE_SECONDS,
    MIN_DELAY_SECONDS,
    MODE_DYNAMIC_STATE,
    MODE_FIXED_STATE,
    SOURCE_VALUE_FROM_STATE,
)


@dataclass
class Flag:
    """A single flag condition (entity must equal match_state)."""

    entity: str
    match_state: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict."""
        return {CONF_FLAG_ENTITY: self.entity, CONF_FLAG_MATCH_STATE: self.match_state}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Flag:
        """Deserialize from dict."""
        try:
            return cls(
                entity=data[CONF_FLAG_ENTITY],
                match_state=data[CONF_FLAG_MATCH_STATE],
            )
        except KeyError as err:
            raise ValueError(f"Flag dict missing required key {err}: {data!r}") from err


@dataclass
class RuleConfig:
    """Typed view of a rule config entry."""

    name: str
    unique_id: str
    target_entities: list[str]
    mode: str
    trigger_states: list[str]
    target_state: str
    delay_seconds: int
    attribute: str | None
    operator: str | None
    threshold: float | None
    target_value: Any | None
    flags: list[Flag]
    debounce_enabled: bool
    debounce_seconds: int
    max_enforcements_per_minute: int
    safety_acknowledged: bool
    # Dynamic modes. source_entity is the followed entity (None for fixed rules).
    # dynamic_state: follow_invert flips the mirrored on/off. dynamic_attribute:
    # source_value_from selects whether the live value is the source's state or a
    # named attribute (source_attribute); CONF_ATTRIBUTE holds the TARGET attribute.
    source_entity: str | None = None
    follow_invert: bool = False
    source_value_from: str = "state"
    source_attribute: str | None = None
    # dynamic_state only: per-direction delay overrides. None = fall back to
    # delay_seconds; an explicit 0 means "immediate" for that direction (distinct
    # from unset). Non-dynamic-state modes ignore these. See effective_delay.
    delay_on_seconds: int | None = None
    delay_off_seconds: int | None = None

    def effective_delay(self, desired_on: bool | None) -> int:
        """Delay (seconds) to use for driving a target toward `desired_on`.

        dynamic_state may override the shared delay_seconds per direction:
        desired on → delay_on_seconds, desired off → delay_off_seconds. An unset
        override (None) falls back to delay_seconds; an explicit 0 is honoured as
        immediate.

        Gated on mode AND a non-None direction: any non-dynamic_state mode (and
        any case where the desired direction is unknown — e.g. source offline)
        returns delay_seconds, identical to pre-feature behaviour. The mode gate
        matters because the private _follow_desired_on() the engine passes DOES
        compute a value for dynamic_attribute (it has a source_entity) — only the
        mode check keeps the overrides dynamic_state-exclusive.
        """
        if self.mode != MODE_DYNAMIC_STATE or desired_on is None:
            return self.delay_seconds
        override = self.delay_on_seconds if desired_on else self.delay_off_seconds
        return override if override is not None else self.delay_seconds


@dataclass
class RuleRuntimeState:
    """Mutable per-rule runtime state. Lock is intentionally not persisted."""

    cooldowns: dict[str, datetime] = field(default_factory=dict)
    enforcement_count_today: int = 0
    enforcement_count_total: int = 0
    last_enforced: datetime | None = None
    suppressed_until: datetime | None = None
    today_reset_date: date | None = None
    rate_limit_window: list[datetime] = field(default_factory=list)
    enabled: bool = True
    suppression_reason: str | None = None
    consecutive_errors: int = 0
    last_error: str | None = None
    # Successful enforcements since entering STATUS_ERROR. Transient: not persisted —
    # ERROR is sticky until recovery, and after a restart the rule re-enters fresh state.
    consecutive_success_count: int = 0
    # Who/what triggered the last enforcement. user_id is set when a UI user caused the
    # triggering state change; None means an automation, script, or integration did it.
    # last_triggered_entity is the target entity that was enforced last.
    # Both transient — not persisted (change on every enforcement).
    last_triggered_user_id: str | None = None
    last_triggered_entity: str | None = None
    # Timestamp of when the persistent total counter window opened. Backfilled from
    # ConfigEntry.created_at on first setup after upgrade; reset to now() whenever
    # async_clear_history zeros enforcement_count_total. None only until the first
    # backfill runs (e.g. entry without created_at on old HA cores).
    counter_total_since: datetime | None = None
    reentrance_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def parse_rule_config(entry: ConfigEntry) -> RuleConfig:
    """Build a RuleConfig from a HA ConfigEntry's data + options."""
    # Options override data so options-flow edits take effect without recreating the entry.
    raw: dict[str, Any] = {**entry.data, **(entry.options or {})}

    flags_raw = raw.get(CONF_FLAGS, []) or []
    if not isinstance(flags_raw, list):
        flags_raw = []
    flags = []
    for _f in flags_raw:
        try:
            flags.append(Flag.from_dict(_f))
        except (ValueError, TypeError) as err:
            raise ValueError(
                f"Corrupt flag entry in rule '{raw.get(CONF_RULE_NAME, 'unknown')}': {err!r} — data: {_f!r}"
            ) from err

    _delay = max(
        MIN_DELAY_SECONDS,
        min(
            MAX_DELAY_SECONDS,
            _to_int_or_default(
                raw.get(CONF_DELAY_SECONDS, DEFAULT_DELAY_SECONDS),
                DEFAULT_DELAY_SECONDS,
            ),
        ),
    )

    def _bounded_delay_or_none(key: str) -> int | None:
        """Parse an optional per-direction delay: None when the key is absent,
        else a bounded int (0 is a valid 'immediate' value, kept distinct from
        unset). Used for the dynamic_state delay_on/off overrides."""
        if raw.get(key) is None:
            return None
        return max(
            MIN_DELAY_SECONDS,
            min(
                MAX_DELAY_SECONDS,
                _to_int_or_default(raw.get(key), DEFAULT_DELAY_SECONDS),
            ),
        )

    _delay_on = _bounded_delay_or_none(CONF_DELAY_ON_SECONDS)
    _delay_off = _bounded_delay_or_none(CONF_DELAY_OFF_SECONDS)
    _debounce = max(
        MIN_DEBOUNCE_SECONDS,
        min(
            MAX_DEBOUNCE_SECONDS,
            _to_int_or_default(
                raw.get(CONF_DEBOUNCE_SECONDS, DEFAULT_DEBOUNCE_SECONDS),
                DEFAULT_DEBOUNCE_SECONDS,
            ),
        ),
    )
    _max_enf = max(
        0,
        min(
            MAX_RATE_LIMIT,
            _to_int_or_default(
                raw.get(
                    CONF_MAX_ENFORCEMENTS_PER_MINUTE,
                    DEFAULT_MAX_ENFORCEMENTS_PER_MINUTE,
                ),
                DEFAULT_MAX_ENFORCEMENTS_PER_MINUTE,
            ),
        ),
    )
    return RuleConfig(
        name=raw.get(CONF_RULE_NAME, entry.title),
        unique_id=raw.get(CONF_RULE_ID, entry.entry_id),
        target_entities=list(raw.get(CONF_TARGET_ENTITIES, []) or []),
        mode=raw.get(CONF_MODE, MODE_FIXED_STATE),
        trigger_states=list(raw.get(CONF_TRIGGER_STATES, DEFAULT_TRIGGER_STATES)),
        target_state=raw.get(CONF_TARGET_STATE, DEFAULT_TARGET_STATE),
        delay_seconds=_delay,
        attribute=raw.get(CONF_ATTRIBUTE),
        operator=raw.get(CONF_OPERATOR),
        threshold=_parse_threshold(raw.get(CONF_ATTRIBUTE), raw.get(CONF_THRESHOLD)),
        target_value=_parse_target_value(
            raw.get(CONF_ATTRIBUTE), raw.get(CONF_TARGET_VALUE)
        ),
        flags=flags,
        debounce_enabled=bool(raw.get(CONF_DEBOUNCE_ENABLED, DEFAULT_DEBOUNCE_ENABLED)),
        debounce_seconds=_debounce,
        max_enforcements_per_minute=_max_enf,
        safety_acknowledged=bool(raw.get(CONF_SAFETY_ACKNOWLEDGED, False)),
        # Dynamic modes: source entity + how to read its value. Defaults keep fixed
        # rules identical. source_attribute only meaningful when
        # source_value_from == attribute (dynamic_attribute).
        source_entity=raw.get(CONF_SOURCE_ENTITY) or None,
        follow_invert=bool(raw.get(CONF_FOLLOW_INVERT, False)),
        source_value_from=raw.get(CONF_SOURCE_VALUE_FROM, SOURCE_VALUE_FROM_STATE),
        source_attribute=raw.get(CONF_SOURCE_ATTRIBUTE) or None,
        delay_on_seconds=_delay_on,
        delay_off_seconds=_delay_off,
    )


def _to_float_or_none(value: Any) -> float | None:
    """Coerce value to float, returning None on failure or None input."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_rgb_color_or_none(value: Any) -> list[int] | None:
    """Coerce value to an RGB list, returning None on failure."""
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    try:
        rgb = [int(channel) for channel in value]
    except (TypeError, ValueError):
        return None
    if any(channel < 0 or channel > 255 for channel in rgb):
        return None
    return rgb


def _parse_threshold(attribute: Any, value: Any) -> float | None:
    """Parse threshold, skipping it for color-match attributes."""
    if attribute in (ATTR_RGB_COLOR, ATTR_COLOR_TEMP_KELVIN):
        return None
    return _to_float_or_none(value)


def _parse_target_value(attribute: Any, value: Any) -> Any | None:
    """Parse target_value based on the configured attribute."""
    if attribute == ATTR_RGB_COLOR:
        return _to_rgb_color_or_none(value)
    if attribute == ATTR_COLOR_TEMP_KELVIN:
        return _to_kelvin_or_none(value)
    return _to_float_or_none(value)


def _to_int_or_default(value: Any, default: int) -> int:
    """Coerce value to int, returning default on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_int_or_none(value: Any) -> int | None:
    """Coerce value to int, returning None on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_kelvin_or_none(value: Any) -> int | None:
    """Coerce to an int Kelvin value within the supported range, else None."""
    kelvin = _to_int_or_none(value)
    if kelvin is None:
        return None
    if kelvin < MIN_COLOR_TEMP_KELVIN or kelvin > MAX_COLOR_TEMP_KELVIN:
        return None
    return kelvin
