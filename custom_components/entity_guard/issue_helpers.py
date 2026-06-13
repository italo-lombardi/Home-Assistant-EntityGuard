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


async def async_check_missing_flag_entities(hass: HomeAssistant, entry_id: str) -> None:
    """Check if any flag entities are missing and create repair issues."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return

    try:
        config = parse_rule_config(entry)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Could not parse config for entry %s", entry_id)
        return

    if not config.flags:
        _LOGGER.debug("No flags configured for rule %s", entry_id)
        return

    ent_reg = er.async_get(hass)
    missing_flags: list[str] = [
        flag.entity for flag in config.flags if ent_reg.async_get(flag.entity) is None
    ]

    issue_id = f"{entry_id}_missing_flags"
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
