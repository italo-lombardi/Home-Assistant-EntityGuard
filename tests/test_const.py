"""Sanity tests for const module."""

from __future__ import annotations

from custom_components.entity_guard import const


def test_domain() -> None:
    assert const.DOMAIN == "entity_guard"


def test_status_values_count() -> None:
    assert len(const.STATUS_VALUES) == 10
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


def test_status_values_have_selector_translations() -> None:
    import json
    import pathlib

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
