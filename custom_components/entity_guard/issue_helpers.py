"""Repair flows for Entity Guard integration."""

from __future__ import annotations

import logging
from urllib.parse import quote

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)
from homeassistant.loader import IntegrationNotLoaded, async_get_loaded_integration

from .const import DOMAIN
from .models import parse_rule_config

_LOGGER = logging.getLogger(__name__)

ISSUE_FLAG_ENTITY_MISSING = "flag_entity_missing"
ISSUE_TARGET_ENTITY_MISSING = "target_entity_missing"
ISSUE_UNSUPPORTED_DOMAIN = "unsupported_domain"
ISSUE_UNSUPPORTED_STATE = "unsupported_state"

# GitHub issue-form template the "Learn more" link opens (file lives at
# .github/ISSUE_TEMPLATE/unsupported_domain.yml). Appended to the manifest's
# issue_tracker so the slug survives a repo rename (single source of truth).
# &domain= prefills the form's `domain` input (GitHub issue-form query params
# key on the field `id`) so the user doesn't retype what the card already knows.
_UNSUPPORTED_DOMAIN_TEMPLATE_QUERY = (
    "/new?template=unsupported_domain.yml&labels=enhancement,unsupported-domain"
)

# Sibling template for the state-level gap (.github/ISSUE_TEMPLATE/unsupported_state.yml).
# &domain= and &state= prefill the form's `domain` and `state` inputs.
_UNSUPPORTED_STATE_TEMPLATE_QUERY = (
    "/new?template=unsupported_state.yml&labels=enhancement,unsupported-state"
)


def missing_flags_issue_id(entry_id: str) -> str:
    """Return the repair-issue ID for a rule's missing flag entities."""
    return f"{entry_id}_missing_flags"


def missing_targets_issue_id(entry_id: str) -> str:
    """Return the repair-issue ID for a rule's missing target entities."""
    return f"{entry_id}_missing_targets"


