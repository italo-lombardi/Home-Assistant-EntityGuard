#!/usr/bin/env python3
"""Smoke test: event-driven fast verify-confirm (feat/fast-verify).

Proves the verify-after-enforce HAPPY PATH is now EVENT-DRIVEN, not a blind
timer. Before this change, verify re-read its target ONCE after a blind
`VERIFY_DELAY_SECONDS` (5s) wait; the confirm — and thus EVENT_VERIFIED /
`binary_sensor.<rule>_recently_enforced_verified` arming — could not land in
under ~5s. After the change the engine confirms the instant the target's
state-change ACK reaches desired (the existing reactive `_handle_state_event`
@callback fires inline on state_changed), so the confirm lands at device
latency + REST/poll overhead — comfortably sub-second engine-side, a few
hundred ms observable. The 5s is now a DEADLINE only.

BEHAVIORAL PROOF (the one HARD assertion):
  Stamp t0, trip the rule's violation ONCE, then time the verified sensor's
  first ON edge. Assert (t1 - t0) < FAST_THRESHOLD_SECONDS (3s). The old blind
  path floors at ~5s (VERIFY_DELAY_SECONDS); the event-driven path lands well
  under 3s. 3s is the discriminator: loose enough to survive serene_booth tick
  + REST/poll overhead, tight enough that a regression to the blind timer
  (≥5s) fails it. The measured delta is also logged for visibility.

SCENARIO-ISOLATION HONESTY GUARD (do not remove):
  This times the FAST event-driven confirm, and is valid ONLY because the
  scenario drives the target compliant on the FIRST enforce — so no corrective
  re-enforce is triggered and the deadline-timer confirm branch is never
  reached. The verified sensor is NOT intrinsically a fast-path-only proxy:
  `_fire_verified` also arms it on the SLOW deadline branch after a corrective
  finally lands. The SCENARIO — a single violation trip, then leave the real
  device alone to obey — is what isolates the fast path. If a future edit
  re-injects the off state after the trip (re-shoving the target), it recreates
  the unreachable/corrective scenario and this timing proof is VOID.

Reuses smoke_follow.py (token/api/waits/cleanup) and smoke_verify_enforce.py
(create_verify_rule, status_sensor_for, force_state, TARGET). Does not modify
either. Requires the integration branch with event-driven verify deployed to
serene_booth. Run inside the container:
  docker cp scripts/smoke_fast_verify.py \
    serene_booth:/workspaces/home-assistant-core/config/smoke_fast_verify.py
  docker exec serene_booth python3 \
    /workspaces/home-assistant-core/config/smoke_fast_verify.py
Exit 0 = all hard assertions green, 1 = a failure.
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "/workspaces/home-assistant-core/config")
import smoke_follow as sf  # shared helpers, resolved inside the container
import smoke_verify_enforce as sv  # reuse create_verify_rule + status_sensor_for

TARGET = sv.TARGET  # allowed smoke target (memory: smoke_test_entities)

# Old blind path floors at VERIFY_DELAY_SECONDS (5s); event-driven lands
# sub-second engine-side. 3s survives REST/poll+tick overhead yet fails a
# regression to the blind timer. Locked at 3s by design — do not tighten.
FAST_THRESHOLD_SECONDS = 3.0
POLL_INTERVAL = 0.2  # fine poll so the ON edge isn't under-resolved


def main() -> int:
    token = sf.mint_token()
    sf.cleanup(token)  # clear any orphan EG rules from prior runs

    name = "ea_smoke_fast_verify"
    slug = name
    verified_eid = f"binary_sensor.{slug}_recently_enforced_verified"

    try:
        # verify-ON rule; target starts compliant-on-first-enforce scenario.
        entry = sv.create_verify_rule(token, name, verify=True)
        sf.check("verify-enabled rule created", bool(entry))

        sv.status_sensor_for(token, slug)  # status sensor live
        # Engine ignores target events during startup grace (~60s): triggering
        # before it arms sees no enforce -> no verify -> no pulse (false FAIL).
        # Stamp t0 only AFTER grace clears, else grace latency pollutes the delta.
        armed = sf.wait_for_armed(token, "fixed_state", [TARGET])
        sf.check("rule left startup grace (armed)", armed)

        # Precondition: TARGET must be available before the trip. If it is
        # unavailable/unknown, force_state(off) writes over it but the real
        # device never produces a state-change ACK to desired, so the
        # event-driven confirm never fires -> no verified pulse -> a <3s false
        # FAIL that wrongly blames the engine change (memory: under_cabinet was
        # 'unavailable' in a past session). Bail explicitly instead. The early
        # return is inside the try, so finally still cleans up our rule.
        pre = sf.get_state(token, TARGET)
        pre_ok = pre and pre.get("state") not in (None, "unavailable", "unknown")
        sf.check(
            "target available before trip (else false FAIL unrelated to verify)",
            pre_ok,
            f"target state={pre.get('state') if pre else None}",
        )
        if not pre_ok:
            return 1  # unavailable device can't ACK; precondition, not our bug

        # --- the fast-verify timing proof ----------------------------------
        # Trip the off->on rule ONCE via a raw /api/states inject (fakes the
        # target reporting "off"); the ENGINE then enforces "on" via the real
        # service and the real device obeys -> ACK -> event-driven confirm ->
        # _fire_verified arms the verified sensor fast. NEVER re-inject "off"
        # after this: a re-shove recreates the corrective/deadline path and
        # voids the timing proof (see docstring honesty guard).
        t0 = time.time()
        sv.force_state(token, TARGET, "off")  # single violation trip

        t1 = None
        deadline = t0 + 25  # generous ceiling so a blind-timer regression is
        # observed (~5s) and fails the < 3s assert, rather than timing out silent
        while time.time() < deadline:
            st = sf.get_state(token, verified_eid)
            if st.get("state") == "on":
                t1 = time.time()
                break
            time.sleep(POLL_INTERVAL)

        if t1 is None:
            sf.check(
                "verified sensor pulsed ON after a confirmed enforce",
                False,
                "no ON edge within 25s (sensor never armed — engine/scenario issue)",
            )
        else:
            delta = t1 - t0
            print(f"  measured enforce->verified delta: {delta:.3f}s")
            sf.check(
                f"fast event-driven confirm (<{FAST_THRESHOLD_SECONDS}s, not blind 5s timer)",
                delta < FAST_THRESHOLD_SECONDS,
                f"delta={delta:.3f}s",
            )
    finally:
        sf.cleanup(token)  # clean our rules even on early exit (memory rule)

    if sf.FAILS:
        print(f"\nFAILED: {sf.FAILS}")
        return 1
    print("\nALL GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
