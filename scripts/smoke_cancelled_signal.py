"""Live smoke for the verify-CANCELLED signal (v0.4.2) — run INSIDE serene_booth.

Proves the LOCKED behaviors of the new per-rule
`binary_sensor.<rule>_recently_enforced_cancelled` sensor, its
`entity_guard_enforced_cancelled` event, and its device trigger over the running
container — the NEUTRAL third twin of smoke_verified_signal (success) and
smoke_unreachable_signal (device failure). `cancelled` means a verify cycle was
ARMED then ABANDONED mid-window with no verdict (external condition changed:
suppressed / flags-unmet / rule disabled / master off) — not a device fault.

Five assertions, split HARD (deterministic over REST) vs BEST-EFFORT (SKIP on a
compliant device, exactly like the verified/unreachable/suppress-drop twins):

  HARD
  1. verify-ON → all three verify twins EXIST alongside recently_enforced
     (verified, unreachable, cancelled). Regression guard for the lifecycle
     wiring — the cancelled sensor is verify-gated like its siblings.
  2. the cancelled DEVICE TRIGGER is schema-valid and ATTACHES: an automation
     built with `platform: device, type: cancelled` on the rule device installs
     and reports state 'on' (async_attach_trigger accepted the type → generic
     TRIGGER_TYPE_EVENT lookup). The FIRE leg is best-effort below.
  3. verify ON→OFF via the options flow → the cancelled sensor's registry row is
     DISABLED (state leaves the state machine). recently_enforced survives.

  BEST-EFFORT (SKIP-tolerant)
  4. a real forced drop → the cancelled sensor pulses ON. We arm a verify cycle
     (trip the off→on rule so the engine enforces) and immediately call
     `entity_guard.suppress`, racing the ~10ms event-driven confirm. If suppress
     wins the race it lands on the reached-but-dropped path → _fire_cancelled
     (reason "suppressed") → sensor arms ON for 30s. On a compliant device the
     confirm almost always wins (verified, not cancelled), so a miss is SKIPPED,
     not failed — the drop→EVENT_CANCELLED→arm chain is unit-locked by
     tests/test_rule_engine.py (test_verify_fire_cancelled_when_suppressed et al).
  5. if (4) landed, the cancelled device-trigger automation flipped SINK on
     (EVENT_CANCELLED reached async_attach_trigger live). Also SKIP-tolerant,
     gated on (4).

Target is the allowed light.living_room_under_cabinet_lights (sv.TARGET);
trigger the allowed input_boolean (sv.TRIGGER); action sink SINK — memory:
smoke_test_entities. Raw /api/states injection trips the trigger without
commanding the physical device; cleanup drops the rule + automations.

Run inside the container:
  docker cp scripts/smoke_cancelled_signal.py \
    serene_booth:/workspaces/home-assistant-core/config/smoke_cancelled_signal.py
  docker exec serene_booth /home/vscode/.local/ha-venv/bin/python \
    /workspaces/home-assistant-core/config/smoke_cancelled_signal.py
Exit 0 = all HARD assertions green (best-effort may SKIP), 1 = a HARD failure.
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "/workspaces/home-assistant-core/config")
import smoke_device_triggers as sdt  # reuse install/delete automation + SINK + device-id
import smoke_follow as sf  # shared helpers, resolved inside the container
import smoke_verified_signal as svs  # reuse set_verify_via_options
import smoke_verify_enforce as sv  # reuse create_verify_rule + force_state + status

TARGET = sv.TARGET
DOMAIN = "entity_guard"
MASTER = "switch.entity_guard_hub_master_enabled"


def set_master(token: str, on: bool) -> None:
    """Flip the hub master-enable switch. The engine only arms rules while master
    is ON (else every rule reads 'master_disabled' and wait_for_armed times out).
    Smoke turns it ON up front and OFF in `finally` so the container is left as
    found regardless of pass/fail."""
    svc = "turn_on" if on else "turn_off"
    sf.api(token, "POST", f"/api/services/switch/{svc}", {"entity_id": MASTER})


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
    # the state machine reading the stale value) and the master switch (left OFF).
    target_pre = (sf.get_state(token, TARGET) or {}).get("state")
    set_master(token, True)
    time.sleep(2)

    entry_id = sv.create_verify_rule(token, name, verify=True)
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

        # --- BEST-EFFORT (4)+(5): force a drop, catch the pulse + trigger fire --
        # Trip the off→on rule (target reports "off") so the engine enforces "on",
        # then IMMEDIATELY suppress — racing the ~10ms event-driven confirm. A
        # suppress win lands the reached ACK on the reached-but-dropped path →
        # _fire_cancelled(reason "suppressed"). A confirm win (the common case on a
        # compliant device) resolves as verified, not cancelled → SKIP.
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
        if saw_on:
            sf.check("recently_enforced_cancelled pulses ON after a forced drop", True)
            # (5) the cancelled device-trigger automation fired → SINK on.
            if sdt.sink_turned_on(token):
                sf.check("cancelled device trigger fired live (SINK on)", True)
            else:
                print(
                    "SKIP: cancelled sensor pulsed but device-trigger SINK edge not "
                    "caught over REST (30s pulse-window race; attach locked by unit "
                    "test test_attach_trigger_fires_on_new_event_types[cancelled])"
                )
        else:
            print(
                "SKIP: verify-drop-while-suppressed race not forced (compliant device "
                "confirmed in ~10ms before suppress → verified, not cancelled) — "
                "drop → EVENT_CANCELLED → sensor-arm chain is unit-locked "
                "(test_verify_fire_cancelled_when_suppressed)"
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
        # Always leave master OFF (as found) — on pass, fail, or exception.
        set_master(token, False)

    if sf.FAILS:
        print(f"\nFAILED: {sf.FAILS}")
        return 1
    print("\nALL GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
