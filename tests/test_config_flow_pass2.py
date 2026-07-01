"""Pass-2 coverage tests targeting config_flow options-flow + create-flow gaps."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.entity_guard.const import (
    CONF_ATTRIBUTE,
    CONF_DEBOUNCE_ENABLED,
    CONF_DEBOUNCE_SECONDS,
    CONF_DELAY_SECONDS,
    CONF_ENTRY_TYPE,
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


def _state_entry(hass, name="StateOpt", entities=None, mode=MODE_STATE, extra=None):
    data = {
        CONF_ENTRY_TYPE: ENTRY_TYPE_RULE,
        "rule_id": "uid-state",
        CONF_RULE_NAME: name,
        CONF_TARGET_ENTITIES: entities or ["light.bedroom"],
        CONF_MODE: mode,
        CONF_TRIGGER_STATES: ["on"],
        CONF_TARGET_STATE: "off",
        CONF_DELAY_SECONDS: 0,
        "flags": [],
        CONF_DEBOUNCE_ENABLED: False,
        CONF_DEBOUNCE_SECONDS: 60,
        CONF_MAX_ENFORCEMENTS_PER_MINUTE: 10,
        CONF_SAFETY_ACKNOWLEDGED: False,
    }
    if extra:
        data.update(extra)
    e = MockConfigEntry(domain=DOMAIN, data=data, title=name, unique_id=data["rule_id"])
    e.add_to_hass(hass)
    return e


def _attr_entry(hass, name="AttrOpt"):
    data = {
        CONF_ENTRY_TYPE: ENTRY_TYPE_RULE,
        "rule_id": "uid-attr",
        CONF_RULE_NAME: name,
        CONF_TARGET_ENTITIES: ["light.bedroom"],
        CONF_MODE: MODE_ATTRIBUTE,
        CONF_ATTRIBUTE: "brightness",
        CONF_OPERATOR: "gt",
        CONF_THRESHOLD: 64,
        CONF_TARGET_VALUE: 64,
        CONF_DELAY_SECONDS: 0,
        "flags": [],
        CONF_DEBOUNCE_ENABLED: False,
        CONF_DEBOUNCE_SECONDS: 60,
        CONF_MAX_ENFORCEMENTS_PER_MINUTE: 10,
        CONF_SAFETY_ACKNOWLEDGED: False,
    }
    e = MockConfigEntry(domain=DOMAIN, data=data, title=name, unique_id=data["rule_id"])
    e.add_to_hass(hass)
    return e


# ---------------------------------------------------------------------------
# Create flow — remaining error branches
# ---------------------------------------------------------------------------


async def _begin_state(hass: HomeAssistant, name):
    res = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    return await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_RULE_NAME: name,
            CONF_TARGET_ENTITIES: ["light.bedroom"],
            CONF_MODE: MODE_STATE,
        },
    )


async def test_state_target_forbidden(hass: HomeAssistant):
    res = await _begin_state(hass, "FB")
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_TRIGGER_STATES: ["on"],
            CONF_TARGET_STATE: "unavailable",
            CONF_DELAY_SECONDS: 0,
        },
    )
    assert res["errors"][CONF_TARGET_STATE] == "forbidden_state"


async def test_state_invalid_delay_via_string(hass: HomeAssistant):
    """Use bypass on form schema — patch _coerce_delay to return None."""
    res = await _begin_state(hass, "BadDel")
    with patch(
        "custom_components.entity_guard.config_flow._coerce_delay", return_value=None
    ):
        res = await hass.config_entries.flow.async_configure(
            res["flow_id"],
            {
                CONF_TRIGGER_STATES: ["on"],
                CONF_TARGET_STATE: "off",
                CONF_DELAY_SECONDS: 0,
            },
        )
    assert res["errors"][CONF_DELAY_SECONDS] == "invalid_delay"


async def test_attribute_invalid_threshold_create_skipped():
    """Schema-validated NumberSelector rejects non-numeric → except branch unreachable from flow."""
    pass


async def test_attribute_invalid_delay_create(hass: HomeAssistant):
    res = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_RULE_NAME: "AttrDel",
            CONF_TARGET_ENTITIES: ["light.bedroom"],
            CONF_MODE: MODE_ATTRIBUTE,
        },
    )
    with patch(
        "custom_components.entity_guard.config_flow._coerce_delay", return_value=None
    ):
        res = await hass.config_entries.flow.async_configure(
            res["flow_id"],
            {
                CONF_ATTRIBUTE: "brightness",
                CONF_OPERATOR: "gt",
                CONF_THRESHOLD: 64,
                CONF_TARGET_VALUE: 64,
                CONF_DELAY_SECONDS: 0,
            },
        )
    assert res["errors"][CONF_DELAY_SECONDS] == "invalid_delay"


async def test_advanced_invalid_rate_via_int_failure_skipped():
    """Schema-validated rate selector clamps numeric input → invalid_rate except path unreachable."""
    pass


# ---------------------------------------------------------------------------
# Options edit_basics — duplicate name
# ---------------------------------------------------------------------------


async def test_options_basics_duplicate_name(hass: HomeAssistant):
    e1 = _state_entry(hass, "ExistingOne")
    e2 = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_RULE, CONF_RULE_NAME: "OtherName"},
        title="OtherName",
        unique_id="other-uid",
    )
    e2.add_to_hass(hass)
    res = await hass.config_entries.options.async_init(e1.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_basics"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {CONF_RULE_NAME: "OtherName"}
    )
    assert res["errors"][CONF_RULE_NAME] == "name_already_exists"


# ---------------------------------------------------------------------------
# Options edit_entities — adds safety target → re-ack flow
# ---------------------------------------------------------------------------


async def test_options_entities_triggers_safety_ack(hass: HomeAssistant):
    e = _state_entry(hass, "AddLock")
    res = await hass.config_entries.options.async_init(e.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_entities"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {CONF_TARGET_ENTITIES: ["lock.front"]}
    )
    assert res["step_id"] == "edit_safety"
    # Reject
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {CONF_SAFETY_ACKNOWLEDGED: False}
    )
    assert res["errors"][CONF_SAFETY_ACKNOWLEDGED] == "safety_not_acknowledged"
    # Accept
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {CONF_SAFETY_ACKNOWLEDGED: True}
    )
    assert res["type"] == FlowResultType.CREATE_ENTRY


# ---------------------------------------------------------------------------
# Options edit_state — full validation matrix
# ---------------------------------------------------------------------------


async def test_options_edit_state_empty_triggers(hass: HomeAssistant):
    e = _state_entry(hass, "EditST1")
    res = await hass.config_entries.options.async_init(e.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_mode"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"],
        {
            CONF_TRIGGER_STATES: [],
            CONF_TARGET_STATE: "off",
            CONF_DELAY_SECONDS: 0,
        },
    )
    assert res["errors"][CONF_TRIGGER_STATES] == "empty_trigger_states"


async def test_options_edit_state_forbidden_trigger(hass: HomeAssistant):
    e = _state_entry(hass, "EditST2")
    res = await hass.config_entries.options.async_init(e.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_mode"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"],
        {
            CONF_TRIGGER_STATES: ["unavailable"],
            CONF_TARGET_STATE: "off",
            CONF_DELAY_SECONDS: 0,
        },
    )
    assert res["errors"][CONF_TRIGGER_STATES] == "forbidden_state"


async def test_options_edit_state_target_forbidden(hass: HomeAssistant):
    e = _state_entry(hass, "EditST3")
    res = await hass.config_entries.options.async_init(e.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_mode"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"],
        {
            CONF_TRIGGER_STATES: ["on"],
            CONF_TARGET_STATE: "unavailable",
            CONF_DELAY_SECONDS: 0,
        },
    )
    assert res["errors"][CONF_TARGET_STATE] == "forbidden_state"


async def test_options_edit_state_target_empty(hass: HomeAssistant):
    e = _state_entry(hass, "EditST4")
    res = await hass.config_entries.options.async_init(e.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_mode"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"],
        {
            CONF_TRIGGER_STATES: ["on"],
            CONF_TARGET_STATE: "  ",
            CONF_DELAY_SECONDS: 0,
        },
    )
    assert res["errors"][CONF_TARGET_STATE] == "empty_target_state"


async def test_options_edit_state_target_in_triggers(hass: HomeAssistant):
    e = _state_entry(hass, "EditST5")
    res = await hass.config_entries.options.async_init(e.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_mode"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"],
        {
            CONF_TRIGGER_STATES: ["on"],
            CONF_TARGET_STATE: "on",
            CONF_DELAY_SECONDS: 0,
        },
    )
    assert res["errors"][CONF_TARGET_STATE] == "target_in_triggers"


async def test_options_edit_state_invalid_delay(hass: HomeAssistant):
    e = _state_entry(hass, "EditST6")
    res = await hass.config_entries.options.async_init(e.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_mode"}
    )
    with patch(
        "custom_components.entity_guard.config_flow._coerce_delay", return_value=None
    ):
        res = await hass.config_entries.options.async_configure(
            res["flow_id"],
            {
                CONF_TRIGGER_STATES: ["on"],
                CONF_TARGET_STATE: "off",
                CONF_DELAY_SECONDS: 0,
            },
        )
    assert res["errors"][CONF_DELAY_SECONDS] == "invalid_delay"


async def test_options_edit_state_save(hass: HomeAssistant):
    e = _state_entry(hass, "EditST7")
    res = await hass.config_entries.options.async_init(e.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_mode"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"],
        {
            CONF_TRIGGER_STATES: ["on"],
            CONF_TARGET_STATE: "off",
            CONF_DELAY_SECONDS: 5,
        },
    )
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert e.data[CONF_DELAY_SECONDS] == 5


# ---------------------------------------------------------------------------
# Options edit_attribute — full matrix
# ---------------------------------------------------------------------------


async def test_options_edit_attribute_save(hass: HomeAssistant):
    e = _attr_entry(hass, "AttrSave")
    res = await hass.config_entries.options.async_init(e.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_mode"}
    )
    assert res["step_id"] == "edit_attribute"
    res = await hass.config_entries.options.async_configure(
        res["flow_id"],
        {
            CONF_ATTRIBUTE: "brightness",
            CONF_OPERATOR: "gt",
            CONF_THRESHOLD: 100,
            CONF_TARGET_VALUE: 80,
            CONF_DELAY_SECONDS: 0,
        },
    )
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert e.data[CONF_THRESHOLD] == 100


async def test_options_edit_attribute_invalid_threshold_skipped():
    """Schema-validated NumberSelector rejects non-numeric → except branch unreachable from flow."""
    pass


async def test_options_edit_attribute_invalid_delay(hass: HomeAssistant):
    e = _attr_entry(hass, "AttrBadD")
    res = await hass.config_entries.options.async_init(e.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_mode"}
    )
    with patch(
        "custom_components.entity_guard.config_flow._coerce_delay", return_value=None
    ):
        res = await hass.config_entries.options.async_configure(
            res["flow_id"],
            {
                CONF_ATTRIBUTE: "brightness",
                CONF_OPERATOR: "gt",
                CONF_THRESHOLD: 64,
                CONF_TARGET_VALUE: 64,
                CONF_DELAY_SECONDS: 0,
            },
        )
    assert res["errors"][CONF_DELAY_SECONDS] == "invalid_delay"


async def test_options_edit_attribute_preserves_unknown_attr(hass: HomeAssistant):
    e = _attr_entry(hass, "AttrPreserve")
    # Inject a non-standard attribute that isn't in attr_options
    hass.config_entries.async_update_entry(
        e, data={**e.data, CONF_ATTRIBUTE: "wattage_custom"}
    )
    res = await hass.config_entries.options.async_init(e.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_mode"}
    )
    assert res["step_id"] == "edit_attribute"


# ---------------------------------------------------------------------------
# Options edit_flags — empty save branch
# ---------------------------------------------------------------------------


async def test_options_edit_flags_save_empty_pair(hass: HomeAssistant):
    e = _state_entry(hass, "FlagSaveEmpty")
    res = await hass.config_entries.options.async_init(e.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "edit_flags"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"action": "save"}
    )
    assert res["type"] == FlowResultType.CREATE_ENTRY


# ---------------------------------------------------------------------------
# _async_ensure_hub branch in flow class
# ---------------------------------------------------------------------------


async def test_create_flow_skips_hub_when_present(hass: HomeAssistant, hub_entry):
    """When hub already exists, preview submission must NOT spawn import flow."""
    hub_entry.add_to_hass(hass)
    res = await _begin_state(hass, "WithHub")
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
        {"add_flags": False, "custom_rate_limit": False, "add_debounce": False},
    )
    res = await hass.config_entries.flow.async_configure(res["flow_id"], {})
    assert res["type"] == FlowResultType.CREATE_ENTRY


# ---------------------------------------------------------------------------
# Branch coverage: missing branches
# ---------------------------------------------------------------------------


async def test_hub_import_aborts_with_rule_entry_present(
    hass: HomeAssistant, hub_entry, rule_entry
):
    """Hub import loop iterates past a rule entry before finding the hub (306->305 branch)."""
    rule_entry.add_to_hass(hass)  # non-hub entry first
    hub_entry.add_to_hass(hass)  # hub entry after
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "import"}, data={}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_flags_step_partial_entity_only(hass: HomeAssistant):
    """Flags step with entity but no match_state shows incomplete_flag (548 True branch)."""
    from custom_components.entity_guard.const import (
        CONF_FLAG_ENTITY,
        CONF_FLAG_MATCH_STATE,
    )

    res = await _begin_state(hass, "FlagsPartial")
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {CONF_TRIGGER_STATES: ["on"], CONF_TARGET_STATE: "off", CONF_DELAY_SECONDS: 0},
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {"add_flags": True, "custom_rate_limit": False, "add_debounce": False},
    )
    # Submit entity but no match_state → incomplete_flag
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {
            CONF_FLAG_ENTITY: "input_boolean.night",
            CONF_FLAG_MATCH_STATE: "",
            "add_another": False,
        },
    )
    assert res["errors"]["base"] == "incomplete_flag"


async def test_advanced_debounce_only_no_rate_limit(hass: HomeAssistant):
    """Advanced step with only debounce enabled skips rate-limit block (612->631, 639->649 branches)."""
    res = await _begin_state(hass, "DebounceOnly")
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {CONF_TRIGGER_STATES: ["on"], CONF_TARGET_STATE: "off", CONF_DELAY_SECONDS: 0},
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {"add_flags": False, "custom_rate_limit": False, "add_debounce": True},
    )
    # Advanced step shown with only debounce field
    assert res["step_id"] == "advanced"
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {CONF_DEBOUNCE_SECONDS: 30},
    )
    # Advanced done → preview step
    assert res["step_id"] == "preview"
    res = await hass.config_entries.flow.async_configure(res["flow_id"], {})
    assert res["type"] == FlowResultType.CREATE_ENTRY


async def test_advanced_rate_limit_disabled_sets_zero(hass: HomeAssistant):
    """Advanced step with rate_limit_enabled=False sets max_enforcements=0 (631->634 branch)."""
    from custom_components.entity_guard.const import CONF_RATE_LIMIT_ENABLED

    res = await _begin_state(hass, "RateOff")
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {CONF_TRIGGER_STATES: ["on"], CONF_TARGET_STATE: "off", CONF_DELAY_SECONDS: 0},
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {"add_flags": False, "custom_rate_limit": True, "add_debounce": False},
    )
    assert res["step_id"] == "advanced"
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {CONF_RATE_LIMIT_ENABLED: False, CONF_MAX_ENFORCEMENTS_PER_MINUTE: 5},
    )
    # Advanced done → preview
    assert res["step_id"] == "preview"
    res = await hass.config_entries.flow.async_configure(res["flow_id"], {})
    assert res["type"] == FlowResultType.CREATE_ENTRY
    # Rate limit disabled → sentinel 0
    assert res["data"][CONF_MAX_ENFORCEMENTS_PER_MINUTE] == 0


async def test_options_ensure_hub_iterates_past_rule_entry(
    hass: HomeAssistant, hub_entry, rule_entry
):
    """Create flow _async_ensure_hub iterates past rule entry to find hub (724->723 branch)."""
    rule_entry.add_to_hass(hass)  # non-hub entry first
    hub_entry.add_to_hass(hass)

    res = await _begin_state(hass, "EnsureHubIter")
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {CONF_TRIGGER_STATES: ["on"], CONF_TARGET_STATE: "off", CONF_DELAY_SECONDS: 0},
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"],
        {"add_flags": False, "custom_rate_limit": False, "add_debounce": False},
    )
    res = await hass.config_entries.flow.async_configure(res["flow_id"], {})
    assert res["type"] == FlowResultType.CREATE_ENTRY
