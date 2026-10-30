#!/usr/bin/env python3
"""Smoke test: manual test_enforce on a CONDITIONAL rule runs FULL verify.

PROD BUG (2026-10-30 logbook): pressing the per-rule `test_enforce` button on a
CONDITIONAL rule (its flags are unmet — e.g. an "away" rule while someone is
home) drove the target correctly but verify resolved CANCELLED(flags_unmet)
instead of VERIFIED. A manual press means the user deliberately overrode the
conditions, so verify must judge the DEVICE (verified / unreachable), NOT
re-apply the flags/suppression gates.

This smoke reproduces the exact prod shape over REST:

  1. Create a fixed_state VERIFY rule with ONE flag condition that is NOT met
     (flag entity forced to a state that won't match) → the rule's status is
     "conditional", enforcement is gated off for the AUTOMATIC path.
  2. Force the target OFF (desired is "on" → reachable device, complies on the
     engine's turn_on).
  3. Press `button.<slug>_test_enforce` via REST — the manual force path.
  4. Assert `binary_sensor.<slug>_recently_enforced_verified` pulses ON and
     `binary_sensor.<slug>_recently_enforced_cancelled` does NOT — i.e. the
     forced cycle bypassed the flags_unmet drop and resolved on device state.

The deterministic bypass (both the deadline _fire path and the event path) is
locked by the unit tests. This adds the live REST confirmation on the real
state machine that the prod CSV flagged.

Reuses smoke_follow.py + smoke_verify_enforce.py helpers.
Requires the manual-force fix deployed to serene_booth.
Usage: docker exec serene_booth python3 \
  /workspaces/home-assistant-core/config/smoke_manual_force_verify.py
Exit 0 = all hard assertions green, 1 = a failure.
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "/workspaces/home-assistant-core/config")
import smoke_follow as sf  # shared helpers, resolved inside the container
import smoke_verify_enforce as sve  # status_sensor_for / force_state / TARGET

TARGET = sve.TARGET  # light.living_room_under_cabinet_lights (allowed)
# Flag entity: an allowed toggle. We require it == "on" but force it "off",
# so the flag is UNMET → the rule sits CONDITIONAL (automatic path gated off).
FLAG_ENTITY = "input_boolean.ea_ctrl_kitchen_1"
FLAG_MATCH = "on"


def create_conditional_verify_rule(token: str, name: str) -> str:
    """Create a fixed_state verify rule with ONE unmet flag condition.

    Walks the config flow generically (like sve.create_verify_rule) but opts
    into the flags step and adds a single flag=FLAG_ENTITY match=FLAG_MATCH."""
    flow = sf.api(
        token, "POST", "/api/config/config_entries/flow", {"handler": "entity_guard"}
    )
    flow_id = flow["flow_id"]
    result = sf.api(
        token,
        "POST",
        f"/api/config/config_entries/flow/{flow_id}",
        {"rule_name": name, "target_entities": [TARGET], "mode": "fixed_state"},
    )
    while result.get("type") == "form":
        sf.guard_form(result)
        step = result.get("step_id")
        if step == "state":
            payload = {
                "trigger_states": ["off"],
                "target_state": "on",
                "delay_seconds": 0,
            }
        elif step == "extras":
            # opt into flags AND verify; both are collected after this step
            payload = {"add_flags": True, "verify_enabled": True}
        elif step == "flags":
            # single flag, no "add_another" → flow advances past flags.
            # Flag keys are "entity"/"match_state" (CONF_FLAG_ENTITY / _MATCH_STATE).
            payload = {
                "entity": FLAG_ENTITY,
                "match_state": FLAG_MATCH,
                "add_another": False,
            }
        else:
            payload = {}  # preview — accept defaults
        result = sf.api(
            token, "POST", f"/api/config/config_entries/flow/{flow_id}", payload
        )
    if result.get("type") != "create_entry":
        raise RuntimeError(f"conditional verify create flow did not finish: {result}")
    entry_id = result["result"]["entry_id"]
    sf.CREATED.append(entry_id)
    return entry_id


def press(token: str, entity_id: str) -> None:
    sf.api(token, "POST", "/api/services/button/press", {"entity_id": entity_id})


def pulsed_on(token: str, entity_id: str, timeout: int = 20) -> bool:
    """Return True if entity_id reads 'on' at any point within timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sf.get_state(token, entity_id).get("state") == "on":
            return True
        time.sleep(1)
    return False


def main() -> int:
    token = sf.mint_token()
    sf.cleanup(token)  # clear orphan EG rules from prior runs

    ok = True
    name = "ea_smoke_manual_force"
    slug = name

    # Force the flag UNMET first so the rule is CONDITIONAL from the start.
    sf.api(token, "POST", f"/api/states/{FLAG_ENTITY}", {"state": "off"})

    entry = create_conditional_verify_rule(token, name)
    sf.check("conditional verify rule created", bool(entry))
    ok = ok and bool(entry)

    status = sve.status_sensor_for(token, slug)
    # confirm the rule really is CONDITIONAL (flag unmet), not armed
    cond = sf.wait_for_state(token, status, "conditional", timeout=90)
    sf.check(
        "rule is CONDITIONAL (flag unmet)",
        cond,
        f"status={sf.get_state(token, status).get('state')}",
    )
    ok = ok and cond

    # Reachable device: target OFF now, desired is "on", real light complies.
    sve.force_state(token, TARGET, "off")
    time.sleep(1)

    verified_bs = f"binary_sensor.{slug}_recently_enforced_verified"
    cancelled_bs = f"binary_sensor.{slug}_recently_enforced_cancelled"

    # Press the manual test_enforce button → forced verify cycle.
    press(token, f"button.{slug}_test_enforce")

    # The forced cycle must VERIFY (device reached) and must NOT CANCEL.
    saw_verified = pulsed_on(token, verified_bs, timeout=25)
    saw_cancelled = pulsed_on(token, cancelled_bs, timeout=3)  # already decided by now

    sf.check("manual force → recently_enforced_verified pulsed", saw_verified)
    sf.check(
        "manual force → recently_enforced_cancelled did NOT pulse", not saw_cancelled
    )
    ok = ok and saw_verified and not saw_cancelled

    sf.cleanup(token)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
