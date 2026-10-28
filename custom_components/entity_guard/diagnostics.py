"""Diagnostics support for Entity Guard.

Produces the redacted JSON dump behind HA's "Download diagnostics" button on
the integration / device page — the first triage artifact for a bug report.
Auto-discovered by HA from this module's presence (no manifest entry needed).

The dump is assembled directly from the live RuleEngine (config + runtime
state) and the config entry, NOT by scraping the status sensor — diagnostics
must not depend on a live entity/state object. Datetimes are ISO-serialized so
the result is JSON-safe; the engine's asyncio lock is never included.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_HUB

if TYPE_CHECKING:  # pragma: no cover
    from .rule_engine import RuleEngine

# HA user UUID of whoever triggered the last enforcement — identifying, not
# triage-relevant. Entity IDs are deliberately NOT redacted: they are the whole
# point of the dump (which target/flag a rule watches), a documented tradeoff.
TO_REDACT = {"last_triggered_user_id", "last_triggered_by"}


def _iso(value: datetime | None) -> str | None:
    """ISO-format a datetime, passing through None."""
    return value.isoformat() if value is not None else None


def _config_dump(engine: RuleEngine) -> dict[str, Any]:
    """Serialize the rule's config (no datetimes here)."""
    c = engine.config
    return {
        "name": c.name,
        "unique_id": c.unique_id,
        "mode": c.mode,
        "target_entities": list(c.target_entities or []),
        "trigger_states": list(c.trigger_states or []),
        "target_state": c.target_state,
        "delay_seconds": c.delay_seconds,
        "delay_on_seconds": c.delay_on_seconds,
        "delay_off_seconds": c.delay_off_seconds,
        "attribute": c.attribute,
        "operator": c.operator,
        "threshold": c.threshold,
        "target_value": c.target_value,
        "flags": [f.to_dict() for f in (c.flags or [])],
        "debounce_enabled": c.debounce_enabled,
        "debounce_seconds": c.debounce_seconds,
        "max_enforcements_per_minute": c.max_enforcements_per_minute,
        "safety_acknowledged": c.safety_acknowledged,
        "source_entity": c.source_entity,
        "follow_invert": c.follow_invert,
        "source_value_from": c.source_value_from,
        "source_attribute": c.source_attribute,
    }


def _state_dump(engine: RuleEngine) -> dict[str, Any]:
    """Serialize the rule's runtime state — datetimes to ISO, lock excluded."""
    s = engine.state
    return {
        "status": engine.current_status(),
        "enabled": s.enabled,
        "enforcement_count_today": s.enforcement_count_today,
        "enforcement_count_total": s.enforcement_count_total,
        "skip_count_today": s.skip_count_today,
        "skip_count_total": s.skip_count_total,
        "last_enforced": _iso(s.last_enforced),
        "cooldowns": {eid: _iso(end) for eid, end in s.cooldowns.items()},
        "suppressed_until": _iso(s.suppressed_until),
        "suppression_reason": s.suppression_reason,
        "consecutive_errors": s.consecutive_errors,
        "last_error": s.last_error,
        "consecutive_success_count": s.consecutive_success_count,
        "rate_limit_window": [_iso(t) for t in s.rate_limit_window],
        "last_triggered_user_id": s.last_triggered_user_id,
        "last_triggered_entity": s.last_triggered_entity,
        "counter_total_since": _iso(s.counter_total_since),
        "target_outcomes": {
            eid: {**o, "at": _iso(o.get("at"))} for eid, o in s.target_outcomes.items()
        },
        # Derived live values (same accessors the sensors use).
        "cooldown_remaining_seconds": engine.cooldown_remaining_seconds(),
        "cooldown_ends": {
            eid: _iso(end) for eid, end in engine.cooldown_ends().items()
        },
        "pending_enforcement_due": {
            eid: _iso(due) for eid, due in engine.pending_enforcement_due().items()
        },
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> dict[str, Any]:
    """Return a redacted diagnostics dump for a config entry.

    Branches on entry type: the hub entry has no RuleEngine, so never index
    hass.data[DOMAIN]["engines"] with it — that KeyErrors. A rule entry gets the
    full config + runtime-state dump.
    """
    entry_block = {
        "entry_id": config_entry.entry_id,
        "title": config_entry.title,
        "version": config_entry.version,
        "minor_version": config_entry.minor_version,
        "source": config_entry.source,
        "entry_type": config_entry.data.get(CONF_ENTRY_TYPE),
        "data": dict(config_entry.data),
        "options": dict(config_entry.options or {}),
    }

    if config_entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_HUB:
        rule_entry_ids = [
            e.entry_id
            for e in hass.config_entries.async_entries(DOMAIN)
            if e.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_HUB
        ]
        return async_redact_data(
            {
                "entry": entry_block,
                "hub": {
                    "master_enabled": bool(
                        hass.data.get(DOMAIN, {}).get("hub_master_enabled", True)
                    ),
                    "rule_entry_ids": rule_entry_ids,
                },
            },
            TO_REDACT,
        )

    # A loaded rule entry always has its engine (set in async_setup_entry, and
    # HA only offers the Download-Diagnostics button for a loaded entry). The
    # .get guard turns the otherwise-impossible load-failure race into a clean
    # message instead of a KeyError 500.
    engine: RuleEngine | None = (
        hass.data.get(DOMAIN, {}).get("engines", {}).get(config_entry.entry_id)
    )
    if engine is None:
        return {"entry": entry_block, "error": "engine not loaded"}
    return async_redact_data(
        {
            "entry": entry_block,
            "config": _config_dump(engine),
            "state": _state_dump(engine),
        },
        TO_REDACT,
    )
