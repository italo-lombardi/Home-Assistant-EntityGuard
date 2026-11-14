"""Live smoke for verify-VERDICT sensor MUTUAL EXCLUSION — run INSIDE serene_booth.

The three verify-verdict sensors on a rule —
  binary_sensor.<rule>_recently_enforced_verified
  binary_sensor.<rule>_recently_enforced_unreachable
  binary_sensor.<rule>_recently_enforced_cancelled
— are mutually exclusive: a verify cycle resolves to exactly ONE verdict, so
arming one must force the other two OFF (even inside their shared 30s window).
The base binary_sensor.<rule>_recently_enforced (command-sent axis) is NOT in
the group and is deliberately left alone.

Twin conventions of smoke_verified_signal / smoke_cancelled_signal: compliant
light.living_room_under_cabinet_lights (sv.TARGET) for the real VERIFIED pulse,
the silent-ignore fixture (sv.SILENT_TARGET) for the deterministic verdict
switch, master flipped ON up front and restored in finally, force_state injects
a virtual state and is undone in finally, cleanup() drops only this run's rules.

Assertions, all HARD (deterministic):

  1. verify-ON rule on the compliant light → force a genuine success → the
     VERIFIED sensor pulses ON (compliant device confirms the re-read →
     _fire_verified).
  2. with VERIFIED ON, the other two verdict sensors (unreachable, cancelled)
     read OFF in the SAME window — the baseline exclusion invariant.
  3. on a SEPARATE silent-ignore rule, force a verdict SWITCH between two
     verdicts it can deterministically produce: exhaust → UNREACHABLE arms ON,
     then suppress a fresh cycle mid-window → CANCELLED arms ON and must clear
     the UNREACHABLE sibling (mutual exclusion). A silent target can never
     verify, so the VERIFIED-pulse legs stay on the compliant light.

Run inside the container:
  docker cp scripts/smoke_verdict_mutual_exclusion.py \
    serene_booth:/workspaces/home-assistant-core/config/smoke_verdict_mutual_exclusion.py
  docker exec serene_booth /home/vscode/.local/ha-venv/bin/python \
    /workspaces/home-assistant-core/config/smoke_verdict_mutual_exclusion.py
Exit 0 = all assertions green, 1 = a failure.
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "/workspaces/home-assistant-core/config")
import smoke_follow as sf  # shared helpers, resolved inside the container
import smoke_verify_enforce as sv  # reuse create_verify_rule + force_state + status

TARGET = sv.TARGET  # compliant light — for the real VERIFIED pulse (legs 1/2)
SILENT = sv.SILENT_TARGET  # silent-ignore fixture — for the verdict switch (leg 3)
DOMAIN = "entity_guard"
MASTER = "switch.entity_guard_hub_master_enabled"


def set_master(token: str, on: bool) -> None:
    svc = "turn_on" if on else "turn_off"
    sf.api(token, "POST", f"/api/services/switch/{svc}", {"entity_id": MASTER})


def get_master(token: str) -> str | None:
    return (sf.get_state(token, MASTER) or {}).get("state")


def _wait_state(token: str, eid: str, want: str, timeout: int = 25) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sf.get_state(token, eid).get("state") == want:
            return True
        time.sleep(1)
    return False


def main() -> int:
    token = sf.mint_token()
    sf.cleanup(token)

    name = "ea_smoke_verdict_mutex"
    slug = name
    verified_eid = f"binary_sensor.{slug}_recently_enforced_verified"
    unreachable_eid = f"binary_sensor.{slug}_recently_enforced_unreachable"
    cancelled_eid = f"binary_sensor.{slug}_recently_enforced_cancelled"

    target_pre = (sf.get_state(token, TARGET) or {}).get("state")
    master_pre = get_master(token)
    set_master(token, True)
    time.sleep(2)

    entry_id = sv.create_verify_rule(token, name, verify=True)
    sf.check("verify-enabled rule created", bool(entry_id))
    try:
        sv.status_sensor_for(token, slug)  # wait past startup grace
        armed = sf.wait_for_armed(token, "fixed_state", [TARGET])
        sf.check("rule left startup grace (armed)", armed)
        time.sleep(2)

        # --- HARD (1): force a genuine success → VERIFIED pulses ON -------------
        # Paint TARGET to the non-desired "off": trips the (off→on) rule WITHOUT
        # us driving desired. The engine enforces "on"; the verify re-read finds
        # "on" → confirm → _fire_verified → VERIFIED arms ON for 30s.
        sv.force_state(token, TARGET, "off")
        verified_on = _wait_state(token, verified_eid, "on")
        sf.check("verified sensor pulses ON after a confirmed enforce", verified_on)

        # --- HARD (2): while VERIFIED is ON, the other verdicts read OFF --------
        # The baseline exclusion invariant: no sibling verdict armed, so only one
        # verdict sensor is ever on. (Guards the regression where verdicts were
        # fully independent and two could read ON at once.)
        if verified_on:
            sf.check(
                "unreachable sensor OFF while verified ON",
                sf.get_state(token, unreachable_eid).get("state") == "off",
                f"unreachable={sf.get_state(token, unreachable_eid).get('state')}",
            )
            sf.check(
                "cancelled sensor OFF while verified ON",
                sf.get_state(token, cancelled_eid).get("state") == "off",
                f"cancelled={sf.get_state(token, cancelled_eid).get('state')}",
            )

        # --- HARD (3): force a verdict SWITCH on a silent rule → exclusive ------
        # Legs 1/2 proved VERIFIED excludes its siblings on a compliant target.
        # Leg 3 proves the switch is live between two verdicts a SILENT-IGNORE
        # rule can deterministically produce: first UNREACHABLE (verify exhausts
        # because the fixture never reaches desired), then CANCELLED (suppress a
        # fresh cycle mid-window). Arming CANCELLED must clear the UNREACHABLE
        # sibling in the same window. Separate rule so legs 1/2 keep a real
        # VERIFIED pulse (a silent target can never verify).
        s_name = "ea_smoke_verdict_mutex_silent"
        s_slug = s_name
        s_verified = f"binary_sensor.{s_slug}_recently_enforced_verified"
        s_unreachable = f"binary_sensor.{s_slug}_recently_enforced_unreachable"
        s_cancelled = f"binary_sensor.{s_slug}_recently_enforced_cancelled"
        s_entry = sv.create_verify_rule_on(
            token, s_name, [SILENT], ["off"], "on", verify=True
        )
        sf.check("silent verdict rule created", bool(s_entry))
        sv.status_sensor_for(token, s_slug)
        sf.check(
            "silent verdict rule armed",
            sf.wait_for_armed(token, "fixed_state", [SILENT]),
        )

        # (3a) exhaust → UNREACHABLE arms ON.
        sv.force_state(token, SILENT, "off")
        unreachable_on = _wait_state(token, s_unreachable, "on", timeout=35)
        sf.check(
            "silent rule: unreachable sensor armed ON after exhaustion", unreachable_on
        )

        # (3b) force CANCELLED on a fresh cycle → it must clear the UNREACHABLE
        # sibling (mutual exclusion). The fixture holds verify pending the full
        # window so suppress deterministically lands on the dropped path.
        sv.force_state(token, SILENT, "off")
        sf.api(
            token,
            "POST",
            f"/api/services/{DOMAIN}/suppress",
            {"rule_id": s_entry, "duration_minutes": 10},
        )
        cancelled_on = _wait_state(token, s_cancelled, "on", timeout=20)
        sf.check(
            "silent rule: cancelled sensor armed ON after a forced drop", cancelled_on
        )
        sf.check(
            "unreachable forced OFF when cancelled armed (mutual exclusion)",
            sf.get_state(token, s_unreachable).get("state") == "off",
            f"unreachable={sf.get_state(token, s_unreachable).get('state')}",
        )
        sf.check(
            "verified still OFF on silent rule (never armed)",
            sf.get_state(token, s_verified).get("state") == "off",
        )
        sf.api(
            token, "POST", f"/api/services/{DOMAIN}/unsuppress", {"rule_id": s_entry}
        )
        sv.force_state(token, SILENT, "off")  # reset fixture for later smokes
    finally:
        sf.cleanup(token)
        if target_pre not in (None, "unavailable", "unknown"):
            sv.force_state(token, TARGET, target_pre)
        set_master(token, master_pre == "on")

    if sf.FAILS:
        print(f"\nFAILED: {sf.FAILS}")
        return 1
    print("\nALL GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
