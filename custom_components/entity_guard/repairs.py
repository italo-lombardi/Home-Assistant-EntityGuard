"""Repair flows for Entity Guard integration."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.issue_registry import async_create_issue, async_delete_issue

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

ISSUE_FLAG_ENTITY_MISSING = "flag_entity_missing"


async def async_check_missing_flag_entities(hass: HomeAssistant, entry_id: str) -> None:
    """Check if any flag entities are missing and create repair issues."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return

    from .models import parse_rule_config

    try:
        config = parse_rule_config(entry)
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Could not parse config for entry %s", entry_id)
        return

    if not config.flags:
        return

    missing_flags: list[str] = []
    for flag in config.flags:
        if hass.states.get(flag.entity) is None:
            missing_flags.append(flag.entity)

    if missing_flags:
        issue_id = f"{entry_id}_missing_flags"
        async_create_issue(
            hass,
            DOMAIN,
            ISSUE_FLAG_ENTITY_MISSING,
            is_fixable=False,
            severity="warning",
            translation_key="flag_entity_missing",
            translation_placeholders={
                "rule_name": entry.title,
                "missing_entities": ", ".join(missing_flags),
            },
            issue_id=issue_id,
        )
    else:
        issue_id = f"{entry_id}_missing_flags"
        async_delete_issue(hass, DOMAIN, issue_id)

