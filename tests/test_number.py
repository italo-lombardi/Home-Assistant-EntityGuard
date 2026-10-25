"""Tests for Entity Guard number platform."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.entity_guard.const import (
    CONF_DEBOUNCE_SECONDS,
    CONF_DELAY_OFF_SECONDS,
    CONF_DELAY_ON_SECONDS,
    CONF_DELAY_SECONDS,
    CONF_ENTRY_TYPE,
    CONF_MAX_ENFORCEMENTS_PER_MINUTE,
    DEFAULT_DEBOUNCE_SECONDS,
    DEFAULT_DELAY_SECONDS,
    DEFAULT_MAX_ENFORCEMENTS_PER_MINUTE,
    DOMAIN,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_RULE,
    MODE_DYNAMIC_STATE,
    MODE_FIXED_STATE,
)
from custom_components.entity_guard.number import (
    EntityGuardDebounceSecondsNumber,
    EntityGuardDelayOffSecondsNumber,
    EntityGuardDelayOnSecondsNumber,
    EntityGuardDelaySecondsNumber,
    EntityGuardMaxEnforcementsNumber,
    async_setup_entry,
)


def _make_engine(delay=5, debounce=60, rate=10, mode=MODE_FIXED_STATE):
    engine = MagicMock()
    engine.config.unique_id = "test-uid"
    engine.config.delay_seconds = delay
    engine.config.debounce_seconds = debounce
    engine.config.max_enforcements_per_minute = rate
    engine.config.mode = mode
    return engine


def _make_rule_entry(**data_overrides):
    data = {
        CONF_ENTRY_TYPE: ENTRY_TYPE_RULE,
        CONF_DELAY_SECONDS: DEFAULT_DELAY_SECONDS,
        CONF_DEBOUNCE_SECONDS: DEFAULT_DEBOUNCE_SECONDS,
        CONF_MAX_ENFORCEMENTS_PER_MINUTE: DEFAULT_MAX_ENFORCEMENTS_PER_MINUTE,
    }
    data.update(data_overrides)
    return MockConfigEntry(domain=DOMAIN, data=data, title="Rule")


async def _flush_debounce(hass: HomeAssistant):
    """Advance time past the number-write debounce window and let it flush."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# EntityGuardDelaySecondsNumber
# ---------------------------------------------------------------------------


def test_delay_native_value_from_config():
    entry = _make_rule_entry()
    engine = _make_engine(delay=15)
    num = EntityGuardDelaySecondsNumber(entry, engine)
    assert num.native_value == 15.0


def test_delay_native_value_from_entry_when_config_missing():
    entry = _make_rule_entry(**{CONF_DELAY_SECONDS: 30})
    engine = _make_engine()
    engine.config.delay_seconds = None
    num = EntityGuardDelaySecondsNumber(entry, engine)
    assert num.native_value == 30.0


