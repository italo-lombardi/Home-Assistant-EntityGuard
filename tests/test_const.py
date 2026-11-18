"""Sanity tests for const module."""

from __future__ import annotations

import json
import pathlib

from custom_components.entity_guard import const


def test_domain() -> None:
    assert const.DOMAIN == "entity_guard"


def test_status_values_count() -> None:
    assert len(const.STATUS_VALUES) == 11
    assert set(const.STATUS_VALUES) == {
        const.STATUS_ERROR,
        const.STATUS_DISABLED,
        const.STATUS_MASTER_DISABLED,
        const.STATUS_SUPPRESSED,
        const.STATUS_ENFORCING,
        const.STATUS_COOLDOWN,
        const.STATUS_ARMED,
        const.STATUS_CONDITIONAL,
        const.STATUS_STARTING,
        const.STATUS_PENDING,
        const.STATUS_VERIFYING,
    }


def test_status_values_unique() -> None:
    assert len(const.STATUS_VALUES) == len(set(const.STATUS_VALUES))


def test_no_legacy_idle_status() -> None:
    assert not hasattr(const, "STATUS_IDLE")
    assert "idle" not in const.STATUS_VALUES


def test_error_threshold() -> None:
    assert const.ERROR_THRESHOLD == 3


def test_supported_operators_excludes_equality() -> None:
    assert "==" not in const.SUPPORTED_OPERATORS
    assert "!=" not in const.SUPPORTED_OPERATORS
    assert set(const.SUPPORTED_OPERATORS) == {"lt", "lte", "gt", "gte"}


def test_entry_types() -> None:
    assert const.ENTRY_TYPE_HUB != const.ENTRY_TYPE_RULE


def test_startup_grace_default_is_60(monkeypatch) -> None:
    monkeypatch.delenv("EG_STARTUP_GRACE_SECONDS", raising=False)
    assert const._startup_grace_seconds() == 60


def test_startup_grace_env_override(monkeypatch) -> None:
    monkeypatch.setenv("EG_STARTUP_GRACE_SECONDS", "5")
    assert const._startup_grace_seconds() == 5


def test_startup_grace_bad_value_falls_back_to_60(monkeypatch) -> None:
    monkeypatch.setenv("EG_STARTUP_GRACE_SECONDS", "not-an-int")
    assert const._startup_grace_seconds() == 60


def test_status_values_have_selector_translations() -> None:
    strings = json.loads(
        (
            pathlib.Path(__file__).parent.parent
            / "custom_components/entity_guard/strings.json"
        ).read_text()
    )
    options = strings["selector"]["status"]["options"]
    for v in const.STATUS_VALUES:
        assert v in options, (
            f"STATUS_VALUE '{v}' missing from strings.json selector.status.options"
        )


def test_color_attributes_supported() -> None:
    assert const.ATTR_RGB_COLOR in const.SUPPORTED_ATTRIBUTES
    assert const.ATTR_COLOR_TEMP_KELVIN in const.SUPPORTED_ATTRIBUTES
    assert const.ATTR_RGB_COLOR in const.ATTRIBUTES_BY_DOMAIN["light"]
    assert const.ATTR_COLOR_TEMP_KELVIN in const.ATTRIBUTES_BY_DOMAIN["light"]


def test_attribute_display_names_match_strings_selector() -> None:
    """ATTRIBUTE_DISPLAY_NAMES (used by the dynamic source-attribute picker) is a
    hand-maintained copy of strings.json selector.attribute.options (used by the
    target-attribute dropdown). They MUST match so known attributes read identically
    in both; this turns silent drift into a red test instead of a stale comment."""
    strings = json.loads(
        (
            pathlib.Path(__file__).parent.parent
            / "custom_components/entity_guard/strings.json"
        ).read_text()
    )
    options = strings["selector"]["attribute"]["options"]
    assert dict(const.ATTRIBUTE_DISPLAY_NAMES) == dict(options), (
        "const.ATTRIBUTE_DISPLAY_NAMES must stay in sync with strings.json "
        "selector.attribute.options"
    )


def test_target_state_is_on_multistate_domains() -> None:
    """Multi-state targets read a running MODE as on (Fix for the follow-mode
    reached-check: a climate at 'heat' must count as on, not just literal 'on')."""
    assert const.target_state_is_on("climate", "heat")
    assert const.target_state_is_on("climate", "cool")
    assert const.target_state_is_on("water_heater", "gas")
    assert const.target_state_is_on("water_heater", "eco")
    assert const.target_state_is_on("water_heater", "performance")
    assert const.target_state_is_on("vacuum", "cleaning")
    assert const.target_state_is_on("cover", "open")
    assert const.target_state_is_on("lawn_mower", "mowing")


def test_target_state_is_on_off_states() -> None:
    """The off / closed / docked state of a multi-state domain reads off."""
    assert not const.target_state_is_on("climate", "off")
    assert not const.target_state_is_on("water_heater", "off")
    assert not const.target_state_is_on("cover", "closed")
    assert not const.target_state_is_on("vacuum", "docked")


def test_target_state_is_on_transient_and_error_not_on() -> None:
    """Transient / error states are NOT on — a device mid-transit or errored is
    not at its active target, so the verify cycle holds instead of false-confirming."""
    assert not const.target_state_is_on("vacuum", "returning")
    assert not const.target_state_is_on("vacuum", "error")
    assert not const.target_state_is_on("cover", "opening")
    assert not const.target_state_is_on("cover", "closing")
    assert not const.target_state_is_on("cover", "stopped")
    assert not const.target_state_is_on("lawn_mower", "error")


def test_target_state_is_on_lock_not_inverted() -> None:
    """Regression guard: lock stays on the flat vocabulary (unlocked = on,
    locked = off). A naive 'not-off = on' would wrongly read locked as on."""
    assert const.target_state_is_on("lock", "unlocked")
    assert not const.target_state_is_on("lock", "locked")


def test_target_state_is_on_binary_domains_unchanged() -> None:
    """Binary / pinned domains fall back to FOLLOW_ON_STATES, unchanged."""
    assert const.target_state_is_on("switch", "on")
    assert not const.target_state_is_on("switch", "off")
    assert const.target_state_is_on("light", "on")
    assert not const.target_state_is_on("media_player", "idle")
    assert not const.target_state_is_on("media_player", "paused")
