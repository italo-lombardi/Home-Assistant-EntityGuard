#!/usr/bin/env python3
"""Smoke test: rapid double manual-force is latest-wins (restart semantics).

The per-rule test_enforce button has restart semantics: a second press
supersedes the first via a cooperative force_generation counter (latest-wins),
checked at the loop top — NOT asyncio.Task.cancel (which re-opened the PR #70
cancel race). An entity already inside its enforce finishes that idempotent
command; the loop then bails on the next iteration when it sees a newer
generation. Only the winning pass re-arms the suppression timer.

WHAT IS LIVE-FORCEABLE vs UNIT-LOCKED (honest scope):
  The generation counter is internal; the forced enforce is idempotent, so the
  target's END state is identical whether one or two passes ran. What a REST
  client CAN observe — and what this asserts — is the SAFETY envelope:

    1. A multi-target rule, pressed twice in quick succession, drives every
       target to the desired state (the winning pass completes a full fan-out).
    2. The rule lands on a clean LIVE status (armed/cooldown/pending/…), never
       "error" — two interleaved forced passes do not corrupt the state machine
       or raise. This is the regression the cooperative counter protects:
       pre-fix, two passes double-commanded and could flicker/raise.

  The mechanics that only the WINNER re-runs (superseded pass bails at the
  loop-top generation check, in-flight enforce completes idempotent) are
  deterministic and locked by the supersede unit tests in
  tests/test_rule_engine.py — a fake _enforce bumps the generation mid-pass and
  asserts the later targets are skipped. Live, those two outcomes are
  observationally identical to a single clean pass, so this smoke proves the
  envelope, not the counter.

Two ALLOWED light targets (memory: smoke_test_entities) give a real multi-entity
fan-out so a supersede has somewhere to interleave. Zero forbidden devices.

Reuses smoke_follow.py + smoke_verify_enforce.py helpers.
Requires the restart-semantics fix deployed to serene_booth.
Usage: docker exec serene_booth python3 \
  /workspaces/home-assistant-core/config/smoke_force_restart.py
Exit 0 = all hard assertions green, 1 = a failure.
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "/workspaces/home-assistant-core/config")
import smoke_follow as sf  # shared helpers, resolved inside the container
import smoke_verify_enforce as sve  # status_sensor_for / force_state

# Two allowed light targets → a real fan-out for a supersede to interleave in.
TARGETS = [
    "light.living_room_under_cabinet_lights",
    "light.living_room_diffuser",
]
DESIRED = "on"
LIVE = {"armed", "conditional", "enforcing", "pending", "cooldown", "verifying"}


def press(token: str, entity_id: str) -> None:
    sf.api(token, "POST", "/api/services/button/press", {"entity_id": entity_id})


def main() -> int:
    token = sf.mint_token()
    sf.cleanup(token)  # clear orphan EG rules from prior runs

    ok = True
    name = "ea_smoke_force_restart"
    slug = name

    entry = sf.create_fixed_state_rule(
        token,
        name,
        TARGETS,
        target_state=DESIRED,
        trigger_states=["off"],  # exclude target_state (target_in_triggers)
    )
    sf.check("multi-target force rule created", bool(entry))
    ok = ok and bool(entry)

    status = sve.status_sensor_for(token, slug)
    armed = sf.wait_for_armed(token, "fixed_state", TARGETS)
    sf.check("rule armed", armed)
    ok = ok and armed

    # Drive BOTH targets off so the forced pass has real work (turn each on).
    for t in TARGETS:
        sve.force_state(token, t, "off")
    time.sleep(1)

    # Two presses in quick succession → 2nd supersedes 1st (latest-wins).
    button = f"button.{slug}_test_enforce"
    press(token, button)
    press(token, button)

    # The winning pass must complete a full fan-out: every target reaches DESIRED.
    deadline = time.time() + 25
    reached = {t: False for t in TARGETS}
    while time.time() < deadline and not all(reached.values()):
        for t in TARGETS:
            if not reached[t] and sf.get_state(token, t).get("state") == DESIRED:
                reached[t] = True
        if not all(reached.values()):
            time.sleep(1)
    for t in TARGETS:
        sf.check(f"target {t} driven to {DESIRED} after double-force", reached[t])
        ok = ok and reached[t]

    # The state machine must stay clean — two interleaved forced passes never
    # corrupt it into "error". Settle, then read the status once.
    time.sleep(3)
    sstate = sf.get_state(token, status).get("state")
    clean = sstate in LIVE
    sf.check(
        "rule lands on a live status, not error, after double-force",
        clean,
        f"status={sstate}",
    )
    ok = ok and clean

    sf.cleanup(token)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
