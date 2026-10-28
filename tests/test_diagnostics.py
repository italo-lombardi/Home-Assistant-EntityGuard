"""Tests for the Entity Guard diagnostics platform."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.entity_guard.const import (
    CONF_ENTRY_TYPE,
    DOMAIN,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_RULE,
)
from custom_components.entity_guard.diagnostics import (
    async_get_config_entry_diagnostics,
)

_DT = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)


def _make_engine() -> MagicMock:
    """A fully-populated engine mock covering every field the dump reads."""
    engine = MagicMock()
    c = engine.config
    c.name = "Front door auto-lock"
    c.unique_id = "rule-uid"
    c.mode = "fixed_state"
    c.target_entities = ["lock.front_door"]
    c.trigger_states = ["unlocked"]
    c.target_state = "locked"
    c.delay_seconds = 10
    c.delay_on_seconds = None
    c.delay_off_seconds = None
    c.attribute = None
    c.operator = None
    c.threshold = None
    c.target_value = None
    c.flags = []  # keep simple; flag serialization covered via a real Flag below
    c.debounce_enabled = True
    c.debounce_seconds = 30
    c.verify_enabled = True
    c.max_enforcements_per_minute = 10
    c.safety_acknowledged = True
    c.source_entity = None
    c.follow_invert = False
    c.source_value_from = "state"
    c.source_attribute = None

    s = engine.state
    s.enabled = True
    s.enforcement_count_today = 2
    s.enforcement_count_total = 17
    s.skip_count_today = 5
    s.skip_count_total = 41
    s.last_enforced = _DT
    s.cooldowns = {"lock.front_door": _DT}
    s.suppressed_until = None
    s.suppression_reason = None
    s.consecutive_errors = 0
    s.last_error = None
    s.consecutive_success_count = 0
    s.rate_limit_window = [_DT]
    s.last_triggered_user_id = "user-uuid-secret"
    s.last_triggered_entity = "lock.front_door"
    s.counter_total_since = _DT
    s.target_outcomes = {
        "lock.front_door": {
            "outcome": "ok",
            "reason": None,
            "error": None,
            "at": _DT,
        }
    }
    engine.current_status.return_value = "armed"
    engine.cooldown_remaining_seconds.return_value = 0.0
    engine.cooldown_ends.return_value = {}
    engine.pending_enforcement_due.return_value = {}
    return engine


def _setup_rule(hass: HomeAssistant) -> tuple[MockConfigEntry, MagicMock]:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_RULE, "target_entities": ["lock.front_door"]},
        title="Front door auto-lock",
    )
    entry.add_to_hass(hass)
    engine = _make_engine()
    hass.data.setdefault(DOMAIN, {}).setdefault("engines", {})[entry.entry_id] = engine
    return entry, engine


async def test_rule_diagnostics_shape(hass: HomeAssistant):
    """A rule entry dumps entry + config + state with expected keys."""
    entry, _ = _setup_rule(hass)
    out = await async_get_config_entry_diagnostics(hass, entry)
    assert set(out) == {"entry", "config", "state"}
    assert out["entry"]["entry_id"] == entry.entry_id
    assert out["entry"]["entry_type"] == ENTRY_TYPE_RULE
    assert out["config"]["mode"] == "fixed_state"
    assert out["config"]["target_entities"] == ["lock.front_door"]
    assert out["config"]["verify_enabled"] is True
    assert out["state"]["status"] == "armed"
    assert out["state"]["enforcement_count_total"] == 17
    assert out["state"]["skip_count_today"] == 5
    assert out["state"]["skip_count_total"] == 41


async def test_rule_diagnostics_serializes_datetimes(hass: HomeAssistant):
    """All datetime fields are ISO strings (JSON-safe), never datetime objects."""
    entry, _ = _setup_rule(hass)
    out = await async_get_config_entry_diagnostics(hass, entry)
    st = out["state"]
    assert st["last_enforced"] == _DT.isoformat()
    assert st["cooldowns"]["lock.front_door"] == _DT.isoformat()
    assert st["rate_limit_window"] == [_DT.isoformat()]
    assert st["target_outcomes"]["lock.front_door"]["at"] == _DT.isoformat()
    assert st["counter_total_since"] == _DT.isoformat()


async def test_rule_diagnostics_redacts_user_id(hass: HomeAssistant):
    """The HA user UUID is redacted; entity IDs are NOT (triage signal)."""
    entry, _ = _setup_rule(hass)
    out = await async_get_config_entry_diagnostics(hass, entry)
    assert out["state"]["last_triggered_user_id"] == REDACTED
    # entity IDs intentionally preserved
    assert out["state"]["last_triggered_entity"] == "lock.front_door"
    assert out["config"]["target_entities"] == ["lock.front_door"]


async def test_rule_diagnostics_excludes_lock(hass: HomeAssistant):
    """reentrance_lock (asyncio.Lock) must never appear — it is not serializable."""
    entry, _ = _setup_rule(hass)
    out = await async_get_config_entry_diagnostics(hass, entry)
    assert "reentrance_lock" not in out["state"]


async def test_rule_diagnostics_serializes_flags(hass: HomeAssistant):
    """A real Flag serializes via to_dict() inside the config dump."""
    from custom_components.entity_guard.models import Flag

    entry, engine = _setup_rule(hass)
    engine.config.flags = [Flag(entity="person.alice", match_state="home")]
    out = await async_get_config_entry_diagnostics(hass, entry)
    assert out["config"]["flags"] == [{"entity": "person.alice", "match_state": "home"}]


async def test_hub_diagnostics_no_engine_lookup(hass: HomeAssistant):
    """The hub entry has NO RuleEngine — the fn must branch, not KeyError."""
    hub = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_HUB},
        options={"master_enabled": False},
        title="Entity Guard Hub",
    )
    hub.add_to_hass(hass)
    rule, _ = _setup_rule(hass)
    hass.data[DOMAIN]["hub_master_enabled"] = False

    out = await async_get_config_entry_diagnostics(hass, hub)
    assert set(out) == {"entry", "hub"}
    assert out["entry"]["entry_type"] == ENTRY_TYPE_HUB
    assert out["hub"]["master_enabled"] is False
    # Lists the rule entry, not the hub itself.
    assert rule.entry_id in out["hub"]["rule_entry_ids"]
    assert hub.entry_id not in out["hub"]["rule_entry_ids"]


async def test_hub_diagnostics_master_enabled_default(hass: HomeAssistant):
    """Missing hub_master_enabled in hass.data defaults to True."""
    hub = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_HUB},
        title="Entity Guard Hub",
    )
    hub.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})
    out = await async_get_config_entry_diagnostics(hass, hub)
    assert out["hub"]["master_enabled"] is True


async def test_rule_diagnostics_engine_not_loaded(hass: HomeAssistant):
    """A rule entry with no engine (load-failure race) returns a clean message,
    not a KeyError 500."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_RULE, "target_entities": ["lock.front_door"]},
        title="Orphan rule",
    )
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {}).setdefault("engines", {})  # engine absent
    out = await async_get_config_entry_diagnostics(hass, entry)
    assert out["error"] == "engine not loaded"
    assert out["entry"]["entry_type"] == ENTRY_TYPE_RULE
