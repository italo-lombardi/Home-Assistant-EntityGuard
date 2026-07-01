"""Tests for Entity Guard services."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.entity_guard.const import DOMAIN
from custom_components.entity_guard.services import (
    _iter_engines,
    _resolve_engine,
    async_register_services,
    async_unload_services,
)


def _make_engine(unique_id="rule-1", name="Test Rule", status="armed", enabled=True):
    engine = MagicMock()
    engine.config.unique_id = unique_id
    engine.config.name = name
    engine.config.target_entities = ["light.x"]
    engine.config.mode = "state"
    engine.current_status.return_value = status
    engine.state.enabled = enabled
    engine.state.suppressed_until = None
    engine.async_suppress = AsyncMock()
    engine.async_unsuppress = AsyncMock()
    engine.async_clear_history = AsyncMock()
    engine.async_reset_cooldowns = AsyncMock()
    engine.set_enabled = MagicMock()
    return engine


def _inject_engines(hass: HomeAssistant, *engines):
    hass.data.setdefault(DOMAIN, {})["engines"] = {
        e.config.unique_id: e for e in engines
    }


# ---------------------------------------------------------------------------
# _iter_engines / _resolve_engine
# ---------------------------------------------------------------------------


def test_iter_engines_empty(hass: HomeAssistant):
    hass.data[DOMAIN] = {"engines": {}}
    assert _iter_engines(hass) == []


def test_iter_engines_returns_engines(hass: HomeAssistant):
    eng = _make_engine()
    _inject_engines(hass, eng)
    assert eng in _iter_engines(hass)


def test_resolve_engine_by_id(hass: HomeAssistant):
    eng = _make_engine(unique_id="abc-123")
    _inject_engines(hass, eng)
    assert _resolve_engine(hass, "abc-123") is eng


def test_resolve_engine_by_name(hass: HomeAssistant):
    eng = _make_engine(name="Night Lights")
    _inject_engines(hass, eng)
    assert _resolve_engine(hass, "Night Lights") is eng


def test_resolve_engine_skips_non_matching_entry(hass: HomeAssistant):
    """_resolve_engine iterates past a non-matching engine to find the target (66->64 branch)."""
    other = _make_engine(unique_id="other-id", name="Other Rule")
    target = _make_engine(unique_id="target-id", name="Target Rule")
    hass.data[DOMAIN] = {"engines": {"e1": other, "e2": target}}
    assert _resolve_engine(hass, "target-id") is target


def test_resolve_engine_not_found(hass: HomeAssistant):
    _inject_engines(hass)
    with pytest.raises(ServiceValidationError):
        _resolve_engine(hass, "nonexistent")


# ---------------------------------------------------------------------------
# Service registration / unload
# ---------------------------------------------------------------------------


async def test_register_services(hass: HomeAssistant):
    hass.data.setdefault(DOMAIN, {})["engines"] = {}
    await async_register_services(hass)
    assert hass.services.has_service(DOMAIN, "suppress")
    assert hass.services.has_service(DOMAIN, "unsuppress")
    assert hass.services.has_service(DOMAIN, "clear_history")
    assert hass.services.has_service(DOMAIN, "list_rules")
    assert hass.services.has_service(DOMAIN, "panic_stop")


async def test_register_services_idempotent(hass: HomeAssistant):
    hass.data.setdefault(DOMAIN, {})["engines"] = {}
    await async_register_services(hass)
    await async_register_services(hass)  # second call should not error


async def test_unload_services(hass: HomeAssistant):
    hass.data.setdefault(DOMAIN, {})["engines"] = {}
    await async_register_services(hass)
    async_unload_services(hass)
    assert not hass.services.has_service(DOMAIN, "suppress")
    assert not hass.services.has_service(DOMAIN, "panic_stop")


# ---------------------------------------------------------------------------
# handle_suppress
# ---------------------------------------------------------------------------


async def test_suppress_service(hass: HomeAssistant):
    eng = _make_engine()
    _inject_engines(hass, eng)
    await async_register_services(hass)
    await hass.services.async_call(
        DOMAIN,
        "suppress",
        {"rule_id": "rule-1", "duration_minutes": 30},
        blocking=True,
    )
    eng.async_suppress.assert_awaited_once_with(duration_minutes=30, user_id=None)


async def test_suppress_service_unknown_rule(hass: HomeAssistant):
    _inject_engines(hass)
    await async_register_services(hass)
    with pytest.raises(Exception):
        await hass.services.async_call(
            DOMAIN,
            "suppress",
            {"rule_id": "does-not-exist", "duration_minutes": 10},
            blocking=True,
        )


# ---------------------------------------------------------------------------
# handle_unsuppress
# ---------------------------------------------------------------------------


async def test_unsuppress_service(hass: HomeAssistant):
    eng = _make_engine()
    _inject_engines(hass, eng)
    await async_register_services(hass)
    await hass.services.async_call(
        DOMAIN, "unsuppress", {"rule_id": "rule-1"}, blocking=True
    )
    eng.async_unsuppress.assert_awaited_once()


# ---------------------------------------------------------------------------
# handle_clear_history
# ---------------------------------------------------------------------------


async def test_clear_history_service(hass: HomeAssistant):
    eng = _make_engine()
    _inject_engines(hass, eng)
    await async_register_services(hass)
    await hass.services.async_call(
        DOMAIN, "clear_history", {"rule_id": "rule-1"}, blocking=True
    )
    eng.async_clear_history.assert_awaited_once()


# ---------------------------------------------------------------------------
# handle_list_rules
# ---------------------------------------------------------------------------


async def test_list_rules_service(hass: HomeAssistant):
    eng = _make_engine(unique_id="r1", name="Night Lights", status="conditional")
    _inject_engines(hass, eng)
    await async_register_services(hass)
    result = await hass.services.async_call(
        DOMAIN, "list_rules", {}, blocking=True, return_response=True
    )
    assert result is not None
    rules = result["rules"]
    assert len(rules) == 1
    assert rules[0]["rule_id"] == "r1"
    assert rules[0]["name"] == "Night Lights"
    assert rules[0]["status"] == "conditional"


# ---------------------------------------------------------------------------
# handle_panic_stop
# ---------------------------------------------------------------------------


async def test_panic_stop_disables_all(hass: HomeAssistant):
    eng1 = _make_engine(unique_id="r1")
    eng2 = _make_engine(unique_id="r2")
    _inject_engines(hass, eng1, eng2)
    await async_register_services(hass)
    await hass.services.async_call(DOMAIN, "panic_stop", {}, blocking=True)
    eng1.set_enabled.assert_called_once_with(False)
    eng2.set_enabled.assert_called_once_with(False)
    eng1.async_suppress.assert_awaited_once()
    eng2.async_suppress.assert_awaited_once()
    assert hass.data[DOMAIN]["hub_master_enabled"] is False


async def test_panic_stop_persists_hub_master_disabled(hass: HomeAssistant):
    """panic_stop must persist master_enabled=False to hub entry options."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.entity_guard.const import CONF_ENTRY_TYPE, ENTRY_TYPE_HUB

    hub_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_HUB},
        options={"master_enabled": True},
        title="Hub",
    )
    hub_entry.add_to_hass(hass)
    _inject_engines(hass)
    await async_register_services(hass)
    await hass.services.async_call(DOMAIN, "panic_stop", {}, blocking=True)
    assert hub_entry.options.get("master_enabled") is False


async def test_panic_stop_persists_per_rule_disabled(hass: HomeAssistant):
    """panic_stop must write enabled=False to each rule's config entry options."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.entity_guard.const import CONF_ENTRY_TYPE, ENTRY_TYPE_RULE

    rule_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_RULE},
        options={},
        title="Rule",
    )
    rule_entry.add_to_hass(hass)
    eng = _make_engine(unique_id="r1")
    hass.data.setdefault(DOMAIN, {})["engines"] = {rule_entry.entry_id: eng}
    await async_register_services(hass)
    await hass.services.async_call(DOMAIN, "panic_stop", {}, blocking=True)
    assert rule_entry.options.get("enabled") is False
