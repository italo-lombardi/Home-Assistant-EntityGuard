"""Logbook describers for Entity Guard."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import Event, HomeAssistant, callback

from .const import (
    DOMAIN,
    EVENT_ENFORCED,
    EVENT_LOOP_DETECTED,
    EVENT_SKIPPED,
    EVENT_SUPPRESSED,
)


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, Any]]], None],
) -> None:
    """Describe Entity Guard events for the logbook."""

    @callback
    def _describe_enforced(event: Event) -> dict[str, Any]:
        data = event.data
        target_state = data.get("target") or data.get("target_state") or "target"
        entity_id = data.get("entity_id")
        rule_name = data.get("rule_name") or "Entity Guard"
        return {
            "name": rule_name,
            "message": f"enforced {target_state} on {entity_id}",
            "entity_id": entity_id,
            "domain": DOMAIN,
        }

    @callback
    def _describe_skipped(event: Event) -> dict[str, Any]:
        data = event.data
        entity_id = data.get("entity_id")
        reason = data.get("reason", "skipped")
        rule_name = data.get("rule_name") or "Entity Guard"
        return {
            "name": rule_name,
            "message": f"skipped {entity_id} ({reason})",
            "entity_id": entity_id,
            "domain": DOMAIN,
        }

    @callback
    def _describe_loop_detected(event: Event) -> dict[str, Any]:
        data = event.data
        rule_name = data.get("rule_name") or "Entity Guard"
        return {
            "name": rule_name,
            "message": "rate limit hit — rule auto-suppressed",
            "entity_id": data.get("entity_id"),
            "domain": DOMAIN,
        }

    @callback
    def _describe_suppressed(event: Event) -> dict[str, Any]:
        data = event.data
        rule_name = data.get("rule_name") or "Entity Guard"
        suppressed_until = data.get("suppressed_until")
        message = (
            f"rule suppressed until {suppressed_until}"
            if suppressed_until
            else "rule suppressed"
        )
        return {
            "name": rule_name,
            "message": message,
            "entity_id": data.get("entity_id"),
            "domain": DOMAIN,
        }

    async_describe_event(DOMAIN, EVENT_ENFORCED, _describe_enforced)
    async_describe_event(DOMAIN, EVENT_SKIPPED, _describe_skipped)
    async_describe_event(DOMAIN, EVENT_LOOP_DETECTED, _describe_loop_detected)
    async_describe_event(DOMAIN, EVENT_SUPPRESSED, _describe_suppressed)
