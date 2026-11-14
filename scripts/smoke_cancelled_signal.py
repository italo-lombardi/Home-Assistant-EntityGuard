"""Live smoke for the verify-CANCELLED signal (v0.4.2) — run INSIDE serene_booth.

Proves the LOCKED behaviors of the new per-rule
`binary_sensor.<rule>_recently_enforced_cancelled` sensor, its
`entity_guard_enforced_cancelled` event, and its device trigger over the running
container — the NEUTRAL third twin of smoke_verified_signal (success) and
smoke_unreachable_signal (device failure). `cancelled` means a verify cycle was
ARMED then ABANDONED mid-window with no verdict (external condition changed:
suppressed / flags-unmet / rule disabled / master off) — not a device fault.

Five assertions, all HARD (deterministic via the silent-ignore fixture):

  1. verify-ON → all three verify twins EXIST alongside recently_enforced
     (verified, unreachable, cancelled). Regression guard for the lifecycle
     wiring — the cancelled sensor is verify-gated like its siblings.
  2. the cancelled DEVICE TRIGGER is schema-valid and ATTACHES: an automation
     built with `platform: device, type: cancelled` on the rule device installs
     and reports state 'on' (async_attach_trigger accepted the type → generic
     TRIGGER_TYPE_EVENT lookup).
  3. verify ON→OFF via the options flow → the cancelled sensor's registry row is
     DISABLED (state leaves the state machine). recently_enforced survives.
  4. a real forced drop → the cancelled sensor pulses ON. The target is the
     silent-ignore fixture (switch.eg_smoke_silent_ignore): it ACKs turn_on but
     never reaches "on", so the verify cycle stays PENDING the full ~5s window
     and the event confirm can never win. `entity_guard.suppress` mid-window
     therefore lands on the reached-but-dropped path deterministically →
     _fire_cancelled → the cancelled sensor arms ON for 30s. We assert on the
     SENSOR PULSE (the public contract), not the internal drop reason.
  5. the cancelled device-trigger automation flipped SINK on (EVENT_CANCELLED
     reached async_attach_trigger live).

Target is the silent-ignore fixture (sv.SILENT_TARGET); action sink SINK —
memory: smoke_test_entities. Raw /api/states injection trips the trigger;
cleanup drops the rule + automations.

Run inside the container:
  docker cp scripts/smoke_cancelled_signal.py \
    serene_booth:/workspaces/home-assistant-core/config/smoke_cancelled_signal.py
  docker exec serene_booth /home/vscode/.local/ha-venv/bin/python \
    /workspaces/home-assistant-core/config/smoke_cancelled_signal.py
Exit 0 = all assertions green, 1 = a failure.
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "/workspaces/home-assistant-core/config")
import smoke_device_triggers as sdt  # reuse install/delete automation + SINK + device-id
import smoke_follow as sf  # shared helpers, resolved inside the container
import smoke_verified_signal as svs  # reuse set_verify_via_options
import smoke_verify_enforce as sv  # reuse create_verify_rule + force_state + status

TARGET = sv.SILENT_TARGET
DOMAIN = "entity_guard"
MASTER = "switch.entity_guard_hub_master_enabled"


def set_master(token: str, on: bool) -> None:
    """Flip the hub master-enable switch. The engine only arms rules while master
    is ON (else every rule reads 'master_disabled' and wait_for_armed times out).
    Smoke turns it ON up front and restores the pre-run value in `finally` so the
    container is left as found regardless of pass/fail."""
    svc = "turn_on" if on else "turn_off"
    sf.api(token, "POST", f"/api/services/switch/{svc}", {"entity_id": MASTER})


def get_master(token: str) -> str | None:
    return (sf.get_state(token, MASTER) or {}).get("state")


def main() -> int:
    token = sf.mint_token()
    sf.cleanup(token)

    name = "ea_smoke_cancelled_signal"
    slug = name
    cancelled_eid = f"binary_sensor.{slug}_recently_enforced_cancelled"
    verified_eid = f"binary_sensor.{slug}_recently_enforced_verified"
    unreachable_eid = f"binary_sensor.{slug}_recently_enforced_unreachable"
    autos: list[str] = []

    # Snapshot what we perturb so `finally` restores it: the TARGET's real state
    # (force_state injects a virtual value over it — no device command, but leaves
    # the state machine reading the stale value) and the master switch (restored
    # to its pre-run value, NOT hardcoded OFF — the batch may have it ON).
    target_pre = (sf.get_state(token, TARGET) or {}).get("state")
    master_pre = get_master(token)
    set_master(token, True)
    time.sleep(2)

    entry_id = sv.create_verify_rule_on(
        token, name, [TARGET], ["off"], "on", verify=True
    )
    sf.check("verify-enabled rule created", bool(entry_id))
    try:
        sv.status_sensor_for(token, slug)  # wait past startup grace
        # Engine IGNORES target events during startup grace (~60s); forcing a drop
        # before it arms sees no enforce → no verify → no pulse (a false FAIL).
        armed = sf.wait_for_armed(token, "fixed_state", [TARGET])
        sf.check("rule left startup grace (armed)", armed)
        time.sleep(2)

        # --- HARD (1): all three verify twins exist while verify is ON ----------
        plain = sf.get_state(token, f"binary_sensor.{slug}_recently_enforced")
        verified = sf.get_state(token, verified_eid)
        unreachable = sf.get_state(token, unreachable_eid)
        cancelled = sf.get_state(token, cancelled_eid)
        sf.check("recently_enforced sensor present (unchanged)", bool(plain))
        sf.check(
            "recently_enforced_verified sensor present (verify on)", bool(verified)
        )
        sf.check(
            "recently_enforced_unreachable sensor present (verify on)",
            bool(unreachable),
        )
        sf.check(
            "recently_enforced_cancelled sensor present (verify on)", bool(cancelled)
        )

        # --- HARD (2): the cancelled device trigger is schema-valid + attaches --
        device_id = sdt.rule_device_id(token, slug)
        sf.check("resolved rule device_id for triggers", bool(device_id))
        sdt.reset_sink(token)
        aid_c = "eg_smoke_cancelled_devtrig"
        autos.append(aid_c)
        sdt.install_device_trigger_automation(token, aid_c, device_id, "cancelled")
        sf.check(
            "cancelled automation installed (type accepted by device_trigger schema)",
            sf.get_state(token, f"automation.{aid_c}").get("state") == "on",
        )

        # --- HARD (4)+(5): force a drop, catch the pulse + trigger fire --------
        # TARGET is the silent-ignore fixture: it ACKs turn_on but never reaches
        # "on", so the verify cycle stays PENDING the full ~5s window and the
        # event confirm can never win. Suppressing mid-window therefore lands the
        # cycle on the dropped path deterministically → _fire_cancelled → the
        # cancelled sensor pulses ON for 30s. We assert on the SENSOR PULSE (the
        # public contract); the exact internal drop reason is not asserted.
        sdt.reset_sink(token)
        sv.force_state(token, TARGET, "off")
        sf.api(
            token,
            "POST",
            f"/api/services/{DOMAIN}/suppress",
            {"rule_id": entry_id, "duration_minutes": 10},
        )
        saw_on = False
        deadline = time.time() + 20
        while time.time() < deadline:
            if sf.get_state(token, cancelled_eid).get("state") == "on":
                saw_on = True
                break
            time.sleep(1)
        sf.check("recently_enforced_cancelled pulses ON after a forced drop", saw_on)
        # (5) the cancelled device-trigger automation fired → SINK on.
        sf.check(
            "cancelled device trigger fired live (SINK on)", sdt.sink_turned_on(token)
        )
        # unsuppress so the verify-off leg below reloads from a clean rule state.
        sf.api(
            token, "POST", f"/api/services/{DOMAIN}/unsuppress", {"rule_id": entry_id}
        )

        # --- HARD (3): verify ON→OFF → cancelled sensor registry row DISABLED ---
        svs.set_verify_via_options(token, entry_id, verify=False)
        time.sleep(6)  # options edit reloads the entry; platforms re-setup
        gone = False
        deadline = time.time() + 20
        while time.time() < deadline:
            st = sf.get_state(token, cancelled_eid)
            if not st or st.get("state") in (None, "unavailable"):
                gone = True
                break
            time.sleep(1)
        sf.check(
            "recently_enforced_cancelled disabled (state gone) on verify-off", gone
        )
        still = sf.get_state(token, f"binary_sensor.{slug}_recently_enforced")
        sf.check("recently_enforced still present after verify-off", bool(still))
    finally:
        for aid in autos:
            sdt.delete_automation(token, aid)
        sf.cleanup(token)
        # Undo the virtual force_state on TARGET so no light is left reading a
        # forced value (force_state never commanded the real device; this just
        # drops the injected state back to where we found it).
        if target_pre not in (None, "unavailable", "unknown"):
            sv.force_state(token, TARGET, target_pre)
        # Restore master to its pre-run value (on pass, fail, or exception).
        # Only force OFF if it was not already ON before this run — never strand
        # a batch that needs master ON.
        set_master(token, master_pre == "on")

    if sf.FAILS:
        print(f"\nFAILED: {sf.FAILS}")
        return 1
    print("\nALL GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
