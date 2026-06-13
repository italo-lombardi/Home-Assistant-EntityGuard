"""Tests for Entity Guard button platform."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.entity_guard.const import (
    CONF_ENTRY_TYPE,
    DOMAIN,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_RULE,
)
from custom_components.entity_guard.button import (
    EntityGuardClearHistoryButton,
    EntityGuardClearSuppressionButton,
    EntityGuardResetButton,
    EntityGuardTestEnforceButton,
)


def _make_engine():
    engine = MagicMock()
    engine.config.unique_id = "test-uid"
    engine.async_reset_cooldowns = AsyncMock()
    engine.async_test_enforce = AsyncMock()
    engine.async_unsuppress = AsyncMock()
    engine.async_clear_history = AsyncMock()
    return engine


def _make_rule_entry():
    return MockConfigEntry(
        domain=DOMAIN, data={CONF_ENTRY_TYPE: ENTRY_TYPE_RULE}, title="Rule"
    )


# ---------------------------------------------------------------------------
# EntityGuardResetButton
# ---------------------------------------------------------------------------


async def test_reset_button_press(hass: HomeAssistant):
    entry = _make_rule_entry()
    engine = _make_engine()
    btn = EntityGuardResetButton(entry, engine)
    await btn.async_press()
    engine.async_reset_cooldowns.assert_awaited_once()


# ---------------------------------------------------------------------------
# EntityGuardTestEnforceButton
# ---------------------------------------------------------------------------


async def test_test_enforce_button_press(hass: HomeAssistant):
    entry = _make_rule_entry()
    engine = _make_engine()
    btn = EntityGuardTestEnforceButton(entry, engine)
    await btn.async_press()
    engine.async_test_enforce.assert_awaited_once()


# ---------------------------------------------------------------------------
# EntityGuardClearSuppressionButton
# ---------------------------------------------------------------------------


async def test_clear_suppression_button_press(hass: HomeAssistant):
    entry = _make_rule_entry()
    engine = _make_engine()
    btn = EntityGuardClearSuppressionButton(entry, engine)
    await btn.async_press()
    engine.async_unsuppress.assert_awaited_once()


# ---------------------------------------------------------------------------
# EntityGuardClearHistoryButton
# ---------------------------------------------------------------------------


async def test_clear_history_button_press(hass: HomeAssistant):
    entry = _make_rule_entry()
    engine = _make_engine()
    btn = EntityGuardClearHistoryButton(entry, engine)
    await btn.async_press()
    engine.async_clear_history.assert_awaited_once()


# ---------------------------------------------------------------------------
# async_setup_entry
# ---------------------------------------------------------------------------


async def test_setup_entry_skips_hub(hass: HomeAssistant):
    from custom_components.entity_guard.button import async_setup_entry

    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_ENTRY_TYPE: ENTRY_TYPE_HUB}, title="Hub"
    )
    added = []
    await async_setup_entry(hass, entry, added.extend)
    assert added == []


async def test_setup_entry_adds_buttons(hass: HomeAssistant):
    from custom_components.entity_guard.button import async_setup_entry

    entry = _make_rule_entry()
    engine = _make_engine()
    hass.data.setdefault(DOMAIN, {})["engines"] = {entry.entry_id: engine}
    added = []
    await async_setup_entry(hass, entry, added.extend)
    assert len(added) == 4
    types = [type(s).__name__ for s in added]
    assert "EntityGuardResetButton" in types
    assert "EntityGuardTestEnforceButton" in types
    assert "EntityGuardClearSuppressionButton" in types
    assert "EntityGuardClearHistoryButton" in types
