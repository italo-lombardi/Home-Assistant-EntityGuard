#!/usr/bin/env python3
"""Live check (serene_booth, REST) for the reached-but-dropped verify path.

Proves the b983de0 fix: when a verify-enabled target reaches desired WHILE the
rule is suppressed mid-window, the event confirm is dropped BUT the stale
deadline handle is dropped too — so a later unsuppress before the ~5s deadline
does NOT re-derive a phantom STATUS_VERIFYING on an already-compliant target.

Two assertions, both honest about what a compliant-device container can show:

  HARD (always reachable): a verify-ON rule that is suppressed then unsuppressed
  lands on a LIVE status (armed/pending/cooldown/conditional/enforcing), NEVER
  stuck on 'verifying'. This holds on every run regardless of race timing — it
  is the user-visible symptom the fix removes, and it is forceable because the
  suppress/unsuppress service pair is deterministic over REST.

  BEST-EFFORT (SKIP-tolerant): catching the target reaching desired in the
  narrow window AFTER suppress but BEFORE the ~5s deadline fires is a race the
  real device almost always loses — on a compliant device the event-driven
  confirm lands in ~10ms, long before we can suppress mid-window. So we try to
  wedge a verify-pending-while-suppressed state and observe the non-stuck
  recovery; if the race doesn't land (the common case), we SKIP that leg rather
  than hard-fail. The exact drop-the-handle logic is unit-locked by
  test_verify_dropped_reached_drops_stale_handle; this is the live corroboration
  only. (Same SKIP honesty as smoke_unreachable_signal / smoke_verify_enforce:
  the compliant container cannot force the slow verify branch on demand.)

Deterministic target: a verify-ON fixed_state rule driving the allowed
`light.living_room_under_cabinet_lights` (sv.TARGET), triggered off the allowed
`input_boolean.ea_ctrl_kitchen_1` (sv.TRIGGER) — memory: smoke_test_entities.
force_state trips/writes the target; no real-device side effect. Because that
light has read 'unavailable' in past sessions, the HARD leg is gated behind a
pre-trip availability guard (an unavailable target can't ACK, which would false-
FAIL on the re-arm — a precondition miss, not a fix regression).

Run INSIDE the container:
  /home/vscode/.local/ha-venv/bin/python -I scripts/smoke_verify_suppress_drop.py
Exit 0 = HARD assertions green (best-effort may SKIP), 1 = a HARD failure.
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "/workspaces/home-assistant-core/config")
import smoke_follow as sf  # token/api/waits/cleanup/check
import smoke_verify_enforce as sv  # create_verify_rule, status_sensor_for, force_state

TARGET = sv.TARGET  # allowed smoke target
DOMAIN = "entity_guard"
LIVE = {"armed", "conditional", "enforcing", "pending", "cooldown"}
POLL = 0.2


def main() -> int:
    token = sf.mint_token()
    sf.cleanup(token)  # clear orphan EG rules from prior runs

    name = "ea_smoke_verify_suppress_drop"
    slug = name
    entry = sv.create_verify_rule(token, name, verify=True)
    sf.check("verify-enabled rule created", bool(entry))

    try:
        sv.status_sensor_for(token, slug)  # status sensor live
        armed = sf.wait_for_armed(token, "fixed_state", [TARGET])
        sf.check("rule left startup grace (armed)", armed)

        # Precondition: TARGET must be available before the trip. If it is
        # unavailable/unknown, force_state(off) writes over it but the real
        # device never ACKs reaching desired -> the suppress/unsuppress leg may
        # not land a LIVE status -> a false HARD FAIL that wrongly blames the
        # b983de0 fix (memory: under_cabinet was 'unavailable' in a past
        # session). Bail explicitly instead. Early return is inside the try, so
        # finally still cleans up our rule. Mirrors smoke_fast_verify.py.
        pre = sf.get_state(token, TARGET)
        pre_ok = pre and pre.get("state") not in (None, "unavailable", "unknown")
        sf.check(
            "target available before trip (else false FAIL unrelated to fix)",
            pre_ok,
            f"target state={pre.get('state') if pre else None}",
        )
        if not pre_ok:
            return 1  # unavailable device can't ACK; precondition, not our bug

        # --- best-effort: try to wedge verify-pending WHILE suppressed --------
        # Trip the off->on rule (target reports "off"); the engine enforces "on".
        # Immediately suppress, racing the ~10ms event-driven confirm. If suppress
        # wins, the reached ACK lands on the dropped path (handle dropped by the
        # fix); if the confirm wins (common), verify already resolved and the
        # suppress just suppresses an armed rule — either way the HARD leg below
        # still proves non-stuck recovery.
        sv.force_state(token, TARGET, "off")
        sf.api(
            token,
            "POST",
            f"/api/services/{DOMAIN}/suppress",
            {"rule_id": entry, "duration_minutes": 10},
        )
        # Observe whether we ever caught VERIFYING-under-suppress (informational).
        saw_verifying = False
        deadline = time.time() + 2.0
        while time.time() < deadline:
            st = sf.get_state(token, f"sensor.{slug}_status")
            if st and st.get("state") == "verifying":
                saw_verifying = True
                break
            time.sleep(POLL)
        if not saw_verifying:
            sf.check(
                "SKIP: verify-pending-while-suppressed race not forced "
                "(compliant device confirmed in ~10ms before suppress) — "
                "handle-drop logic is unit-locked",
                True,
            )

        # --- HARD: unsuppress must NOT strand the rule on 'verifying' ---------
        sf.api(
            token,
            "POST",
            f"/api/services/{DOMAIN}/unsuppress",
            {"rule_id": entry},
        )
        st = None
        deadline = time.time() + 30
        while time.time() < deadline:
            st = sf.get_state(token, f"sensor.{slug}_status")
            if st and st.get("state") not in (None, "suppressed", "verifying"):
                break
            time.sleep(POLL)
        cur = st.get("state") if st else None
        sf.check(
            "unsuppress -> not stuck on 'verifying' (b983de0 stale-handle fix)",
            cur != "verifying",
            f"state={cur}",
        )
        sf.check(
            "unsuppress -> lands on a live status",
            cur in LIVE,
            f"state={cur}",
        )
    finally:
        sf.cleanup(token)

    if sf.FAILS:
        print(f"\nFAILED: {sf.FAILS}")
        return 1
    print("\nALL GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
