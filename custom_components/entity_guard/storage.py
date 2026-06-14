"""Persistent storage for the Entity Guard integration."""

from __future__ import annotations

import copy
import logging
from datetime import date, datetime
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    STORAGE_KEY,
    STORE_SAVE_DELAY_SECONDS,
    STORE_VERSION,
)
from .models import RuleRuntimeState

_LOGGER = logging.getLogger(__name__)


def _default_rule_blob() -> dict[str, Any]:
    """Return an empty per-rule persisted blob."""
    return {
        "cooldowns": {},
        "enforcement_count_today": 0,
        "enforcement_count_total": 0,
        "last_enforced": None,
        "suppressed_until": None,
        "today_reset_date": None,
        "rate_limit_window": [],
        "enabled": True,
        "suppression_reason": None,
        "consecutive_errors": 0,
        "last_error": None,
    }


def _serialize_dt(value: datetime | None) -> str | None:
    """ISO-format a datetime, passing through None."""
    return value.isoformat() if value else None


def _parse_dt(value: Any) -> datetime | None:
    """Parse an ISO timestamp; return None on bad input."""
    if not value:
        return None
    try:
        return dt_util.parse_datetime(value)
    except (TypeError, ValueError):
        return None


class EntityGuardStore:
    """Wraps HA Store with per-rule blobs and corruption fallback."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the store."""
        self._hass = hass
        self._store: Store = Store(hass, STORE_VERSION, STORAGE_KEY)
        self._data: dict[str, Any] = {"version": STORE_VERSION, "rules": {}}

    async def async_load(self) -> None:
        """Load persisted state. Per-rule corruption is contained."""
        raw = await self._store.async_load()
        if raw is None:
            self._data = {"version": STORE_VERSION, "rules": {}}
            return
        if not isinstance(raw, dict):
            _LOGGER.warning(
                "Persisted root is not a dict (%s); resetting", type(raw).__name__
            )
            self._data = {"version": STORE_VERSION, "rules": {}}
            return

        raw_version = raw.get("version", 1) if isinstance(raw, dict) else 1
        if raw_version < STORE_VERSION:
            raw = await self._async_migrate(raw, raw_version)
        rules_in = raw.get("rules", {}) or {}
        rules_out: dict[str, dict[str, Any]] = {}

        for rule_id, blob in rules_in.items():
            try:
                rules_out[rule_id] = self._validate_blob(blob)
            except Exception as err:  # noqa: BLE001
                # Per spec: corruption in one rule must not crash the whole load.
                _LOGGER.warning(
                    "Corrupt persisted state for rule %s, resetting: %s", rule_id, err
                )
                rules_out[rule_id] = _default_rule_blob()
                persistent_notification.async_create(
                    self._hass,
                    f"Entity Guard could not load saved state for rule "
                    f"`{rule_id}`. Counters and cooldowns have been reset.",
                    title="Entity Guard storage error",
                    notification_id=f"{DOMAIN}_storage_{rule_id}",
                )

        self._data = {"version": STORE_VERSION, "rules": rules_out}

    async def _async_migrate(
        self, raw: dict[str, Any], from_version: int
    ) -> dict[str, Any]:
        """Migrate storage data from from_version to STORE_VERSION.

        Each supported migration step must be implemented explicitly.
        Add an elif block for each version transition before bumping STORE_VERSION.
        """
        _LOGGER.info(
            "Migrating Entity Guard storage from v%d to v%d",
            from_version,
            STORE_VERSION,
        )
        # No migrations defined yet — STORE_VERSION is still 1.
        # When bumping STORE_VERSION, add: if from_version == N: raw = _migrate_N_to_N1(raw)
        if from_version >= STORE_VERSION:
            return raw
        raise NotImplementedError(
            f"No migration path from storage v{from_version} to v{STORE_VERSION}. "
            "Implement the migration in EntityGuardStore._async_migrate before bumping STORE_VERSION."
        )

    def _validate_blob(self, blob: Any) -> dict[str, Any]:
        """Coerce a persisted blob to the canonical shape, raising on bad data."""
        if not isinstance(blob, dict):
            raise ValueError("rule blob is not a dict")

        cooldowns_raw = blob.get("cooldowns", {}) or {}
        if not isinstance(cooldowns_raw, dict):
            raise ValueError("cooldowns is not a dict")

        rate_window_raw = blob.get("rate_limit_window", []) or []
        if not isinstance(rate_window_raw, list):
            raise ValueError("rate_limit_window is not a list")

        return {
            "cooldowns": {
                eid: ts for eid, ts in cooldowns_raw.items() if isinstance(ts, str)
            },
            "enforcement_count_today": int(blob.get("enforcement_count_today", 0) or 0),
            "enforcement_count_total": int(blob.get("enforcement_count_total", 0) or 0),
            "last_enforced": blob.get("last_enforced"),
            "suppressed_until": blob.get("suppressed_until"),
            "today_reset_date": blob.get("today_reset_date"),
            "rate_limit_window": [t for t in rate_window_raw if isinstance(t, str)],
            "enabled": bool(blob.get("enabled", True)),
            "suppression_reason": (
                blob.get("suppression_reason")
                if isinstance(blob.get("suppression_reason"), str)
                else None
            ),
            "consecutive_errors": int(blob.get("consecutive_errors", 0) or 0),
            "last_error": (
                blob.get("last_error")
                if isinstance(blob.get("last_error"), str)
                else None
            ),
        }

    def _schedule_save(self) -> None:
        """Schedule a debounced save."""
        self._store.async_delay_save(self._data_provider, STORE_SAVE_DELAY_SECONDS)

    def async_save(self) -> None:
        """Schedule a debounced save (10-second delay). For immediate flush, use async_save_now()."""
        self._schedule_save()

    async def async_save_now(self) -> None:
        """Force immediate persist, bypassing the debounce window."""
        await self._store.async_save(self._data_provider())

    def _data_provider(self) -> dict[str, Any]:
        """Snapshot used by Store for the actual write."""
        return copy.deepcopy(self._data)

    def get_rule_state(self, rule_id: str) -> dict[str, Any]:
        """Return persisted blob for a rule (defaults if absent)."""
        blob = self._data["rules"].get(rule_id)
        return copy.deepcopy(blob) if blob else _default_rule_blob()

    def set_rule_state(self, rule_id: str, state: dict[str, Any]) -> None:
        """Replace the persisted blob for a rule and queue a save."""
        self._data["rules"][rule_id] = state
        self._schedule_save()

    def delete_rule_state(self, rule_id: str) -> None:
        """Remove a rule's persisted state entirely."""
        if rule_id in self._data["rules"]:
            del self._data["rules"][rule_id]
            self._schedule_save()

    def clear_rule_history(self, rule_id: str) -> None:
        """Reset counters/cooldowns for a rule but keep the slot."""
        self._data["rules"][rule_id] = _default_rule_blob()
        self._schedule_save()

    @staticmethod
    def runtime_to_blob(state: RuleRuntimeState) -> dict[str, Any]:
        """Serialize a RuleRuntimeState to a JSON-friendly blob."""
        return {
            "cooldowns": {
                eid: _serialize_dt(ts) for eid, ts in state.cooldowns.items()
            },
            "enforcement_count_today": state.enforcement_count_today,
            "enforcement_count_total": state.enforcement_count_total,
            "last_enforced": _serialize_dt(state.last_enforced),
            "suppressed_until": _serialize_dt(state.suppressed_until),
            "today_reset_date": (
                state.today_reset_date.isoformat() if state.today_reset_date else None
            ),
            "rate_limit_window": [
                _serialize_dt(t) for t in state.rate_limit_window if t
            ],
            "enabled": state.enabled,
            "suppression_reason": state.suppression_reason,
            "consecutive_errors": state.consecutive_errors,
            "last_error": state.last_error,
        }

    @staticmethod
    def blob_to_runtime(blob: dict[str, Any]) -> RuleRuntimeState:
        """Deserialize a persisted blob into a RuleRuntimeState."""
        cooldowns: dict[str, datetime] = {}
        for eid, ts in (blob.get("cooldowns") or {}).items():
            parsed = _parse_dt(ts)
            if parsed is not None:
                cooldowns[eid] = parsed

        rate_window: list[datetime] = []
        for ts in blob.get("rate_limit_window") or []:
            parsed = _parse_dt(ts)
            if parsed is not None:
                rate_window.append(parsed)

        today_reset: date | None = None
        raw_date = blob.get("today_reset_date")
        if isinstance(raw_date, str):
            try:
                today_reset = date.fromisoformat(raw_date)
            except ValueError:
                today_reset = None

        return RuleRuntimeState(
            cooldowns=cooldowns,
            enforcement_count_today=int(blob.get("enforcement_count_today", 0) or 0),
            enforcement_count_total=int(blob.get("enforcement_count_total", 0) or 0),
            last_enforced=_parse_dt(blob.get("last_enforced")),
            suppressed_until=_parse_dt(blob.get("suppressed_until")),
            today_reset_date=today_reset,
            rate_limit_window=rate_window,
            enabled=bool(blob.get("enabled", True)),
            suppression_reason=(
                blob.get("suppression_reason")
                if isinstance(blob.get("suppression_reason"), str)
                else None
            ),
            consecutive_errors=int(blob.get("consecutive_errors", 0) or 0),
            last_error=(
                blob.get("last_error")
                if isinstance(blob.get("last_error"), str)
                else None
            ),
        )
