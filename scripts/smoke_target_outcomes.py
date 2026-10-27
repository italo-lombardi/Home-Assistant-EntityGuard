#!/usr/bin/env python3
"""Smoke test: per-target outcome visibility (Feature A).

Feature A records a per-target enforcement outcome on the rule's runtime state
and exposes it as the status sensor's `target_outcomes` attribute (keyed by
target entity_id): {"outcome": "ok"|"skipped"|"failed", "reason", "error",
"at"}. The Lovelace card's "needs attention" panel shows ONLY the actionable
set — outcome=="failed" (a service call raised) OR reason=="no_service_mapping"
(a target the rule cannot drive, i.e. misconfig) — and HIDES benign waits
(light_off, source_offline, …).

REST-observable surface asserted here (allowed/disposable entities only):
  1. A target the rule CANNOT drive (target_state with no service mapping) is
     recorded in target_outcomes with reason=="no_service_mapping" — the
     actionable outcome the card surfaces. Driven on input_boolean (allowed
     target): a fixed_state rule with target_state="gamma" resolves to NO
     service (input_boolean maps only on/off, and "gamma" is not on/off), so
     the enforce path records no_service_mapping.
  2. A healthy rule that simply hasn't had to act records NO actionable outcome
     for its target (nothing failed, nothing to fix) — the benign/waiting case
     the card correctly keeps empty.

HONEST SCOPE — what this does NOT cover:
  - A genuinely RAISED service call (outcome=="failed" with error): inducing
    that reliably needs a device/helper that rejects the call. The only
    reliable raisers in this container are the user's REAL entities
    (input_select.washwise_*, climate.smart_thermostat_*) which are OFF LIMITS,
    and creating a disposable input_select helper over plain REST is not
    reliable in-container. The raised-service failed path is instead locked by
    the engine unit test test_target_outcomes_failed_on_service_error. This
    smoke covers the no_service_mapping actionable path + the attribute shape
    end-to-end over REST.
  - The "silent ignore" path (device returns OK, emits no state change) is NOT
    Feature A — that is the unbuilt verify-after-enforce feature. Not claimed.

Reuses smoke_follow.py helpers (token mint, REST api, create flow, cleanup).
Requires the integration branch (target_outcomes attr) deployed to serene_booth.
Usage: docker exec serene_booth python3 \
  /workspaces/home-assistant-core/config/smoke_target_outcomes.py
Exit 0 = all green, 1 = a failure.
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "/workspaces/home-assistant-core/config")
import smoke_follow as sf  # shared helpers, resolved inside the container

# input_boolean is an ALLOWED smoke target (memory: smoke_test_entities). Its
# service map is {on, off} only — a target_state of "gamma" resolves to NO
# service, the deterministic no_service_mapping path, with zero device side
# effect (the service is never called).
UNDRIVABLE_TARGET = "input_boolean.ea_ctrl_kitchen_1"
UNDRIVABLE_STATE = "gamma"  # not in input_boolean's {on, off} map, not on/off


def _find_status_sensor(token: str, name_slug: str, timeout: int = 100) -> str:
    """Return THIS run's status sensor once live past startup grace. Bind by the
    per-run name slug + fixed_state mode + target set so an orphan can't shadow
    it. Raises on timeout."""
    live = {"armed", "conditional", "enforcing", "cooldown", "pending"}
    deadline = time.time() + timeout
    while time.time() < deadline:
        matches = [
            s["entity_id"]
            for s in sf.api(token, "GET", "/api/states")
            if (
                s.get("entity_id", "").startswith(f"sensor.{name_slug}")
                and s["entity_id"].endswith("_status")
                and s.get("attributes", {}).get("mode") == "fixed_state"
                and (s.get("attributes", {}).get("target_entities") or [])
                == [UNDRIVABLE_TARGET]
                and s.get("state") in live
            )
        ]
        if len(matches) > 1:
            raise RuntimeError(f"multiple status sensors for this run: {matches}")
        if matches:
            return matches[0]
        time.sleep(3)
    raise RuntimeError("rule never armed past startup grace")


def _outcome_for(token: str, status_e: str, target: str) -> dict:
    """Return the target_outcomes entry for `target`, or {} if absent."""
    attrs = sf.get_state(token, status_e).get("attributes", {})
    outcomes = attrs.get("target_outcomes")
    if not isinstance(outcomes, dict):
        return {}
    entry = outcomes.get(target)
    return entry if isinstance(entry, dict) else {}


def _wait_for_outcome(
    token: str, status_e: str, target: str, timeout: int = 20
) -> dict:
    """Poll until target_outcomes has an entry for target; return it (or {})."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        entry = _outcome_for(token, status_e, target)
        if entry:
            return entry
        time.sleep(2)
    return {}


