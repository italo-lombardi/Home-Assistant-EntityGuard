"""Live smoke for the verify-CONFIRMED signal (feat/enforced-verified-signal).

Proves the three LOCKED behaviors of the new per-rule
`binary_sensor.<rule>_recently_enforced_verified` sensor over the running
container, end to end:

  1. verify-ON + a genuine success → the verified sensor pulses ON. This is the
     user-facing proof that EVENT_VERIFIED fired: `_fire_verified` arms the
     sensor on the same confirm, so an observed ON edge == the event fired on a
     confirmed re-read (the event itself is additionally locked by unit tests).
  2. verify-ON → the verified sensor EXISTS (recently_enforced stays alongside).
  3. verify ON->OFF via the options flow → the verified sensor's registry row is
     DISABLED (not removed): its state drops from the state machine. Reversible.

Run inside the container:
  docker cp scripts/smoke_verified_signal.py \
    serene_booth:/workspaces/home-assistant-core/config/smoke_verified_signal.py
  docker exec serene_booth /home/vscode/.local/ha-venv/bin/python \
    /workspaces/home-assistant-core/config/smoke_verified_signal.py
Exit 0 = all hard assertions green, 1 = a failure.
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "/workspaces/home-assistant-core/config")
import smoke_follow as sf  # shared helpers, resolved inside the container
import smoke_verify_enforce as sv  # reuse create_verify_rule + status_sensor_for

TARGET = sv.TARGET


def set_verify_via_options(token: str, entry_id: str, verify: bool) -> None:
    """Walk the options flow and set verify_enabled, leaving all else default.

    The options flow opens on a MENU (edit_basics/edit_mode/edit_entities/
    edit_flags/edit_advanced); verify_enabled lives under edit_advanced, next to
    the debounce/rate-limit fields. Pick that branch and submit the full advanced
    payload with the other fields at safe defaults (they are additionally covered
    by the options-flow unit tests; this smoke only toggles verify).
    """
    flow = sf.api(
        token,
        "POST",
        "/api/config/config_entries/options/flow",
        {"handler": entry_id},
    )
    flow_id = flow["flow_id"]
    result = sf.api(
        token,
        "POST",
        f"/api/config/config_entries/options/flow/{flow_id}",
        {"next_step_id": "edit_advanced"},
    )
    sf.guard_form(result)
    result = sf.api(
        token,
        "POST",
        f"/api/config/config_entries/options/flow/{flow_id}",
        {
            "debounce_enabled": False,
            "debounce_seconds": 0,
            "rate_limit_enabled": True,
            "max_enforcements_per_minute": 10,
            "verify_enabled": verify,
        },
    )
    if result.get("type") != "create_entry":
        raise RuntimeError(f"options flow did not finish: {result}")


def main() -> int:
    token = sf.mint_token()
    sf.cleanup(token)

    name = "ea_smoke_verified_signal"
    slug = name
    verified_eid = f"binary_sensor.{slug}_recently_enforced_verified"

    # --- verify-ON rule ----------------------------------------------------
    entry = sv.create_verify_rule(token, name, verify=True)
    sf.check("verify-enabled rule created", bool(entry))
    entry_id = entry
    sv.status_sensor_for(token, slug)  # wait past startup grace
    # The engine IGNORES target events during startup grace (~60s); triggering
    # before it arms sees no enforce -> no verify -> no pulse (a false FAIL).
    armed = sf.wait_for_armed(token, "fixed_state", [TARGET])
    sf.check("rule left startup grace (armed)", armed)
    time.sleep(2)

    # (2) the verified sensor exists while verify is ON; recently_enforced too.
    plain = sf.get_state(token, f"binary_sensor.{slug}_recently_enforced")
    verified = sf.get_state(token, verified_eid)
    sf.check("recently_enforced sensor present (unchanged)", bool(plain))
    sf.check("recently_enforced_verified sensor present (verify on)", bool(verified))

    # (1) genuine success → verified sensor pulses ON.
    # Paint the target to the NON-desired state ("off") via a raw /api/states
    # injection — this trips the trigger (rule: trigger off -> target on) WITHOUT
    # driving the target to desired ourselves. The ENGINE then enforces "on" via
    # the real service; the verify re-read finds "on" -> confirm -> _fire_verified
    # -> the verified sensor arms ON for 30s. (Driving "on" ourselves would leave
    # the engine nothing to enforce -> no verify cycle -> no pulse.)
    sv.force_state(token, TARGET, "off")
    saw_on = False
    deadline = time.time() + 25
    while time.time() < deadline:
        st = sf.get_state(token, verified_eid)
        if st.get("state") == "on":
            saw_on = True
            break
        time.sleep(1)
    sf.check("recently_enforced_verified pulses ON after a confirmed enforce", saw_on)

    # (3) verify ON->OFF -> the verified sensor's registry row is DISABLED:
    # its state leaves the state machine (get_state returns empty/unavailable).
    set_verify_via_options(token, entry_id, verify=False)
    time.sleep(6)  # options edit reloads the entry; platforms re-setup
    gone = False
    deadline = time.time() + 20
    while time.time() < deadline:
        st = sf.get_state(token, verified_eid)
        if not st or st.get("state") in (None, "unavailable"):
            gone = True
            break
        time.sleep(1)
    sf.check("recently_enforced_verified disabled (state gone) on verify-off", gone)
    # recently_enforced stays — only the verified twin is verify-gated.
    still = sf.get_state(token, f"binary_sensor.{slug}_recently_enforced")
    sf.check("recently_enforced still present after verify-off", bool(still))

    sf.cleanup(token)
    if sf.FAILS:
        print(f"\nFAILED: {sf.FAILS}")
        return 1
    print("\nALL GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
