"""Data models for the Entity Guard integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_ATTRIBUTE,
    CONF_DEBOUNCE_ENABLED,
    CONF_DEBOUNCE_SECONDS,
    CONF_DELAY_SECONDS,
    CONF_FLAG_ENTITY,
    CONF_FLAG_MATCH_STATE,
    CONF_FLAGS,
    CONF_MAX_ENFORCEMENTS_PER_MINUTE,
    CONF_MODE,
    CONF_OPERATOR,
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
    DEFAULT_MAX_ENFORCEMENTS_PER_MINUTE,
    DEFAULT_TARGET_STATE,
    DEFAULT_TRIGGER_STATES,
    MAX_DEBOUNCE_SECONDS,
    MAX_DELAY_SECONDS,
    MAX_RATE_LIMIT,
    MIN_DEBOUNCE_SECONDS,
    MIN_DELAY_SECONDS,
    MODE_STATE,
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
    def from_dict(cls, data: dict[str, Any]) -> "Flag":
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
    target_value: float | None
    flags: list[Flag]
    debounce_enabled: bool
    debounce_seconds: int
    max_enforcements_per_minute: int
    safety_acknowledged: bool


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
        mode=raw.get(CONF_MODE, MODE_STATE),
        trigger_states=list(raw.get(CONF_TRIGGER_STATES, DEFAULT_TRIGGER_STATES)),
        target_state=raw.get(CONF_TARGET_STATE, DEFAULT_TARGET_STATE),
        delay_seconds=_delay,
        attribute=raw.get(CONF_ATTRIBUTE),
        operator=raw.get(CONF_OPERATOR),
        threshold=_to_float_or_none(raw.get(CONF_THRESHOLD)),
        target_value=_to_float_or_none(raw.get(CONF_TARGET_VALUE)),
        flags=flags,
        debounce_enabled=bool(raw.get(CONF_DEBOUNCE_ENABLED, DEFAULT_DEBOUNCE_ENABLED)),
        debounce_seconds=_debounce,
        max_enforcements_per_minute=_max_enf,
        safety_acknowledged=bool(raw.get(CONF_SAFETY_ACKNOWLEDGED, False)),
    )


def _to_float_or_none(value: Any) -> float | None:
    """Coerce value to float, returning None on failure or None input."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int_or_default(value: Any, default: int) -> int:
    """Coerce value to int, returning default on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