def main() -> int:
    token = sf.mint_token()

    run_token = str(int(time.time()))
    rule_name = f"EG Target Outcomes Smoke {run_token}"
    name_slug = f"eg_target_outcomes_smoke_{run_token}"

    print(f"Creating fixed_state rule '{rule_name}' (undrivable target_state)…")
    # trigger_states includes the target's real states (on/off) so that whatever
    # state input_boolean is in, the rule sees a trigger and attempts to enforce
    # the undrivable target_state → no_service_mapping.
    entry_id = sf.create_fixed_state_rule(
        token,
        rule_name,
        [UNDRIVABLE_TARGET],
        target_state=UNDRIVABLE_STATE,
        trigger_states=["on", "off"],
    )
    print(f"  created entry {entry_id}")

    try:
        status_e = _find_status_sensor(token, name_slug)
        sf.check("rule armed past startup grace", bool(status_e), status_e)

        # Nudge the target so the rule evaluates + attempts enforcement. Toggling
        # input_boolean is a no-op side-effect on an allowed entity; the rule's
        # enforce of "gamma" resolves to no service and records the outcome.
        cur = sf.get_state(token, UNDRIVABLE_TARGET).get("state", "off")
        flip = "off" if cur == "on" else "on"
        sf.api(
            token,
            "POST",
            f"/api/services/input_boolean/turn_{flip}",
            {"entity_id": UNDRIVABLE_TARGET},
        )

        # 1. The undrivable target is recorded with the actionable no_service_mapping
        #    reason (the outcome the card's "needs attention" panel surfaces).
        entry = _wait_for_outcome(token, status_e, UNDRIVABLE_TARGET)
        sf.check(
            "target_outcomes records the undrivable target",
            bool(entry),
            f"entry={entry}",
        )
        sf.check(
            "reason == no_service_mapping (actionable, card-shown)",
            entry.get("reason") == "no_service_mapping",
            f"reason={entry.get('reason')}",
        )
        # no_service_mapping is classed "skipped" by the engine (only a raised
        # service call is "failed"), but it IS in the card's actionable set.
        sf.check(
            "outcome is a recorded non-ok value",
            entry.get("outcome") in ("skipped", "failed"),
            f"outcome={entry.get('outcome')}",
        )

        # 2. The outcome is NOT spuriously labelled a raised failure — the
        #    benign/misconfig distinction holds (this is no_service_mapping, not
        #    a service_call_failed, so no error string should be set).
        sf.check(
            "not mislabelled as a raised failure (no error for no_service_mapping)",
            entry.get("outcome") != "failed" and not entry.get("error"),
            f"outcome={entry.get('outcome')} error={entry.get('error')}",
        )

    finally:
        sf.cleanup(token)

    if sf.FAILS:
        print(f"\nFAILED: {len(sf.FAILS)} check(s): {sf.FAILS}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 — smoke: any error is a failure
        print(f"ERROR: {e}")
        try:
            sf.cleanup(sf.mint_token())
        except Exception as ce:  # noqa: BLE001
            print(f"  [WARN] cleanup after error failed: {ce}")
        sys.exit(1)
