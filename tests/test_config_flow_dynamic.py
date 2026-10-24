"""Config-flow tests for dynamic_state + dynamic_attribute modes (two-step attribute pick)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import selector
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.entity_guard.config_flow import (
    _build_summary,
    _dynamic_attribute_schema,
    _dynamic_state_schema,
    _source_attribute_options,
    _validate_dynamic_attribute,
    _validate_dynamic_state,
    _validate_source_attribute_pick,
    _validate_source_target,
)
from custom_components.entity_guard.const import (
    CONF_ATTRIBUTE,
    CONF_DELAY_SECONDS,
    CONF_ENTRY_TYPE,
    CONF_FOLLOW_INVERT,
    CONF_MODE,
    CONF_RULE_NAME,
    CONF_SOURCE_ATTRIBUTE,
    CONF_SOURCE_ENTITY,
    CONF_SOURCE_VALUE_FROM,
    CONF_TARGET_ENTITIES,
    DOMAIN,
    ENTRY_TYPE_RULE,
    MODE_DYNAMIC_ATTRIBUTE,
    MODE_DYNAMIC_STATE,
    SOURCE_VALUE_FROM_ATTRIBUTE,
    SOURCE_VALUE_FROM_STATE,
)
from custom_components.entity_guard.models import parse_rule_config


@pytest.fixture(autouse=True)
def _no_setup():
    with (
        patch(
            "custom_components.entity_guard.async_setup_entry",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "custom_components.entity_guard._async_install_card",
            new_callable=AsyncMock,
        ),
    ):
        yield


async def _begin(hass, name, mode, targets=None):
    res = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    return await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_RULE_NAME: name,
            CONF_TARGET_ENTITIES: targets or ["light.bedroom"],
            CONF_MODE: mode,
        },
    )


async def _finish(hass, flow_id):
    """Drive the extras step through to CREATE_ENTRY."""
    res = await hass.config_entries.flow.async_configure(
        flow_id,
        {"add_flags": False, "custom_rate_limit": False, "add_debounce": False},
    )
    return await hass.config_entries.flow.async_configure(res["flow_id"], {})


def _dyn_entry(hass, name, mode, extra=None, targets=None):
    data = {
        CONF_ENTRY_TYPE: ENTRY_TYPE_RULE,
        "rule_id": f"uid-{name}",
        CONF_RULE_NAME: name,
        CONF_TARGET_ENTITIES: targets or ["light.bedroom"],
        CONF_MODE: mode,
        CONF_SOURCE_ENTITY: "binary_sensor.motion",
        CONF_FOLLOW_INVERT: False,
        CONF_SOURCE_VALUE_FROM: SOURCE_VALUE_FROM_STATE,
        "flags": [],
        "debounce_enabled": False,
        "debounce_seconds": 60,
        "max_enforcements_per_minute": 10,
        "safety_acknowledged": False,
    }
    if extra:
        data.update(extra)
    e = MockConfigEntry(domain=DOMAIN, data=data, title=name, unique_id=data["rule_id"])
    e.add_to_hass(hass)
    return e


# ---------------------------------------------------------------------------
# Validators + helpers (unit)
# ---------------------------------------------------------------------------


def test_validate_source_target_empty():
    assert _validate_source_target(None, ["light.a"]) == {
        CONF_SOURCE_ENTITY: "empty_source_entity"
    }


def test_validate_source_target_self():
    assert _validate_source_target("light.a", ["light.a"]) == {
        CONF_SOURCE_ENTITY: "source_in_targets"
    }


def test_validate_dynamic_state_ok():
    assert _validate_dynamic_state("binary_sensor.m", ["light.a"]) == {}


def test_validate_dynamic_attribute_bad_target():
    assert _validate_dynamic_attribute(
        "binary_sensor.m", ["light.a"], "volume_level"
    ) == {CONF_ATTRIBUTE: "attribute_invalid_for_target"}


def test_validate_dynamic_attribute_ok():
    assert (
        _validate_dynamic_attribute("binary_sensor.m", ["light.a"], "brightness") == {}
    )


def test_validate_source_attribute_pick_empty():
    assert _validate_source_attribute_pick("") == {
        CONF_SOURCE_ATTRIBUTE: "source_attribute_required"
    }


def test_validate_source_attribute_pick_ok():
    assert _validate_source_attribute_pick("brightness") == {}


def test_source_attribute_options_filters_noise(hass: HomeAssistant):
    hass.states.async_set(
        "sensor.s",
        "123",
        {"friendly_name": "S", "supported_features": 3, "battery_level": 80},
    )
    opts = _source_attribute_options(hass, "sensor.s")
    values = [o["value"] for o in opts]
    assert "battery_level" in values
    assert "friendly_name" not in values
    assert "supported_features" not in values


def test_source_attribute_options_offline_empty(hass: HomeAssistant):
    assert _source_attribute_options(hass, "sensor.absent") == []
    assert _source_attribute_options(hass, None) == []


def test_friendly_name_fallbacks(hass: HomeAssistant):
    from custom_components.entity_guard.config_flow import _friendly_name

    assert _friendly_name(hass, None) == ""  # no entity
    assert _friendly_name(None, "sensor.x") == "sensor.x"  # no hass → id
    assert _friendly_name(hass, "sensor.absent") == "sensor.absent"  # no state → id
    hass.states.async_set("sensor.named", "1", {"friendly_name": "Named"})
    assert _friendly_name(hass, "sensor.named") == "Named"
    hass.states.async_set("sensor.unnamed", "1", {})  # no friendly_name → id
    assert _friendly_name(hass, "sensor.unnamed") == "sensor.unnamed"


def test_source_attribute_options_filters_capability_noise(hass: HomeAssistant):
    hass.states.async_set(
        "light.src",
        "on",
        {
            "brightness": 200,
            "supported_color_modes": ["rgb", "color_temp"],
            "color_mode": "rgb",
            "effect_list": ["none", "rainbow"],
            "min_color_temp_kelvin": 2000,
            "max_mireds": 500,
            "battery_level": 50,
        },
    )
    values = [o["value"] for o in _source_attribute_options(hass, "light.src")]
    assert "brightness" in values and "battery_level" in values
    for noise in (
        "supported_color_modes",
        "color_mode",
        "effect_list",
        "min_color_temp_kelvin",
        "max_mireds",
    ):
        assert noise not in values


def test_source_attribute_options_label_matches_target_for_known(hass: HomeAssistant):
    # Known attr label uses the target-dropdown display name ("Brightness"); the
    # option value stays the raw key. No current-value suffix.
    hass.states.async_set("light.src", "on", {"brightness": 200})
    opts = _source_attribute_options(hass, "light.src")
    opt = next(o for o in opts if o["value"] == "brightness")
    assert opt["label"] == "Brightness"


def test_source_attribute_options_label_prettifies_unknown(hass: HomeAssistant):
    hass.states.async_set("sensor.s", "1", {"my_custom_value": 42})
    opts = _source_attribute_options(hass, "sensor.s")
    opt = next(o for o in opts if o["value"] == "my_custom_value")
    assert opt["label"] == "My custom value"


def test_source_attribute_options_label_has_no_value_even_when_none(
    hass: HomeAssistant,
):
    # Off entity reports None-valued attrs; the label must NOT show "(None)".
    hass.states.async_set("light.off", "off", {"brightness": None})
    opts = _source_attribute_options(hass, "light.off")
    opt = next(o for o in opts if o["value"] == "brightness")
    assert opt["label"] == "Brightness"
    assert "None" not in opt["label"] and "(" not in opt["label"]


def test_pick_schema_is_dropdown_mode():
    # DROPDOWN mode so HA shows the beautified label collapsed, not the raw value.
    from custom_components.entity_guard.config_flow import _source_attribute_pick_schema

    schema = _source_attribute_pick_schema(
        [selector.SelectOptionDict(value="brightness", label="Brightness")], None
    )
    for _key, v in schema.schema.items():
        assert v.config["mode"] == selector.SelectSelectorMode.DROPDOWN
        assert v.config["custom_value"] is True


async def test_pick_description_uses_friendly_name(hass: HomeAssistant):
    hass.states.async_set(
        "sensor.lux", "on", {"friendly_name": "Hallway Lux", "battery_level": 80}
    )
    res = await _begin(hass, "FriendlyDesc", MODE_DYNAMIC_STATE)
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_SOURCE_ENTITY: "sensor.lux",
            CONF_FOLLOW_INVERT: False,
            CONF_SOURCE_VALUE_FROM: SOURCE_VALUE_FROM_ATTRIBUTE,
        },
    )
    assert res["step_id"] == "source_attribute_pick"
    assert res["description_placeholders"]["source"] == "Hallway Lux"


# ---------------------------------------------------------------------------
# _build_summary
# ---------------------------------------------------------------------------


def test_summary_dynamic_state_mirror():
    s = _build_summary(
        {
            CONF_RULE_NAME: "S",
            CONF_TARGET_ENTITIES: ["light.a"],
            CONF_MODE: MODE_DYNAMIC_STATE,
            CONF_SOURCE_ENTITY: "binary_sensor.motion",
            CONF_FOLLOW_INVERT: False,
        }
    )
    assert "dynamic state" in s and "(mirror)" in s


def test_summary_dynamic_state_invert():
    s = _build_summary(
        {
            CONF_RULE_NAME: "S",
            CONF_TARGET_ENTITIES: ["light.a"],
            CONF_MODE: MODE_DYNAMIC_STATE,
            CONF_SOURCE_ENTITY: "binary_sensor.motion",
            CONF_FOLLOW_INVERT: True,
        }
    )
    assert "(invert)" in s


def test_summary_dynamic_state_from_attribute():
    s = _build_summary(
        {
            CONF_RULE_NAME: "S",
            CONF_TARGET_ENTITIES: ["light.a"],
            CONF_MODE: MODE_DYNAMIC_STATE,
            CONF_SOURCE_ENTITY: "sensor.lux",
            CONF_FOLLOW_INVERT: False,
            CONF_SOURCE_VALUE_FROM: SOURCE_VALUE_FROM_ATTRIBUTE,
            CONF_SOURCE_ATTRIBUTE: "battery_level",
        }
    )
    assert "battery_level" in s


def test_summary_dynamic_attribute_from_state():
    s = _build_summary(
        {
            CONF_RULE_NAME: "A",
            CONF_TARGET_ENTITIES: ["light.a"],
            CONF_MODE: MODE_DYNAMIC_ATTRIBUTE,
            CONF_SOURCE_ENTITY: "sensor.lux",
            CONF_SOURCE_VALUE_FROM: SOURCE_VALUE_FROM_STATE,
            CONF_ATTRIBUTE: "brightness",
        }
    )
    assert "dynamic attribute" in s and "`sensor.lux`.state" in s


def test_summary_dynamic_attribute_from_attribute():
    s = _build_summary(
        {
            CONF_RULE_NAME: "A",
            CONF_TARGET_ENTITIES: ["light.a"],
            CONF_MODE: MODE_DYNAMIC_ATTRIBUTE,
            CONF_SOURCE_ENTITY: "light.src",
            CONF_SOURCE_VALUE_FROM: SOURCE_VALUE_FROM_ATTRIBUTE,
            CONF_SOURCE_ATTRIBUTE: "brightness",
            CONF_ATTRIBUTE: "brightness",
        }
    )
    assert "`light.src`.brightness" in s


# ---------------------------------------------------------------------------
# Create — dynamic_state (state source: one step)
# ---------------------------------------------------------------------------


async def test_create_dynamic_state_from_state(hass: HomeAssistant):
    res = await _begin(hass, "DynState", MODE_DYNAMIC_STATE)
    assert res["step_id"] == "dynamic_state_params"
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_SOURCE_ENTITY: "binary_sensor.motion",
            CONF_FOLLOW_INVERT: True,
            CONF_SOURCE_VALUE_FROM: SOURCE_VALUE_FROM_STATE,
            CONF_DELAY_SECONDS: 30,
        },
    )
    res = await _finish(hass, res["flow_id"])
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert res["data"][CONF_SOURCE_ENTITY] == "binary_sensor.motion"
    assert res["data"][CONF_FOLLOW_INVERT] is True
    assert res["data"][CONF_SOURCE_ATTRIBUTE] is None
    assert res["data"][CONF_DELAY_SECONDS] == 30
    assert CONF_ATTRIBUTE not in res["data"]


async def test_create_dynamic_state_invalid_delay(hass: HomeAssistant):
    # Invalid delay re-shows the first step (does not advance to pick/extras).
    from custom_components.entity_guard.config_flow import EntityGuardConfigFlow

    flow = EntityGuardConfigFlow()
    flow.hass = hass
    flow._rule_data = {
        CONF_TARGET_ENTITIES: ["light.bedroom"],
        CONF_MODE: MODE_DYNAMIC_STATE,
    }
    res = await flow.async_step_dynamic_state_params(
        {
            CONF_SOURCE_ENTITY: "binary_sensor.motion",
            CONF_FOLLOW_INVERT: False,
            CONF_SOURCE_VALUE_FROM: SOURCE_VALUE_FROM_STATE,
            CONF_DELAY_SECONDS: -5,
        }
    )
    assert res["step_id"] == "dynamic_state_params"
    assert res["errors"][CONF_DELAY_SECONDS] == "invalid_delay"


async def test_create_dynamic_attribute_invalid_delay(hass: HomeAssistant):
    from custom_components.entity_guard.config_flow import EntityGuardConfigFlow

    flow = EntityGuardConfigFlow()
    flow.hass = hass
    flow._rule_data = {
        CONF_TARGET_ENTITIES: ["light.bedroom"],
        CONF_MODE: MODE_DYNAMIC_ATTRIBUTE,
    }
    res = await flow.async_step_dynamic_attribute_params(
        {
            CONF_SOURCE_ENTITY: "sensor.lux",
            CONF_SOURCE_VALUE_FROM: SOURCE_VALUE_FROM_STATE,
            CONF_ATTRIBUTE: "brightness",
            CONF_DELAY_SECONDS: -1,
        }
    )
    assert res["step_id"] == "dynamic_attribute_params"
    assert res["errors"][CONF_DELAY_SECONDS] == "invalid_delay"


# ---------------------------------------------------------------------------
# Create — dynamic_state (attribute source: two steps, live pick)
# ---------------------------------------------------------------------------


async def test_create_dynamic_state_from_attribute_two_step(hass: HomeAssistant):
    hass.states.async_set("sensor.lux", "on", {"battery_level": 80})
    res = await _begin(hass, "DynStateAttr", MODE_DYNAMIC_STATE)
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_SOURCE_ENTITY: "sensor.lux",
            CONF_FOLLOW_INVERT: False,
            CONF_SOURCE_VALUE_FROM: SOURCE_VALUE_FROM_ATTRIBUTE,
        },
    )
    # Advances to the live-attribute pick step.
    assert res["step_id"] == "source_attribute_pick"
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {CONF_SOURCE_ATTRIBUTE: "battery_level"}
    )
    res = await _finish(hass, res["flow_id"])
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert res["data"][CONF_SOURCE_VALUE_FROM] == SOURCE_VALUE_FROM_ATTRIBUTE
    assert res["data"][CONF_SOURCE_ATTRIBUTE] == "battery_level"
    cfg = parse_rule_config(
        _dyn_entry(hass, "dsa", MODE_DYNAMIC_STATE, extra=res["data"])
    )
    assert cfg.source_attribute == "battery_level"


async def test_create_dynamic_state_pick_requires_choice(hass: HomeAssistant):
    hass.states.async_set("sensor.lux", "on", {"battery_level": 80})
    res = await _begin(hass, "DynStatePickBad", MODE_DYNAMIC_STATE)
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_SOURCE_ENTITY: "sensor.lux",
            CONF_FOLLOW_INVERT: False,
            CONF_SOURCE_VALUE_FROM: SOURCE_VALUE_FROM_ATTRIBUTE,
        },
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {CONF_SOURCE_ATTRIBUTE: "  "}
    )
    assert res["step_id"] == "source_attribute_pick"
    assert res["errors"][CONF_SOURCE_ATTRIBUTE] == "source_attribute_required"


async def test_create_dynamic_state_pick_no_attributes(hass: HomeAssistant):
    # Source has no state → empty options, nothing stored → no-attributes hint.
    res = await _begin(hass, "DynStateNoAttr", MODE_DYNAMIC_STATE)
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_SOURCE_ENTITY: "sensor.absent",
            CONF_FOLLOW_INVERT: False,
            CONF_SOURCE_VALUE_FROM: SOURCE_VALUE_FROM_ATTRIBUTE,
        },
    )
    assert res["step_id"] == "source_attribute_pick"
    assert res["errors"]["base"] == "source_has_no_attributes"


async def test_create_dynamic_state_source_in_targets_handler(hass: HomeAssistant):
    from custom_components.entity_guard.config_flow import EntityGuardConfigFlow

    flow = EntityGuardConfigFlow()
    flow.hass = hass
    flow._rule_data = {
        CONF_TARGET_ENTITIES: ["light.bedroom"],
        CONF_MODE: MODE_DYNAMIC_STATE,
    }
    res = await flow.async_step_dynamic_state_params(
        {
            CONF_SOURCE_ENTITY: "light.bedroom",
            CONF_FOLLOW_INVERT: False,
            CONF_SOURCE_VALUE_FROM: SOURCE_VALUE_FROM_STATE,
        }
    )
    assert res["step_id"] == "dynamic_state_params"
    assert res["errors"][CONF_SOURCE_ENTITY] == "source_in_targets"


# ---------------------------------------------------------------------------
# Create — dynamic_attribute
# ---------------------------------------------------------------------------


async def test_create_dynamic_attribute_from_state(hass: HomeAssistant):
    res = await _begin(hass, "DynAttrState", MODE_DYNAMIC_ATTRIBUTE)
    assert res["step_id"] == "dynamic_attribute_params"
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_SOURCE_ENTITY: "sensor.lux",
            CONF_SOURCE_VALUE_FROM: SOURCE_VALUE_FROM_STATE,
            CONF_ATTRIBUTE: "brightness",
        },
    )
    res = await _finish(hass, res["flow_id"])
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert res["data"][CONF_SOURCE_ATTRIBUTE] is None
    assert res["data"][CONF_ATTRIBUTE] == "brightness"


async def test_create_dynamic_attribute_from_attribute_two_step(hass: HomeAssistant):
    hass.states.async_set("light.src", "on", {"brightness": 200})
    res = await _begin(hass, "DynAttrAttr", MODE_DYNAMIC_ATTRIBUTE)
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_SOURCE_ENTITY: "light.src",
            CONF_SOURCE_VALUE_FROM: SOURCE_VALUE_FROM_ATTRIBUTE,
            CONF_ATTRIBUTE: "brightness",
        },
    )
    assert res["step_id"] == "source_attribute_pick"
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {CONF_SOURCE_ATTRIBUTE: "brightness"}
    )
    res = await _finish(hass, res["flow_id"])
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert res["data"][CONF_SOURCE_ATTRIBUTE] == "brightness"
    assert res["data"][CONF_ATTRIBUTE] == "brightness"


# ---------------------------------------------------------------------------
# Options edit — dynamic_state (state) + dynamic_attribute (attribute, two-step)
# ---------------------------------------------------------------------------


async def test_options_edit_dynamic_state(hass: HomeAssistant):
    e = _dyn_entry(hass, "EditDS", MODE_DYNAMIC_STATE)
    res = await hass.config_entries.options.async_init(e.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_mode"}
    )
    assert res["step_id"] == "edit_dynamic_state_params"
    res = await hass.config_entries.options.async_configure(
        res["flow_id"],
        {
            CONF_SOURCE_ENTITY: "binary_sensor.other",
            CONF_FOLLOW_INVERT: True,
            CONF_SOURCE_VALUE_FROM: SOURCE_VALUE_FROM_STATE,
        },
    )
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert e.options[CONF_SOURCE_ENTITY] == "binary_sensor.other"
    assert e.options[CONF_SOURCE_ATTRIBUTE] is None


async def test_options_edit_dynamic_attribute_two_step(hass: HomeAssistant):
    hass.states.async_set("light.src", "on", {"brightness": 150})
    e = _dyn_entry(
        hass,
        "EditDA",
        MODE_DYNAMIC_ATTRIBUTE,
        extra={CONF_ATTRIBUTE: "brightness"},
    )
    res = await hass.config_entries.options.async_init(e.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_mode"}
    )
    assert res["step_id"] == "edit_dynamic_attribute_params"
    res = await hass.config_entries.options.async_configure(
        res["flow_id"],
        {
            CONF_SOURCE_ENTITY: "light.src",
            CONF_SOURCE_VALUE_FROM: SOURCE_VALUE_FROM_ATTRIBUTE,
            CONF_ATTRIBUTE: "brightness",
        },
    )
    assert res["step_id"] == "edit_source_attribute_pick"
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {CONF_SOURCE_ATTRIBUTE: "brightness"}
    )
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert e.options[CONF_SOURCE_ATTRIBUTE] == "brightness"


async def test_options_edit_dynamic_state_offline_source_preserves_attr(
    hass: HomeAssistant,
):
    # Edge 4: editing an attr-sourced rule whose source is offline — the stored
    # attribute is preserved; the pick step does not error or force a re-pick.
    e = _dyn_entry(
        hass,
        "EditOffline",
        MODE_DYNAMIC_STATE,
        extra={
            CONF_SOURCE_VALUE_FROM: SOURCE_VALUE_FROM_ATTRIBUTE,
            CONF_SOURCE_ATTRIBUTE: "battery_level",
        },
    )
    res = await hass.config_entries.options.async_init(e.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_mode"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"],
        {
            CONF_SOURCE_ENTITY: "sensor.offline",  # no state set → no live attrs
            CONF_FOLLOW_INVERT: False,
            CONF_SOURCE_VALUE_FROM: SOURCE_VALUE_FROM_ATTRIBUTE,
        },
    )
    # Pick step opens with no error (stored battery_level falls back via custom_value).
    assert res["step_id"] == "edit_source_attribute_pick"
    assert res["errors"] in (None, {})
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {CONF_SOURCE_ATTRIBUTE: "battery_level"}
    )
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert e.options[CONF_SOURCE_ATTRIBUTE] == "battery_level"


async def test_options_edit_dynamic_attribute_from_state_one_step(hass: HomeAssistant):
    # Editing dynamic_attribute with value_from=state saves in one step (no pick).
    e = _dyn_entry(
        hass,
        "EditDAState",
        MODE_DYNAMIC_ATTRIBUTE,
        extra={CONF_ATTRIBUTE: "brightness"},
    )
    res = await hass.config_entries.options.async_init(e.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_mode"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"],
        {
            CONF_SOURCE_ENTITY: "sensor.lux",
            CONF_SOURCE_VALUE_FROM: SOURCE_VALUE_FROM_STATE,
            CONF_ATTRIBUTE: "brightness",
        },
    )
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert e.options[CONF_SOURCE_ATTRIBUTE] is None


async def test_options_edit_pick_no_attributes(hass: HomeAssistant):
    # Edit pick step, source offline AND nothing stored → no-attributes hint.
    e = _dyn_entry(
        hass,
        "EditPickNoAttr",
        MODE_DYNAMIC_STATE,
        extra={CONF_SOURCE_VALUE_FROM: SOURCE_VALUE_FROM_ATTRIBUTE},
    )
    res = await hass.config_entries.options.async_init(e.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_mode"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"],
        {
            CONF_SOURCE_ENTITY: "sensor.offline",
            CONF_FOLLOW_INVERT: False,
            CONF_SOURCE_VALUE_FROM: SOURCE_VALUE_FROM_ATTRIBUTE,
        },
    )
    assert res["step_id"] == "edit_source_attribute_pick"
    assert res["errors"]["base"] == "source_has_no_attributes"


# ---------------------------------------------------------------------------
# Migrated follow+attr → dynamic_state opens edit clean (attr dropped, state source)
# ---------------------------------------------------------------------------


async def test_migrated_dynamic_state_edit_clean(hass: HomeAssistant):
    e = _dyn_entry(hass, "Migrated", MODE_DYNAMIC_STATE)
    res = await hass.config_entries.options.async_init(e.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_mode"}
    )
    assert res["step_id"] == "edit_dynamic_state_params"
    assert res["errors"] in (None, {})


async def test_create_dynamic_attribute_handler_rejects_source_in_targets(
    hass: HomeAssistant,
):
    from custom_components.entity_guard.config_flow import EntityGuardConfigFlow

    flow = EntityGuardConfigFlow()
    flow.hass = hass
    flow._rule_data = {
        CONF_TARGET_ENTITIES: ["light.bedroom"],
        CONF_MODE: MODE_DYNAMIC_ATTRIBUTE,
    }
    res = await flow.async_step_dynamic_attribute_params(
        {
            CONF_SOURCE_ENTITY: "light.bedroom",
            CONF_SOURCE_VALUE_FROM: SOURCE_VALUE_FROM_STATE,
            CONF_ATTRIBUTE: "brightness",
        }
    )
    assert res["step_id"] == "dynamic_attribute_params"
    assert res["errors"][CONF_SOURCE_ENTITY] == "source_in_targets"


# ---------------------------------------------------------------------------
# Direct-handler backstop coverage for form-unreachable validation-error branches
# (the EntitySelector/SelectSelector block bad input before these handlers in the
# live form, so the error re-show branches are driven directly).
# ---------------------------------------------------------------------------


def _opts_flow(hass, working):
    from custom_components.entity_guard.config_flow import EntityGuardOptionsFlow

    flow = EntityGuardOptionsFlow()
    flow.hass = hass
    flow._working = working
    return flow


async def test_edit_dynamic_state_handler_rejects_source_in_targets(
    hass: HomeAssistant,
):
    flow = _opts_flow(
        hass, {CONF_TARGET_ENTITIES: ["light.bedroom"], CONF_MODE: MODE_DYNAMIC_STATE}
    )
    res = await flow.async_step_edit_dynamic_state_params(
        {
            CONF_SOURCE_ENTITY: "light.bedroom",
            CONF_FOLLOW_INVERT: False,
            CONF_SOURCE_VALUE_FROM: SOURCE_VALUE_FROM_STATE,
        }
    )
    assert res["step_id"] == "edit_dynamic_state_params"
    assert res["errors"][CONF_SOURCE_ENTITY] == "source_in_targets"


async def test_edit_dynamic_attribute_handler_rejects_source_in_targets(
    hass: HomeAssistant,
):
    flow = _opts_flow(
        hass,
        {CONF_TARGET_ENTITIES: ["light.bedroom"], CONF_MODE: MODE_DYNAMIC_ATTRIBUTE},
    )
    res = await flow.async_step_edit_dynamic_attribute_params(
        {
            CONF_SOURCE_ENTITY: "light.bedroom",
            CONF_SOURCE_VALUE_FROM: SOURCE_VALUE_FROM_STATE,
            CONF_ATTRIBUTE: "brightness",
        }
    )
    assert res["step_id"] == "edit_dynamic_attribute_params"
    assert res["errors"][CONF_SOURCE_ENTITY] == "source_in_targets"


async def test_edit_dynamic_state_invalid_delay(hass: HomeAssistant):
    flow = _opts_flow(
        hass, {CONF_TARGET_ENTITIES: ["light.bedroom"], CONF_MODE: MODE_DYNAMIC_STATE}
    )
    res = await flow.async_step_edit_dynamic_state_params(
        {
            CONF_SOURCE_ENTITY: "binary_sensor.motion",
            CONF_FOLLOW_INVERT: False,
            CONF_SOURCE_VALUE_FROM: SOURCE_VALUE_FROM_STATE,
            CONF_DELAY_SECONDS: -5,
        }
    )
    assert res["step_id"] == "edit_dynamic_state_params"
    assert res["errors"][CONF_DELAY_SECONDS] == "invalid_delay"


async def test_edit_dynamic_attribute_invalid_delay(hass: HomeAssistant):
    flow = _opts_flow(
        hass,
        {CONF_TARGET_ENTITIES: ["light.bedroom"], CONF_MODE: MODE_DYNAMIC_ATTRIBUTE},
    )
    res = await flow.async_step_edit_dynamic_attribute_params(
        {
            CONF_SOURCE_ENTITY: "sensor.lux",
            CONF_SOURCE_VALUE_FROM: SOURCE_VALUE_FROM_STATE,
            CONF_ATTRIBUTE: "brightness",
            CONF_DELAY_SECONDS: -5,
        }
    )
    assert res["step_id"] == "edit_dynamic_attribute_params"
    assert res["errors"][CONF_DELAY_SECONDS] == "invalid_delay"


async def test_edit_pick_handler_rejects_empty_choice(hass: HomeAssistant):
    hass.states.async_set("sensor.lux", "on", {"battery_level": 80})
    flow = _opts_flow(
        hass,
        {
            CONF_TARGET_ENTITIES: ["light.bedroom"],
            CONF_MODE: MODE_DYNAMIC_STATE,
            CONF_SOURCE_ENTITY: "sensor.lux",
        },
    )
    res = await flow.async_step_edit_source_attribute_pick({CONF_SOURCE_ATTRIBUTE: " "})
    assert res["step_id"] == "edit_source_attribute_pick"
    assert res["errors"][CONF_SOURCE_ATTRIBUTE] == "source_attribute_required"


# ---------------------------------------------------------------------------
# Source selector excludes targets (SPEC §9)
# ---------------------------------------------------------------------------


def test_dynamic_state_schema_excludes_targets():
    schema = _dynamic_state_schema(exclude_targets=["light.a"])
    for key, v in schema.schema.items():
        if getattr(key, "schema", key) == CONF_SOURCE_ENTITY:
            assert v.config["exclude_entities"] == ["light.a"]
            break


def test_dynamic_attribute_schema_excludes_targets():
    schema = _dynamic_attribute_schema(["brightness"], exclude_targets=["light.a"])
    for key, v in schema.schema.items():
        if getattr(key, "schema", key) == CONF_SOURCE_ENTITY:
            assert v.config["exclude_entities"] == ["light.a"]
            break


def test_dynamic_state_schema_no_exclude_when_empty():
    schema = _dynamic_state_schema(exclude_targets=[])
    for key, v in schema.schema.items():
        if getattr(key, "schema", key) == CONF_SOURCE_ENTITY:
            assert "exclude_entities" not in v.config
            break