def test_delay_native_value_fallback_prefers_options_over_data():
    """When the engine config attr is missing, the fallback reads the merged view
    (options over data) so a persisted slider value wins over the stale data value."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_RULE, CONF_DELAY_SECONDS: 30},
        options={CONF_DELAY_SECONDS: 450},
        title="Rule",
    )
    engine = _make_engine()
    engine.config.delay_seconds = None
    num = EntityGuardDelaySecondsNumber(entry, engine)
    assert num.native_value == 450.0


async def test_delay_set_native_value(hass: HomeAssistant):
    entry = _make_rule_entry()
    entry.add_to_hass(hass)
    engine = _make_engine(delay=0)
    num = EntityGuardDelaySecondsNumber(entry, engine)
    num.hass = hass
    num._attr_available = True
    num.async_write_ha_state = MagicMock()
    await num.async_set_native_value(20.0)
    # Applied live to engine immediately.
    assert engine.config.delay_seconds == 20
    # Persisted to options on debounce so it survives reload/restart.
    await _flush_debounce(hass)
    assert entry.options[CONF_DELAY_SECONDS] == 20


# ---------------------------------------------------------------------------
# EntityGuardDebounceSecondsNumber
# ---------------------------------------------------------------------------


def test_debounce_native_value():
    entry = _make_rule_entry()
    engine = _make_engine(debounce=90)
    num = EntityGuardDebounceSecondsNumber(entry, engine)
    assert num.native_value == 90.0


async def test_debounce_set_native_value(hass: HomeAssistant):
    entry = _make_rule_entry()
    entry.add_to_hass(hass)
    engine = _make_engine(debounce=60)
    num = EntityGuardDebounceSecondsNumber(entry, engine)
    num.hass = hass
    num._attr_available = True
    num.async_write_ha_state = MagicMock()
    await num.async_set_native_value(120.0)
    assert engine.config.debounce_seconds == 120
    await _flush_debounce(hass)
    assert entry.options[CONF_DEBOUNCE_SECONDS] == 120


# ---------------------------------------------------------------------------
# EntityGuardMaxEnforcementsNumber
# ---------------------------------------------------------------------------


def test_max_enforcements_native_value():
    entry = _make_rule_entry()
    engine = _make_engine(rate=5)
    num = EntityGuardMaxEnforcementsNumber(entry, engine)
    assert num.native_value == 5.0


async def test_max_enforcements_set_native_value(hass: HomeAssistant):
    entry = _make_rule_entry()
    entry.add_to_hass(hass)
    engine = _make_engine(rate=10)
    num = EntityGuardMaxEnforcementsNumber(entry, engine)
    num.hass = hass
    num._attr_available = True
    num.async_write_ha_state = MagicMock()
    await num.async_set_native_value(3.0)
    assert engine.config.max_enforcements_per_minute == 3
    await _flush_debounce(hass)
    assert entry.options[CONF_MAX_ENFORCEMENTS_PER_MINUTE] == 3


async def test_max_enforcements_accepts_zero(hass: HomeAssistant):
    """0 disables rate limiting; the UI floor allows it and it persists as 0."""
    entry = _make_rule_entry()
    entry.add_to_hass(hass)
    engine = _make_engine(rate=10)
    num = EntityGuardMaxEnforcementsNumber(entry, engine)
    assert num.native_min_value == 0
    num.hass = hass
    num._attr_available = True
    num.async_write_ha_state = MagicMock()
    await num.async_set_native_value(0.0)
    assert engine.config.max_enforcements_per_minute == 0
    await _flush_debounce(hass)
    assert entry.options[CONF_MAX_ENFORCEMENTS_PER_MINUTE] == 0


async def test_two_sliders_within_debounce_persist_together(hass: HomeAssistant):
    """Two different sliders moved in the debounce window coalesce into one write
    that persists both keys (lost-update regression guard)."""
    entry = _make_rule_entry()
    entry.add_to_hass(hass)
    engine = _make_engine(delay=0, debounce=60)
    delay_num = EntityGuardDelaySecondsNumber(entry, engine)
    debounce_num = EntityGuardDebounceSecondsNumber(entry, engine)
    for n in (delay_num, debounce_num):
        n.hass = hass
        n._attr_available = True
        n.async_write_ha_state = MagicMock()
    await delay_num.async_set_native_value(45.0)
    await debounce_num.async_set_native_value(90.0)
    await _flush_debounce(hass)
    assert entry.options[CONF_DELAY_SECONDS] == 45
    assert entry.options[CONF_DEBOUNCE_SECONDS] == 90


async def test_remove_flushes_pending_write(hass: HomeAssistant):
    """Entity removal always flushes a pending debounced write — the value must
    survive a restart even if the slider was moved within the 2-second debounce
    window before HA shut down.  async_update_entry is safe on a tearing-down
    entry: it updates in-memory options and HA's FINAL_WRITE stage flushes disk."""
    entry = _make_rule_entry()
    entry.add_to_hass(hass)
    engine = _make_engine(delay=0)
    num = EntityGuardDelaySecondsNumber(entry, engine)
    num.hass = hass
    num.async_write_ha_state = MagicMock()
    await num.async_added_to_hass()
    await num.async_set_native_value(30.0)
    # A write is pending (not yet flushed).
    pending = hass.data[DOMAIN]["pending_number_writes"]
    assert entry.entry_id in pending
    # Fire the on_remove callback — must flush, not drop.
    num._cancel_pending_write()
    assert entry.entry_id not in pending
    assert entry.options[CONF_DELAY_SECONDS] == 30
    # A second removal with nothing queued is a no-op.
    num._cancel_pending_write()
    assert entry.options[CONF_DELAY_SECONDS] == 30


