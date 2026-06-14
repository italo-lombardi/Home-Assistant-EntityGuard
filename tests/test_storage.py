"""Tests for EntityGuardStore."""

from __future__ import annotations

from datetime import datetime, date, timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.entity_guard.models import RuleRuntimeState
from custom_components.entity_guard.storage import (
    EntityGuardStore,
    _default_rule_blob,
    _parse_dt,
    _serialize_dt,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_serialize_dt_none():
    assert _serialize_dt(None) is None


def test_serialize_dt_value():
    dt = datetime(2025, 1, 15, 12, 0, 0)
    result = _serialize_dt(dt)
    assert result == dt.isoformat()


def test_parse_dt_none():
    assert _parse_dt(None) is None
    assert _parse_dt("") is None


def test_parse_dt_valid():
    dt = datetime(2025, 1, 15, 12, 0, 0, tzinfo=dt_util.UTC)
    result = _parse_dt(dt.isoformat())
    assert result is not None


def test_parse_dt_invalid():
    assert _parse_dt("not-a-date") is None


def test_parse_dt_typeerror():
    # Non-string truthy value triggers TypeError in parse_datetime → except branch
    assert _parse_dt(123) is None


def test_default_rule_blob_structure():
    blob = _default_rule_blob()
    assert blob["cooldowns"] == {}
    assert blob["enforcement_count_today"] == 0
    assert blob["enforcement_count_total"] == 0
    assert blob["last_enforced"] is None
    assert blob["suppressed_until"] is None
    assert blob["enabled"] is True
    assert blob["suppression_reason"] is None


# ---------------------------------------------------------------------------
# runtime_to_blob / blob_to_runtime
# ---------------------------------------------------------------------------


def test_runtime_to_blob_empty():
    state = RuleRuntimeState()
    blob = EntityGuardStore.runtime_to_blob(state)
    assert blob["cooldowns"] == {}
    assert blob["enforcement_count_today"] == 0
    assert blob["enforcement_count_total"] == 0
    assert blob["last_enforced"] is None
    assert blob["enabled"] is True
    assert blob["suppression_reason"] is None


def test_runtime_to_blob_with_data():
    now = datetime(2025, 6, 1, 10, 0, 0, tzinfo=dt_util.UTC)
    state = RuleRuntimeState(
        cooldowns={"light.x": now},
        enforcement_count_today=3,
        enforcement_count_total=10,
        last_enforced=now,
        suppressed_until=now + timedelta(hours=1),
        today_reset_date=now.date(),
        rate_limit_window=[now],
        enabled=False,
        suppression_reason="manual",
    )
    blob = EntityGuardStore.runtime_to_blob(state)
    assert blob["enforcement_count_today"] == 3
    assert blob["enforcement_count_total"] == 10
    assert blob["enabled"] is False
    assert blob["suppression_reason"] == "manual"
    assert "light.x" in blob["cooldowns"]
    assert len(blob["rate_limit_window"]) == 1


def test_blob_to_runtime_empty():
    blob = _default_rule_blob()
    state = EntityGuardStore.blob_to_runtime(blob)
    assert state.enabled is True
    assert state.enforcement_count_today == 0
    assert state.cooldowns == {}


def test_blob_to_runtime_with_data():
    now = datetime(2025, 6, 1, 10, 0, 0, tzinfo=dt_util.UTC)
    blob = {
        "cooldowns": {"switch.a": now.isoformat()},
        "enforcement_count_today": 5,
        "enforcement_count_total": 20,
        "last_enforced": now.isoformat(),
        "suppressed_until": (now + timedelta(minutes=30)).isoformat(),
        "today_reset_date": "2025-06-01",
        "rate_limit_window": [now.isoformat()],
        "enabled": False,
        "suppression_reason": "loop_protection",
    }
    state = EntityGuardStore.blob_to_runtime(blob)
    assert state.enforcement_count_today == 5
    assert state.enforcement_count_total == 20
    assert state.enabled is False
    assert state.suppression_reason == "loop_protection"
    assert "switch.a" in state.cooldowns
    assert len(state.rate_limit_window) == 1
    assert state.today_reset_date == date(2025, 6, 1)


def test_blob_to_runtime_bad_date():
    blob = {**_default_rule_blob(), "today_reset_date": "not-a-date"}
    state = EntityGuardStore.blob_to_runtime(blob)
    assert state.today_reset_date is None


def test_roundtrip():
    now = datetime(2025, 6, 1, 8, 30, 0, tzinfo=dt_util.UTC)
    original = RuleRuntimeState(
        cooldowns={"light.y": now},
        enforcement_count_today=2,
        enforcement_count_total=7,
        last_enforced=now,
        today_reset_date=now.date(),
        enabled=True,
        suppression_reason=None,
    )
    blob = EntityGuardStore.runtime_to_blob(original)
    restored = EntityGuardStore.blob_to_runtime(blob)
    assert restored.enforcement_count_today == original.enforcement_count_today
    assert restored.enforcement_count_total == original.enforcement_count_total
    assert restored.enabled == original.enabled
    assert "light.y" in restored.cooldowns


# ---------------------------------------------------------------------------
# EntityGuardStore async_load
# ---------------------------------------------------------------------------


async def test_async_load_none(hass: HomeAssistant):
    store = EntityGuardStore(hass)
    with patch.object(
        store._store, "async_load", new_callable=AsyncMock, return_value=None
    ):
        await store.async_load()
    assert store._data == {"version": store._data["version"], "rules": {}}


async def test_async_load_not_dict(hass: HomeAssistant):
    store = EntityGuardStore(hass)
    with patch.object(
        store._store, "async_load", new_callable=AsyncMock, return_value=[1, 2]
    ):
        await store.async_load()
    assert store._data["rules"] == {}


async def test_async_load_valid(hass: HomeAssistant):
    now = datetime(2025, 6, 1, 0, 0, 0, tzinfo=dt_util.UTC)
    blob = {
        "cooldowns": {},
        "enforcement_count_today": 1,
        "enforcement_count_total": 5,
        "last_enforced": now.isoformat(),
        "suppressed_until": None,
        "today_reset_date": None,
        "rate_limit_window": [],
        "enabled": True,
        "suppression_reason": None,
    }
    raw = {"version": 1, "rules": {"rule-1": blob}}
    store = EntityGuardStore(hass)
    with patch.object(
        store._store, "async_load", new_callable=AsyncMock, return_value=raw
    ):
        await store.async_load()
    assert "rule-1" in store._data["rules"]
    assert store._data["rules"]["rule-1"]["enforcement_count_today"] == 1


async def test_async_load_corrupt_rule_resets(hass: HomeAssistant):
    raw = {"version": 1, "rules": {"bad-rule": "not-a-dict"}}
    store = EntityGuardStore(hass)
    with patch.object(
        store._store, "async_load", new_callable=AsyncMock, return_value=raw
    ):
        await store.async_load()
    blob = store._data["rules"]["bad-rule"]
    assert blob["enforcement_count_today"] == 0


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_get_set_delete(hass: HomeAssistant):
    store = EntityGuardStore(hass)
    blob = _default_rule_blob()
    blob["enforcement_count_today"] = 3
    with patch.object(hass, "async_create_task"):
        store.set_rule_state("r1", blob)
        retrieved = store.get_rule_state("r1")
        assert retrieved["enforcement_count_today"] == 3
        store.delete_rule_state("r1")
        after_delete = store.get_rule_state("r1")
        assert after_delete["enforcement_count_today"] == 0


def test_get_missing_returns_default(hass: HomeAssistant):
    store = EntityGuardStore(hass)
    blob = store.get_rule_state("nonexistent")
    assert blob == _default_rule_blob()


def test_clear_rule_history(hass: HomeAssistant):
    store = EntityGuardStore(hass)
    with patch.object(hass, "async_create_task"):
        blob = _default_rule_blob()
        blob["enforcement_count_total"] = 99
        store.set_rule_state("r1", blob)
        store.clear_rule_history("r1")
        after = store.get_rule_state("r1")
        assert after["enforcement_count_total"] == 0


# ---------------------------------------------------------------------------
# _validate_blob bad types
# ---------------------------------------------------------------------------


def test_validate_blob_cooldowns_not_dict(hass: HomeAssistant):
    store = EntityGuardStore(hass)
    import pytest

    with pytest.raises(ValueError, match="cooldowns"):
        store._validate_blob({"cooldowns": "bad", "rate_limit_window": []})


def test_validate_blob_rate_limit_not_list(hass: HomeAssistant):
    store = EntityGuardStore(hass)
    import pytest

    with pytest.raises(ValueError, match="rate_limit_window"):
        store._validate_blob({"cooldowns": {}, "rate_limit_window": "bad"})


def test_validate_blob_not_dict(hass: HomeAssistant):
    store = EntityGuardStore(hass)
    import pytest

    with pytest.raises(ValueError, match="not a dict"):
        store._validate_blob("not-a-dict")


# ---------------------------------------------------------------------------
# async_save / async_save_now / _data_provider
# ---------------------------------------------------------------------------


async def test_async_save_now(hass: HomeAssistant):
    store = EntityGuardStore(hass)
    with patch.object(store._store, "async_save", new_callable=AsyncMock) as mock_save:
        await store.async_save_now()
    mock_save.assert_called_once()


async def test_async_save_delays(hass: HomeAssistant):
    store = EntityGuardStore(hass)
    with patch.object(store._store, "async_delay_save") as mock_delay:
        store.async_save()
    mock_delay.assert_called_once()


def test_data_provider_returns_deep_copy(hass: HomeAssistant):
    store = EntityGuardStore(hass)
    store._data["rules"]["r1"] = {"enforcement_count_today": 5}
    copy1 = store._data_provider()
    copy2 = store._data_provider()
    assert copy1 == copy2
    copy1["rules"]["r1"]["enforcement_count_today"] = 99
    assert store._data["rules"]["r1"]["enforcement_count_today"] == 5


async def test_async_migrate_logs_and_returns_raw(hass: HomeAssistant):
    """_async_migrate returns raw when from_version >= STORE_VERSION (no-op path)."""
    from custom_components.entity_guard.storage import EntityGuardStore, STORE_VERSION

    raw = {"version": STORE_VERSION, "rules": {}}
    store = EntityGuardStore(hass)
    result = await store._async_migrate(raw, STORE_VERSION)
    assert result == raw


async def test_async_migrate_raises_for_unimplemented_path(hass: HomeAssistant):
    """_async_migrate raises NotImplementedError for versions with no migration path."""
    import pytest
    from custom_components.entity_guard.storage import EntityGuardStore

    store = EntityGuardStore(hass)
    with pytest.raises(NotImplementedError, match="No migration path"):
        await store._async_migrate({"version": 0, "rules": {}}, 0)


async def test_async_load_triggers_migration(hass: HomeAssistant):
    """async_load calls _async_migrate when stored version is stale."""
    from unittest.mock import AsyncMock, patch
    from custom_components.entity_guard.storage import EntityGuardStore, STORE_VERSION

    store = EntityGuardStore(hass)
    stale_version = STORE_VERSION - 1 if STORE_VERSION > 1 else 0
    raw = {"version": stale_version, "rules": {}}

    with patch.object(store._store, "async_load", AsyncMock(return_value=raw)):
        with patch.object(
            store, "_async_migrate", AsyncMock(return_value=raw)
        ) as mock_migrate:
            await store.async_load()
            if stale_version < STORE_VERSION:
                mock_migrate.assert_awaited_once_with(raw, stale_version)