async def async_check_missing_flag_entities(hass: HomeAssistant, entry_id: str) -> None:
    """Check if any flag entities are missing and create repair issues."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return

    try:
        config = parse_rule_config(entry)
    except Exception:
        _LOGGER.exception("Could not parse config for entry %s", entry_id)
        return

    issue_id = missing_flags_issue_id(entry_id)
    if not config.flags:
        _LOGGER.debug("No flags configured for rule %s", entry_id)
        async_delete_issue(hass, DOMAIN, issue_id)
        return

    ent_reg = er.async_get(hass)
    # An entity is "present" if it is in the registry OR the state machine. Rules
    # enforce via hass.states, and legitimate state-only entities (YAML group.*,
    # template/MQTT/command_line without a unique_id) never appear in the registry
    # — checking the registry alone would flag a working target/flag as missing
    # forever. Only flag an entity absent from BOTH.
    missing_flags: list[str] = [
        flag.entity
        for flag in config.flags
        if ent_reg.async_get(flag.entity) is None
        and hass.states.get(flag.entity) is None
    ]

    if missing_flags:
        _LOGGER.warning(
            "Flag entities missing for rule '%s': %s",
            entry.title,
            ", ".join(missing_flags),
        )
        async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=IssueSeverity.WARNING,
            translation_key=ISSUE_FLAG_ENTITY_MISSING,
            translation_placeholders={
                "rule_name": entry.title,
                "missing_entities": ", ".join(missing_flags),
            },
        )
    else:
        _LOGGER.debug("Flag validation passed for rule %s", entry_id)
        async_delete_issue(hass, DOMAIN, issue_id)


async def async_check_missing_target_entities(
    hass: HomeAssistant, entry_id: str
) -> None:
    """Check if any TARGET entities are missing and create/clear a repair issue.

    A guarded target that is renamed or removed leaves the rule watching a dead
    entity_id that never fires again — the rule silently stops enforcing (worst
    case for a kill-switch rule). Surface a WARNING repair issue so the user
    re-points or recreates the rule; we deliberately do NOT auto-rewrite the
    config (a rename often keeps the entity, and silently re-pointing a
    kill-switch at the wrong entity is worse than a visible repair). Mirrors
    async_check_missing_flag_entities; self-clearing.
    """
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return

    try:
        config = parse_rule_config(entry)
    except Exception:
        _LOGGER.exception("Could not parse config for entry %s", entry_id)
        return

    issue_id = missing_targets_issue_id(entry_id)
    if not config.target_entities:
        _LOGGER.debug("No target entities configured for rule %s", entry_id)
        async_delete_issue(hass, DOMAIN, issue_id)
        return

    ent_reg = er.async_get(hass)
    # Present if in the registry OR the state machine (see the flag check above):
    # a state-only target (group.*, template/MQTT without unique_id) enforces fine
    # and must not be flagged missing just because it has no registry entry.
    missing_targets: list[str] = [
        target
        for target in config.target_entities
        if ent_reg.async_get(target) is None and hass.states.get(target) is None
    ]

    if missing_targets:
        _LOGGER.warning(
            "Target entities missing for rule '%s': %s",
            entry.title,
            ", ".join(missing_targets),
        )
        async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=IssueSeverity.WARNING,
            translation_key=ISSUE_TARGET_ENTITY_MISSING,
            translation_placeholders={
                "rule_name": entry.title,
                "missing_entities": ", ".join(missing_targets),
            },
        )
    else:
        _LOGGER.debug("Target validation passed for rule %s", entry_id)
        async_delete_issue(hass, DOMAIN, issue_id)


def unsupported_domain_issue_id(domain: str) -> str:
    """Return the repair-issue ID for a domain Entity Guard cannot control.

    Keyed by DOMAIN, not entry_id (unlike the missing-entity helpers above):
    the gap is per-domain, not per-rule — expanding the service map fixes every
    rule targeting that domain at once, so one card per domain is right.
    """
    return f"{ISSUE_UNSUPPORTED_DOMAIN}_{domain}"


def async_raise_unsupported_domain(hass: HomeAssistant, domain: str) -> None:
    """Raise a repair issue when a rule targets a domain with no service mapping.

    Fires from the enforce path's no_service_mapping skip: the user built (by
    typing a custom non-on/off state on a domain absent from DOMAIN_SERVICE_MAP
    and DOMAIN_MODE_SERVICE_MAP) a rule Entity Guard silently can't drive. The
    "Learn more" link opens a GitHub issue-form template asking for the domain /
    desired state / example entity so the service map can be expanded.

    No self-clear: an unmapped domain only becomes mapped via a code update +
    restart, and a restart clears repair issues anyway (unlike the missing-entity
    helpers, whose condition can flip at runtime). Add clearing only if it nags.
    """
    learn_more_url: str | None = None
    # ponytail: the enforce path only runs while loaded, so the lookup normally
    # succeeds; the guard covers the narrow enforce-during-teardown/reload window
    # where it would raise and crash enforce. Skip the link, keep the card.
    try:
        issue_tracker = async_get_loaded_integration(hass, DOMAIN).issue_tracker
    except IntegrationNotLoaded:
        issue_tracker = None
    if issue_tracker:
        # issue_tracker is ".../issues"; the form template lives at ".../issues/new".
        # Append &domain= so GitHub prefills the form's `domain` input (quote in
        # case a domain ever contains a URL-unsafe char).
        learn_more_url = (
            issue_tracker
            + _UNSUPPORTED_DOMAIN_TEMPLATE_QUERY
            + f"&domain={quote(domain)}"
        )

    async_create_issue(
        hass,
        DOMAIN,
        unsupported_domain_issue_id(domain),
        is_fixable=False,
        severity=IssueSeverity.WARNING,
        translation_key=ISSUE_UNSUPPORTED_DOMAIN,
        translation_placeholders={"domain": domain},
        learn_more_url=learn_more_url,
    )


def unsupported_state_issue_id(domain: str, state: str) -> str:
    """Return the repair-issue ID for a state Entity Guard cannot drive a domain to.

    Keyed by (domain, state): the domain IS controllable, but this one state has
    no service mapping (or is absent from the device's valid-state list). One card
    per (domain, state) pair — expanding support for that state fixes every rule
    requesting it at once.
    """
    return f"{ISSUE_UNSUPPORTED_STATE}_{domain}_{state}"


def async_raise_unsupported_state(hass: HomeAssistant, domain: str, state: str) -> None:
    """Raise a repair issue when a rule wants a state the domain can't be set to.

    Fires from the enforce path's no_service_mapping skip when the domain IS in a
    service map but this specific state resolves to no service — either a non-on/off
    state on a plain DOMAIN_SERVICE_MAP domain, or a mode-domain state absent from
    the device's valid-state attribute (climate hvac_modes etc.). Distinct from
    async_raise_unsupported_domain: the domain is controllable, only the state is
    the gap. The "Learn more" link opens a GitHub issue-form prefilled with the
    domain and the state.

    No self-clear: the same rationale as async_raise_unsupported_domain — a new
    state mapping lands via a code update + restart, which clears repair issues.
    """
    learn_more_url: str | None = None
    # ponytail: the enforce path only runs while loaded; the guard covers the narrow
    # enforce-during-teardown/reload window where the lookup would raise. Skip the
    # link, keep the card.
    try:
        issue_tracker = async_get_loaded_integration(hass, DOMAIN).issue_tracker
    except IntegrationNotLoaded:
        issue_tracker = None
    if issue_tracker:
        learn_more_url = (
            issue_tracker
            + _UNSUPPORTED_STATE_TEMPLATE_QUERY
            + f"&domain={quote(domain)}&state={quote(state)}"
        )

    async_create_issue(
        hass,
        DOMAIN,
        unsupported_state_issue_id(domain, state),
        is_fixable=False,
        severity=IssueSeverity.WARNING,
        translation_key=ISSUE_UNSUPPORTED_STATE,
        translation_placeholders={"domain": domain, "state": state},
        learn_more_url=learn_more_url,
    )