async def test_remove_while_loaded_flushes_pending_write(hass: HomeAssistant):
    """Removing ONE slider entity while the config entry is still loaded (e.g. the
    entity is disabled, or HA is stopping) flushes the coalesced write instead of
    dropping it — the pending map is shared across the entry's three sliders, so a
    silent cancel would lose a value the other two contributed."""
    from homeassistant.config_entries import ConfigEntryState

    entry = _make_rule_entry()
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    engine = _make_engine(delay=0)
    num = EntityGuardDelaySecondsNumber(entry, engine)
    num.hass = hass
    num.async_write_ha_state = MagicMock()
    await num.async_added_to_hass()
    await num.async_set_native_value(30.0)
    pending = hass.data[DOMAIN]["pending_number_writes"]
    assert entry.entry_id in pending
    # Entry still loaded → removal flushes the queued value now.
    num._cancel_pending_write()
    assert entry.entry_id not in pending
    assert entry.options[CONF_DELAY_SECONDS] == 30
    # A second removal with nothing queued is a no-op (guard early-return).
    num._cancel_pending_write()
    assert entry.options[CONF_DELAY_SECONDS] == 30


async def test_flush_noop_when_queue_empty(hass: HomeAssistant):
    """A stray flush with no queued values is a no-op (defensive guard)."""
    entry = _make_rule_entry()
    entry.add_to_hass(hass)
    engine = _make_engine(delay=0)
    num = EntityGuardDelaySecondsNumber(entry, engine)
    num.hass = hass
    num.async_write_ha_state = MagicMock()
    await num.async_set_native_value(30.0)
    # Drop the queued values but keep the entry key, then let the timer fire.
    hass.data[DOMAIN]["pending_number_writes"][entry.entry_id]["values"] = {}
    await _flush_debounce(hass)
    assert CONF_DELAY_SECONDS not in entry.options


async def test_flush_marks_entry_to_skip_reload(hass: HomeAssistant):
    """The flush that writes options must mark the entry so the update listener
    skips the reload (the value is already applied live)."""
    from custom_components.entity_guard.number import SKIP_RELOAD_KEY

    entry = _make_rule_entry()
    entry.add_to_hass(hass)
    engine = _make_engine(delay=0)
    num = EntityGuardDelaySecondsNumber(entry, engine)
    num.hass = hass
    num._attr_available = True
    num.async_write_ha_state = MagicMock()
    await num.async_set_native_value(42.0)
    await _flush_debounce(hass)
    assert entry.options[CONF_DELAY_SECONDS] == 42
    assert entry.entry_id in hass.data[DOMAIN][SKIP_RELOAD_KEY]


async def test_flush_skips_write_when_value_unchanged(hass: HomeAssistant):
    """Re-setting a slider to the value already in options writes nothing (so the
    update listener never fires for a no-op) and leaves no skip-reload mark."""
    from custom_components.entity_guard.number import SKIP_RELOAD_KEY

    entry = _make_rule_entry(**{CONF_DELAY_SECONDS: 15})
    entry.add_to_hass(hass)
    # Seed options with the value we'll "re-set" so the flush sees no change.
    hass.config_entries.async_update_entry(entry, options={CONF_DELAY_SECONDS: 15})
    engine = _make_engine(delay=15)
    num = EntityGuardDelaySecondsNumber(entry, engine)
    num.hass = hass
    num._attr_available = True
    num.async_write_ha_state = MagicMock()
    await num.async_set_native_value(15.0)
    await _flush_debounce(hass)
    assert entry.options == {CONF_DELAY_SECONDS: 15}
    assert entry.entry_id not in hass.data.get(DOMAIN, {}).get(SKIP_RELOAD_KEY, set())


