"""Live smoke for verify-VERDICT sensor MUTUAL EXCLUSION — run INSIDE serene_booth.

The three verify-verdict sensors on a rule —
  binary_sensor.<rule>_recently_enforced_verified
  binary_sensor.<rule>_recently_enforced_unreachable
  binary_sensor.<rule>_recently_enforced_cancelled
— are mutually exclusive: a verify cycle resolves to exactly ONE verdict, so
arming one must force the other two OFF (even inside their shared 30s window).
The base binary_sensor.<rule>_recently_enforced (command-sent axis) is NOT in
the group and is deliberately left alone.

Twin conventions of smoke_verified_signal / smoke_cancelled_signal: allowed
target light.living_room_under_cabinet_lights (sv.TARGET), master flipped ON up
front and restored in finally, force_state injects a virtual state over the
TARGET (no real device command) and is undone in finally, cleanup() drops only
this run's own rule.

Assertions, split HARD (deterministic over REST) vs BEST-EFFORT (SKIP on a
compliant device, like the verified/cancelled twins):

  HARD
  1. verify-ON rule → force a genuine success → the VERIFIED sensor pulses ON
     (deterministic: compliant device confirms the re-read → _fire_verified).
  2. with VERIFIED ON, the other two verdict sensors (unreachable, cancelled)
     read OFF in the SAME window — the baseline exclusion invariant (only one
     verdict is ever on at once).

  BEST-EFFORT (SKIP-tolerant)
  3. force a SECOND, DIFFERENT verdict inside the window and assert the switch is
     exclusive: trip the rule again and immediately `entity_guard.suppress`,
     racing the ~10ms event-driven confirm. If suppress wins, the reached ACK
     lands on the reached-but-dropped path → _fire_cancelled → CANCELLED arms ON
     AND VERIFIED must now read OFF (the arm cleared its sibling). On a compliant
     device the confirm usually wins (stays verified) → SKIP. The clear-sibling
     mechanism is unit-locked (test_verdict_sensors_mutually_exclusive).

Run inside the container:
  docker cp scripts/smoke_verdict_mutual_exclusion.py \
    serene_booth:/workspaces/home-assistant-core/config/smoke_verdict_mutual_exclusion.py
  docker exec serene_booth /home/vscode/.local/ha-venv/bin/python \
    /workspaces/home-assistant-core/config/smoke_verdict_mutual_exclusion.py
Exit 0 = all HARD assertions green (best-effort may SKIP), 1 = a HARD failure.
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "/workspaces/home-assistant-core/config")
import smoke_follow as sf  # shared helpers, resolved inside the container
import smoke_verify_enforce as sv  # reuse create_verify_rule + force_state + status

TARGET = sv.TARGET
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

        # --- BEST-EFFORT (3): force a SECOND verdict → exclusive switch ---------
        # Re-trip the rule and immediately suppress, racing the ~10ms confirm. A
        # suppress win lands _fire_cancelled → CANCELLED arms ON; the arm must have
        # cleared its sibling, so VERIFIED now reads OFF in the same window. A
        # confirm win (common on a compliant device) stays verified → SKIP.
        sv.force_state(token, TARGET, "off")
        sf.api(
            token,
            "POST",
            f"/api/services/{DOMAIN}/suppress",
            {"rule_id": entry_id, "duration_minutes": 10},
        )
        cancelled_on = _wait_state(token, cancelled_eid, "on", timeout=20)
        if cancelled_on:
            sf.check("cancelled sensor armed ON after a forced drop", True)
            # The whole point: arming cancelled cleared the verified sibling.
            sf.check(
                "verified sensor forced OFF when cancelled armed (mutual exclusion)",
                sf.get_state(token, verified_eid).get("state") == "off",
                f"verified={sf.get_state(token, verified_eid).get('state')}",
            )
            sf.check(
                "unreachable sensor still OFF (never armed)",
                sf.get_state(token, unreachable_eid).get("state") == "off",
            )
        else:
            print(
                "SKIP: second-verdict switch not forced live (compliant device "
                "confirmed in ~10ms before suppress → stayed verified, no cancelled "
                "to switch to) — the clear-sibling mechanism is unit-locked "
                "(test_verdict_sensors_mutually_exclusive)"
            )
        sf.api(
            token, "POST", f"/api/services/{DOMAIN}/unsuppress", {"rule_id": entry_id}
        )
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
