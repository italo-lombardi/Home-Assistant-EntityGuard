"""Repair flows for Entity Guard integration."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)

from .const import DOMAIN
from .models import parse_rule_config

_LOGGER = logging.getLogger(__name__)

ISSUE_FLAG_ENTITY_MISSING = "flag_entity_missing"
ISSUE_TARGET_ENTITY_MISSING = "target_entity_missing"


def missing_flags_issue_id(entry_id: str) -> str:
    """Return the repair-issue ID for a rule's missing flag entities."""
    return f"{entry_id}_missing_flags"


def missing_targets_issue_id(entry_id: str) -> str:
    """Return the repair-issue ID for a rule's missing target entities."""
    return f"{entry_id}_missing_targets"


async def async_check_missing_flag_entities(hass: HomeAssistant, entry_id: str) -> None:
    """Check if any flag entities are missing and create repair issues."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return

    try:
        config = parse_rule_config(entry)
    except Exception:
        _LOGGER.exception("Could not parse config for entry %s", entry_id)
        return

    issue_id = missing_flags_issue_id(entry_id)
    if not config.flags:
        _LOGGER.debug("No flags configured for rule %s", entry_id)
        async_delete_issue(hass, DOMAIN, issue_id)
        return

    ent_reg = er.async_get(hass)
    missing_flags: list[str] = [
        flag.entity for flag in config.flags if ent_reg.async_get(flag.entity) is None
    ]

    if missing_flags:
        _LOGGER.warning(
            "Flag entities missing for rule '%s': %s",
            entry.title,
            ", ".join(missing_flags),
        )
        async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=IssueSeverity.WARNING,
            translation_key=ISSUE_FLAG_ENTITY_MISSING,
            translation_placeholders={
                "rule_name": entry.title,
                "missing_entities": ", ".join(missing_flags),
            },
        )
    else:
        _LOGGER.debug("Flag validation passed for rule %s", entry_id)
        async_delete_issue(hass, DOMAIN, issue_id)


async def async_check_missing_target_entities(
    hass: HomeAssistant, entry_id: str
) -> None:
    """Check if any TARGET entities are missing and create/clear a repair issue.

    A guarded target that is renamed or removed leaves the rule watching a dead
    entity_id that never fires again — the rule silently stops enforcing (worst
    case for a kill-switch rule). Surface a WARNING repair issue so the user
    re-points or recreates the rule; we deliberately do NOT auto-rewrite the
    config (a rename often keeps the entity, and silently re-pointing a
    kill-switch at the wrong entity is worse than a visible repair). Mirrors
    async_check_missing_flag_entities; self-clearing.
    """
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return

    try:
        config = parse_rule_config(entry)
    except Exception:
        _LOGGER.exception("Could not parse config for entry %s", entry_id)
        return

    issue_id = missing_targets_issue_id(entry_id)
    if not config.target_entities:
        _LOGGER.debug("No target entities configured for rule %s", entry_id)
        async_delete_issue(hass, DOMAIN, issue_id)
        return

    ent_reg = er.async_get(hass)
    missing_targets: list[str] = [
        target for target in config.target_entities if ent_reg.async_get(target) is None
    ]

    if missing_targets:
        _LOGGER.warning(
            "Target entities missing for rule '%s': %s",
            entry.title,
            ", ".join(missing_targets),
        )
        async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=IssueSeverity.WARNING,
            translation_key=ISSUE_TARGET_ENTITY_MISSING,
            translation_placeholders={
                "rule_name": entry.title,
                "missing_entities": ", ".join(missing_targets),
            },
        )
    else:
        _LOGGER.debug("Target validation passed for rule %s", entry_id)
        async_delete_issue(hass, DOMAIN, issue_id)