# ---------------------------------------------------------------------------
# async_setup_entry
# ---------------------------------------------------------------------------


async def test_setup_entry_skips_hub(hass: HomeAssistant):
    from custom_components.entity_guard.number import async_setup_entry

    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_ENTRY_TYPE: ENTRY_TYPE_HUB}, title="Hub"
    )
    added = []
    await async_setup_entry(hass, entry, added.extend)
    assert added == []


async def test_setup_entry_adds_numbers(hass: HomeAssistant):
    from custom_components.entity_guard.number import async_setup_entry

    entry = _make_rule_entry()
    engine = _make_engine()
    hass.data.setdefault(DOMAIN, {})["engines"] = {entry.entry_id: engine}
    added = []
    await async_setup_entry(hass, entry, added.extend)
    assert len(added) == 3
    types = [type(s).__name__ for s in added]
    assert "EntityGuardDelaySecondsNumber" in types
    assert "EntityGuardDebounceSecondsNumber" in types
    assert "EntityGuardMaxEnforcementsNumber" in types


# ---------------------------------------------------------------------------
# async_added_to_hass — dispatcher subscription and availability
# ---------------------------------------------------------------------------


async def test_number_async_added_subscribes(hass: HomeAssistant):
    from homeassistant.helpers.dispatcher import async_dispatcher_send

    entry = _make_rule_entry()
    engine = _make_engine(delay=5)
    num = EntityGuardDelaySecondsNumber(entry, engine)
    num.hass = hass
    num.async_write_ha_state = MagicMock()

    assert num._attr_available is False
    await num.async_added_to_hass()
    assert num._attr_available is True

    async_dispatcher_send(hass, f"entity_guard_rule_update_{engine.config.unique_id}")
    await hass.async_block_till_done()
    num.async_write_ha_state.assert_called()


# ---------------------------------------------------------------------------
# Per-direction delay sliders (dynamic_state). Regression for the dead/mislabelled
# single "Delay" slider: dynamic_state now exposes delay_on / delay_off sliders that
# write the directional override keys (which effective_delay actually reads), and the
# single shared slider is gated OUT for dynamic_state.
# ---------------------------------------------------------------------------


async def test_setup_entry_dynamic_state_uses_directional_sliders(hass: HomeAssistant):
    """dynamic_state → two directional delay sliders, NO single delay_seconds slider."""
    entry = _make_rule_entry()
    engine = _make_engine(mode=MODE_DYNAMIC_STATE)
    hass.data.setdefault(DOMAIN, {})["engines"] = {entry.entry_id: engine}
    added = []
    await async_setup_entry(hass, entry, added.extend)
    types = {type(s).__name__ for s in added}
    assert "EntityGuardDelayOnSecondsNumber" in types
    assert "EntityGuardDelayOffSecondsNumber" in types
    assert "EntityGuardDelaySecondsNumber" not in types  # gated out
    # Debounce + rate-limit sliders unchanged.
    assert "EntityGuardDebounceSecondsNumber" in types
    assert "EntityGuardMaxEnforcementsNumber" in types


async def test_setup_entry_fixed_mode_keeps_single_delay_slider(hass: HomeAssistant):
    """Non-dynamic_state → the single shared delay slider, no directional ones."""
    entry = _make_rule_entry()
    engine = _make_engine(mode=MODE_FIXED_STATE)
    hass.data.setdefault(DOMAIN, {})["engines"] = {entry.entry_id: engine}
    added = []
    await async_setup_entry(hass, entry, added.extend)
    types = {type(s).__name__ for s in added}
    assert "EntityGuardDelaySecondsNumber" in types
    assert "EntityGuardDelayOnSecondsNumber" not in types
    assert "EntityGuardDelayOffSecondsNumber" not in types


