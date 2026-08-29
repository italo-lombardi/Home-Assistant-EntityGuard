"""Test fixtures."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.entity_guard.const import (
    CONF_ENTRY_TYPE,
    DOMAIN,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_RULE,
)


def capturing_create_task(sink=None):
    """async_create_task stand-in that closes the coroutine (no 'never awaited'
    warning) and optionally records each call in `sink`."""

    def _side_effect(coro, *args, **kwargs):
        if hasattr(coro, "close"):
            coro.close()
        if sink is not None:
            sink.append(coro)

    return _side_effect


@pytest.fixture
def hub_entry():
    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_HUB},
        title="Entity Guard Hub",
    )


@pytest.fixture
def rule_entry():
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_RULE,
            "rule_id": "test-rule-uuid",
            "rule_name": "Test Rule",
            "target_entities": ["light.test"],
            "mode": "state",
            "trigger_states": ["on"],
            "target_state": "off",
            "delay_seconds": 0,
            "flags": [],
            "debounce_enabled": False,
            "debounce_seconds": 60,
            "max_enforcements_per_minute": 10,
            "safety_acknowledged": False,
        },
        title="Test Rule",
        unique_id="test-rule-uuid",
    )


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield
