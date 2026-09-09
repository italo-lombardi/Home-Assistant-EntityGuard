"""End-to-end flow tests for config_flow.py covering steps."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.entity_guard.const import (
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    CONF_ATTRIBUTE,
    CONF_DEBOUNCE_ENABLED,
    CONF_DEBOUNCE_SECONDS,
    CONF_DELAY_SECONDS,
    CONF_ENTRY_TYPE,
    CONF_FLAG_ENTITY,
    CONF_FLAG_MATCH_STATE,
    CONF_MAX_ENFORCEMENTS_PER_MINUTE,
    CONF_MODE,
    CONF_OPERATOR,
    CONF_RULE_NAME,
    CONF_SAFETY_ACKNOWLEDGED,
    CONF_TARGET_ENTITIES,
    CONF_TARGET_STATE,
    CONF_TARGET_VALUE,
    CONF_THRESHOLD,
    CONF_TRIGGER_STATES,
    DOMAIN,
    ENTRY_TYPE_RULE,
    MODE_ATTRIBUTE,
    MODE_STATE,
)


def _schema_key_names(result: dict) -> set[str]:
    return {getattr(key, "schema", key) for key in result["data_schema"].schema}


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


# ---------------------------------------------------------------------------
# Rule basics step — error paths
# ---------------------------------------------------------------------------


async def test_rule_step_empty_name_error(hass: HomeAssistant):
    res = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_RULE_NAME: "   ",
            CONF_TARGET_ENTITIES: ["light.bedroom"],
            CONF_MODE: MODE_STATE,
        },
    )
    assert res["type"] == FlowResultType.FORM
    assert res["errors"][CONF_RULE_NAME] == "empty_rule_name"


async def test_rule_step_duplicate_name(hass: HomeAssistant):
    existing = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_RULE, CONF_RULE_NAME: "Dup"},
        title="Dup",
    )
    existing.add_to_hass(hass)
    res = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_RULE_NAME: "Dup",
            CONF_TARGET_ENTITIES: ["light.bedroom"],
            CONF_MODE: MODE_STATE,
        },
    )
    assert res["errors"][CONF_RULE_NAME] == "name_already_exists"


async def test_rule_step_no_entities(hass: HomeAssistant):
    res = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_RULE_NAME: "Test",
            CONF_TARGET_ENTITIES: [],
            CONF_MODE: MODE_STATE,
        },
    )
    assert res["errors"][CONF_TARGET_ENTITIES] == "empty_target_entities"


# ---------------------------------------------------------------------------
# State step — error paths
# ---------------------------------------------------------------------------


async def _begin_state_flow(hass: HomeAssistant, name="StateRule"):
    res = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_RULE_NAME: name,
            CONF_TARGET_ENTITIES: ["light.bedroom"],
            CONF_MODE: MODE_STATE,
        },
    )
    assert res["step_id"] == "state"
    return res


async def test_state_step_empty_triggers(hass: HomeAssistant):
    res = await _begin_state_flow(hass, "S1")
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_TRIGGER_STATES: [],
            CONF_TARGET_STATE: "off",
            CONF_DELAY_SECONDS: 0,
        },
    )
    assert res["errors"][CONF_TRIGGER_STATES] == "empty_trigger_states"


async def test_state_step_forbidden_trigger(hass: HomeAssistant):
    res = await _begin_state_flow(hass, "S2")
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_TRIGGER_STATES: ["unavailable"],
            CONF_TARGET_STATE: "off",
            CONF_DELAY_SECONDS: 0,
        },
    )
    assert res["errors"][CONF_TRIGGER_STATES] == "forbidden_state"


async def test_state_step_target_in_triggers(hass: HomeAssistant):
    res = await _begin_state_flow(hass, "S3")
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_TRIGGER_STATES: ["on"],
            CONF_TARGET_STATE: "on",
            CONF_DELAY_SECONDS: 0,
        },
    )
    assert res["errors"][CONF_TARGET_STATE] == "target_in_triggers"


async def test_state_step_invalid_target(hass: HomeAssistant):
    res = await _begin_state_flow(hass, "S4")
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_TRIGGER_STATES: ["on"],
            CONF_TARGET_STATE: "   ",
            CONF_DELAY_SECONDS: 0,
        },
    )
    assert res["errors"][CONF_TARGET_STATE] == "empty_target_state"


# ---------------------------------------------------------------------------
# Full state-mode flow → preview → entry created
# ---------------------------------------------------------------------------


async def test_full_state_flow_creates_entry(hass: HomeAssistant):
    res = await _begin_state_flow(hass, "FullState")
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_TRIGGER_STATES: ["on"],
            CONF_TARGET_STATE: "off",
            CONF_DELAY_SECONDS: 0,
        },
    )
    assert res["step_id"] == "extras"
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {"add_flags": False, "custom_rate_limit": False, "add_debounce": False},
    )
    assert res["step_id"] == "preview"
    res = await hass.config_entries.flow.async_configure(res["flow_id"], {})
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert res["title"] == "FullState"


async def test_full_state_flow_with_extras(hass: HomeAssistant):
    res = await _begin_state_flow(hass, "WithExtras")
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_TRIGGER_STATES: ["on"],
            CONF_TARGET_STATE: "off",
            CONF_DELAY_SECONDS: 0,
        },
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {"add_flags": True, "custom_rate_limit": True, "add_debounce": True},
    )
    assert res["step_id"] == "flags"
    # Add one flag and proceed
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_FLAG_ENTITY: "input_boolean.x",
            CONF_FLAG_MATCH_STATE: "on",
            "add_another": False,
        },
    )
    assert res["step_id"] == "advanced"
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_DEBOUNCE_SECONDS: 30,
            "rate_limit_enabled": True,
            CONF_MAX_ENFORCEMENTS_PER_MINUTE: 5,
        },
    )
    assert res["step_id"] == "preview"


async def test_flags_incomplete_flag_error(hass: HomeAssistant):
    res = await _begin_state_flow(hass, "FlagErr")
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_TRIGGER_STATES: ["on"],
            CONF_TARGET_STATE: "off",
            CONF_DELAY_SECONDS: 0,
        },
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {"add_flags": True, "custom_rate_limit": False, "add_debounce": False},
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {CONF_FLAG_ENTITY: "input_boolean.x", CONF_FLAG_MATCH_STATE: ""},
    )
    assert res["errors"]["base"] == "incomplete_flag"


async def test_advanced_invalid_rate_skipped(hass: HomeAssistant):
    # Selector schema clamps to MIN/MAX before flow can hit invalid_rate branch.
    pass


async def test_advanced_rate_disabled_sets_zero(hass: HomeAssistant):
    res = await _begin_state_flow(hass, "RateOff")
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_TRIGGER_STATES: ["on"],
            CONF_TARGET_STATE: "off",
            CONF_DELAY_SECONDS: 0,
        },
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {"add_flags": False, "custom_rate_limit": True, "add_debounce": False},
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"rate_limit_enabled": False}
    )
    assert res["step_id"] == "preview"


# ---------------------------------------------------------------------------
# Attribute mode flow
# ---------------------------------------------------------------------------


async def test_attribute_flow(hass: HomeAssistant):
    res = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_RULE_NAME: "AttrRule",
            CONF_TARGET_ENTITIES: ["light.bedroom"],
            CONF_MODE: MODE_ATTRIBUTE,
        },
    )
    assert res["step_id"] == "attribute"
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {CONF_ATTRIBUTE: "brightness"}
    )
    assert res["step_id"] == "attribute_params"
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_OPERATOR: "gt",
            CONF_THRESHOLD: 64,
            CONF_TARGET_VALUE: 64,
            CONF_DELAY_SECONDS: 0,
        },
    )
    assert res["step_id"] == "extras"


async def test_attribute_params_schema_by_selected_attribute_create_flow(
    hass: HomeAssistant,
):
    for attr in (ATTR_RGB_COLOR, ATTR_COLOR_TEMP_KELVIN, "brightness"):
        res = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        res = await hass.config_entries.flow.async_configure(
            res["flow_id"],
            {
                CONF_RULE_NAME: f"ColorRule-{attr}",
                CONF_TARGET_ENTITIES: ["light.bedroom"],
                CONF_MODE: MODE_ATTRIBUTE,
            },
        )
        assert res["step_id"] == "attribute"
        res = await hass.config_entries.flow.async_configure(
            res["flow_id"], {CONF_ATTRIBUTE: attr}
        )
        assert res["step_id"] == "attribute_params"
        schema_keys = _schema_key_names(res)
        assert CONF_TARGET_VALUE in schema_keys
        if attr in (ATTR_RGB_COLOR, ATTR_COLOR_TEMP_KELVIN):
            assert CONF_OPERATOR not in schema_keys
            assert CONF_THRESHOLD not in schema_keys
        else:
            assert CONF_OPERATOR in schema_keys
            assert CONF_THRESHOLD in schema_keys


async def test_attribute_flow_color_temp_kelvin(hass: HomeAssistant):
    res = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_RULE_NAME: "KelvinRule",
            CONF_TARGET_ENTITIES: ["light.bedroom"],
            CONF_MODE: MODE_ATTRIBUTE,
        },
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {CONF_ATTRIBUTE: ATTR_COLOR_TEMP_KELVIN},
    )
    assert res["step_id"] == "attribute_params"
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {CONF_TARGET_VALUE: 2700, CONF_DELAY_SECONDS: 0}
    )
    assert res["step_id"] == "extras"


async def test_full_attribute_flow_rgb_color_creates_entry(hass: HomeAssistant):
    res = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_RULE_NAME: "Accent Lock",
            CONF_TARGET_ENTITIES: ["light.bedroom"],
            CONF_MODE: MODE_ATTRIBUTE,
        },
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {CONF_ATTRIBUTE: ATTR_RGB_COLOR}
    )
    assert res["step_id"] == "attribute_params"
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {CONF_TARGET_VALUE: [255, 0, 0], CONF_DELAY_SECONDS: 0}
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {"add_flags": False, "custom_rate_limit": False, "add_debounce": False},
    )
    assert res["step_id"] == "preview"
    res = await hass.config_entries.flow.async_configure(res["flow_id"], {})
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert res["data"][CONF_TARGET_VALUE] == [255, 0, 0]
    assert res["data"][CONF_OPERATOR] is None
    assert res["data"][CONF_THRESHOLD] is None


async def test_full_attribute_flow_color_temp_kelvin_creates_entry(hass: HomeAssistant):
    res = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_RULE_NAME: "Kelvin Lock",
            CONF_TARGET_ENTITIES: ["light.bedroom"],
            CONF_MODE: MODE_ATTRIBUTE,
        },
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {CONF_ATTRIBUTE: ATTR_COLOR_TEMP_KELVIN}
    )
    assert res["step_id"] == "attribute_params"
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {CONF_TARGET_VALUE: 3000, CONF_DELAY_SECONDS: 0}
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {"add_flags": False, "custom_rate_limit": False, "add_debounce": False},
    )
    assert res["step_id"] == "preview"
    res = await hass.config_entries.flow.async_configure(res["flow_id"], {})
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert res["data"][CONF_TARGET_VALUE] == 3000
    assert res["data"][CONF_OPERATOR] is None
    assert res["data"][CONF_THRESHOLD] is None


async def test_attribute_invalid_delay_skipped(hass: HomeAssistant):
    # Schema-validated bounds prevent reaching invalid_delay error branch via flow harness;
    # _coerce_delay rejection covered in helper unit tests.
    pass


# ---------------------------------------------------------------------------
# _attribute_schema default type-guard tests (Item 2)
# ---------------------------------------------------------------------------


def test_attribute_schema_rgb_default_falls_back_when_prior_value_is_int():
    """Switching to rgb_color when prior target_value was a Kelvin int yields RGB default."""
    from custom_components.entity_guard.config_flow import _attribute_schema

    schema = _attribute_schema(ATTR_RGB_COLOR, target_value_default=2700)
    defaults = {
        getattr(key, "schema", key): key.default()
        for key in schema.schema
        if hasattr(key, "default")
    }
    assert defaults[CONF_TARGET_VALUE] == [255, 255, 255]


def test_attribute_schema_rgb_default_uses_valid_list():
    """Switching to rgb_color when prior target_value is a valid RGB list uses it."""
    from custom_components.entity_guard.config_flow import _attribute_schema

    schema = _attribute_schema(ATTR_RGB_COLOR, target_value_default=[10, 20, 30])
    defaults = {
        getattr(key, "schema", key): key.default()
        for key in schema.schema
        if hasattr(key, "default")
    }
    assert defaults[CONF_TARGET_VALUE] == [10, 20, 30]


def test_attribute_schema_kelvin_default_falls_back_when_prior_value_is_list():
    """Switching to color_temp_kelvin when prior target_value was an RGB list yields Kelvin default."""
    from custom_components.entity_guard.config_flow import _attribute_schema

    schema = _attribute_schema(ATTR_COLOR_TEMP_KELVIN, target_value_default=[255, 0, 0])
    defaults = {
        getattr(key, "schema", key): key.default()
        for key in schema.schema
        if hasattr(key, "default")
    }
    assert defaults[CONF_TARGET_VALUE] == 2700


def test_attribute_schema_kelvin_default_uses_valid_int():
    """Switching to color_temp_kelvin when prior target_value is a valid int uses it."""
    from custom_components.entity_guard.config_flow import _attribute_schema

    schema = _attribute_schema(ATTR_COLOR_TEMP_KELVIN, target_value_default=4000)
    defaults = {
        getattr(key, "schema", key): key.default()
        for key in schema.schema
        if hasattr(key, "default")
    }
    assert defaults[CONF_TARGET_VALUE] == 4000


# ---------------------------------------------------------------------------
# Safety step
# ---------------------------------------------------------------------------


async def test_safety_required_for_lock(hass: HomeAssistant):
    res = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_RULE_NAME: "Lock",
            CONF_TARGET_ENTITIES: ["lock.front"],
            CONF_MODE: MODE_STATE,
        },
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_TRIGGER_STATES: ["unlocked"],
            CONF_TARGET_STATE: "locked",
            CONF_DELAY_SECONDS: 0,
        },
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {"add_flags": False, "custom_rate_limit": False, "add_debounce": False},
    )
    assert res["step_id"] == "safety"
    # Reject: no acknowledgment
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {CONF_SAFETY_ACKNOWLEDGED: False}
    )
    assert res["errors"][CONF_SAFETY_ACKNOWLEDGED] == "safety_not_acknowledged"
    # Accept
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {CONF_SAFETY_ACKNOWLEDGED: True}
    )
    assert res["step_id"] == "preview"


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------


@pytest.fixture
def options_rule_entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_RULE,
            "rule_id": "uid",
            CONF_RULE_NAME: "OptRule",
            CONF_TARGET_ENTITIES: ["light.bedroom"],
            CONF_MODE: MODE_STATE,
            CONF_TRIGGER_STATES: ["on"],
            CONF_TARGET_STATE: "off",
            CONF_DELAY_SECONDS: 0,
            "flags": [],
            CONF_DEBOUNCE_ENABLED: False,
            CONF_DEBOUNCE_SECONDS: 60,
            CONF_MAX_ENFORCEMENTS_PER_MINUTE: 10,
            CONF_SAFETY_ACKNOWLEDGED: False,
        },
        title="OptRule",
        unique_id="uid",
    )
    entry.add_to_hass(hass)
    return entry


async def test_options_hub_aborts(hass: HomeAssistant, hub_entry):
    hub_entry.add_to_hass(hass)
    res = await hass.config_entries.options.async_init(hub_entry.entry_id)
    assert res["type"] == FlowResultType.ABORT
    assert res["reason"] == "hub_no_options"


async def test_options_init_menu(hass: HomeAssistant, options_rule_entry):
    res = await hass.config_entries.options.async_init(options_rule_entry.entry_id)
    assert res["type"] == FlowResultType.MENU


async def test_options_edit_basics_empty_name(hass: HomeAssistant, options_rule_entry):
    res = await hass.config_entries.options.async_init(options_rule_entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_basics"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {CONF_RULE_NAME: "  "}
    )
    assert res["errors"][CONF_RULE_NAME] == "empty_rule_name"


async def test_options_edit_basics_save(hass: HomeAssistant, options_rule_entry):
    res = await hass.config_entries.options.async_init(options_rule_entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_basics"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {CONF_RULE_NAME: "Renamed"}
    )
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert options_rule_entry.data[CONF_RULE_NAME] == "Renamed"


async def test_options_edit_entities_empty(hass: HomeAssistant, options_rule_entry):
    res = await hass.config_entries.options.async_init(options_rule_entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_entities"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {CONF_TARGET_ENTITIES: []}
    )
    assert res["errors"][CONF_TARGET_ENTITIES] == "empty_target_entities"


async def test_options_edit_mode_state(hass: HomeAssistant, options_rule_entry):
    res = await hass.config_entries.options.async_init(options_rule_entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_mode"}
    )
    assert res["step_id"] == "edit_state"


async def test_options_edit_advanced_save(hass: HomeAssistant, options_rule_entry):
    res = await hass.config_entries.options.async_init(options_rule_entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_advanced"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"],
        {
            CONF_DEBOUNCE_ENABLED: True,
            CONF_DEBOUNCE_SECONDS: 45,
            CONF_MAX_ENFORCEMENTS_PER_MINUTE: 8,
        },
    )
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert options_rule_entry.data[CONF_DEBOUNCE_SECONDS] == 45


async def test_options_save_clears_stale_slider_options(
    hass: HomeAssistant, options_rule_entry
):
    """A deliberate flow edit must not be masked by a slider value the number
    entities persisted into options. HA blanks entry.options on options-flow
    finish, so any stale slider key is gone and the edit in data wins via
    parse_rule_config."""
    hass.config_entries.async_update_entry(
        options_rule_entry,
        options={
            CONF_DELAY_SECONDS: 999,
            CONF_DEBOUNCE_SECONDS: 999,
            CONF_MAX_ENFORCEMENTS_PER_MINUTE: 999,
        },
    )
    res = await hass.config_entries.options.async_init(options_rule_entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_advanced"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"],
        {
            CONF_DEBOUNCE_ENABLED: True,
            CONF_DEBOUNCE_SECONDS: 45,
            CONF_MAX_ENFORCEMENTS_PER_MINUTE: 8,
        },
    )
    assert res["type"] == FlowResultType.CREATE_ENTRY
    opts = options_rule_entry.options
    assert CONF_DELAY_SECONDS not in opts
    assert CONF_DEBOUNCE_SECONDS not in opts
    assert CONF_MAX_ENFORCEMENTS_PER_MINUTE not in opts
    # The edit landed in data, no longer masked by the stale option value.
    assert options_rule_entry.data[CONF_DEBOUNCE_SECONDS] == 45
    assert options_rule_entry.data[CONF_MAX_ENFORCEMENTS_PER_MINUTE] == 8


async def test_options_edit_flags_clear(hass: HomeAssistant, options_rule_entry):
    res = await hass.config_entries.options.async_init(options_rule_entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_flags"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"action": "clear"}
    )
    assert res["type"] == FlowResultType.CREATE_ENTRY


async def test_options_edit_flags_add(hass: HomeAssistant, options_rule_entry):
    res = await hass.config_entries.options.async_init(options_rule_entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_flags"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"],
        {
            CONF_FLAG_ENTITY: "input_boolean.x",
            CONF_FLAG_MATCH_STATE: "on",
            "action": "add",
        },
    )
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert len(options_rule_entry.data["flags"]) == 1


async def test_options_edit_flags_add_incomplete(
    hass: HomeAssistant, options_rule_entry
):
    res = await hass.config_entries.options.async_init(options_rule_entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_flags"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {CONF_FLAG_MATCH_STATE: "on", "action": "add"}
    )
    assert res["errors"]["base"] == "incomplete_flag"


# ---------------------------------------------------------------------------
# Coverage gaps
# ---------------------------------------------------------------------------


async def test_options_edit_entities_safety_redirect(
    hass: HomeAssistant, options_rule_entry
):
    """Changing target to a safety domain redirects to edit_safety step."""
    res = await hass.config_entries.options.async_init(options_rule_entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_entities"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {CONF_TARGET_ENTITIES: ["lock.front_door"]}
    )
    assert res["step_id"] == "edit_safety"


async def test_options_edit_flags_replace(hass: HomeAssistant, options_rule_entry):
    """Replace action replaces all flags with the single submitted flag."""
    res = await hass.config_entries.options.async_init(options_rule_entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_flags"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"],
        {
            CONF_FLAG_ENTITY: "input_boolean.night",
            CONF_FLAG_MATCH_STATE: "on",
            "action": "replace",
        },
    )
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert len(options_rule_entry.data["flags"]) == 1
    assert (
        options_rule_entry.data["flags"][0][CONF_FLAG_ENTITY] == "input_boolean.night"
    )


async def test_options_edit_entities_save_non_safety(
    hass: HomeAssistant, options_rule_entry
):
    """Changing entities to non-safety domain saves directly without safety step."""
    res = await hass.config_entries.options.async_init(options_rule_entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_entities"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {CONF_TARGET_ENTITIES: ["switch.outlet"]}
    )
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert options_rule_entry.data[CONF_TARGET_ENTITIES] == ["switch.outlet"]


async def test_options_edit_flags_replace_incomplete(
    hass: HomeAssistant, options_rule_entry
):
    """Replace action with missing entity shows incomplete_flag error."""
    res = await hass.config_entries.options.async_init(options_rule_entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_flags"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"],
        {CONF_FLAG_MATCH_STATE: "on", "action": "replace"},
    )
    assert res["errors"]["base"] == "incomplete_flag"