def test_directional_native_value_from_override():
    """Slider shows the directional override when set."""
    entry = _make_rule_entry()
    engine = _make_engine(mode=MODE_DYNAMIC_STATE)
    engine.config.delay_on_seconds = 12
    engine.config.delay_off_seconds = 88
    on = EntityGuardDelayOnSecondsNumber(entry, engine)
    off = EntityGuardDelayOffSecondsNumber(entry, engine)
    assert on.native_value == 12.0
    assert off.native_value == 88.0


def test_directional_native_value_falls_back_to_shared_when_unset():
    """Override None (pre-feature rule) → slider shows the shared delay_seconds, not None."""
    entry = _make_rule_entry()
    engine = _make_engine(delay=25, mode=MODE_DYNAMIC_STATE)
    engine.config.delay_on_seconds = None
    engine.config.delay_off_seconds = None
    on = EntityGuardDelayOnSecondsNumber(entry, engine)
    off = EntityGuardDelayOffSecondsNumber(entry, engine)
    assert on.native_value == 25.0
    assert off.native_value == 25.0


def test_directional_native_value_reads_entry_when_config_missing():
    """Both config overrides AND config.delay_seconds None → read merged entry dict
    (the directional key if present, else the entry's delay_seconds)."""
    entry = _make_rule_entry(**{CONF_DELAY_SECONDS: 33})
    engine = _make_engine(mode=MODE_DYNAMIC_STATE)
    engine.config.delay_on_seconds = None
    engine.config.delay_off_seconds = None
    engine.config.delay_seconds = None
    on = EntityGuardDelayOnSecondsNumber(entry, engine)
    # delay_on absent from entry → falls to entry's delay_seconds (33).
    assert on.native_value == 33.0
    # If the directional key IS in the entry, it wins.
    entry2 = _make_rule_entry(**{CONF_DELAY_OFF_SECONDS: 70, CONF_DELAY_SECONDS: 33})
    off = EntityGuardDelayOffSecondsNumber(entry2, engine)
    assert off.native_value == 70.0


async def test_directional_slider_write_is_read_by_effective_delay(hass: HomeAssistant):
    """THE regression lock: moving the delay-on slider changes the ACTUAL on-delay.

    Uses a real RuleConfig so effective_delay runs. Before the fix the single slider
    wrote delay_seconds, which effective_delay ignores for dynamic_state → dead control.
    The directional slider writes delay_on_seconds, which effective_delay reads.
    """
    from custom_components.entity_guard.models import RuleConfig

    config = RuleConfig(
        unique_id="u",
        name="Follow",
        target_entities=["light.a"],
        mode=MODE_DYNAMIC_STATE,
        trigger_states=[],
        target_state=None,
        delay_seconds=5,
        attribute=None,
        operator=None,
        threshold=None,
        target_value=None,
        flags=[],
        debounce_enabled=False,
        debounce_seconds=60,
        max_enforcements_per_minute=10,
        safety_acknowledged=False,
        delay_on_seconds=5,
        delay_off_seconds=5,
    )
    engine = MagicMock()
    engine.config = config
    entry = _make_rule_entry()
    entry.add_to_hass(hass)
    num = EntityGuardDelayOnSecondsNumber(entry, engine)
    num.hass = hass
    num._attr_available = True
    num.async_write_ha_state = MagicMock()

    # Pre-write: on-delay is 5.
    assert config.effective_delay(True) == 5

    await num.async_set_native_value(40.0)

    # Live: the override is updated, and effective_delay(True) now reflects it.
    assert config.delay_on_seconds == 40
    assert config.effective_delay(True) == 40
    # off-delay untouched.
    assert config.effective_delay(False) == 5
    # Persisted to the directional key (not delay_seconds) so it survives reload.
    await _flush_debounce(hass)
    assert entry.options[CONF_DELAY_ON_SECONDS] == 40
    assert CONF_DELAY_SECONDS not in entry.options
