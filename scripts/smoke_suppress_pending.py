#!/usr/bin/env python3
"""Entity Guard SUPPRESS-DURING-PENDING smoke test — run INSIDE serene_booth.

Regression guard for the bug where pressing Suppress while a rule is in its
delay_seconds pending-enforcement countdown did NOT stop the scheduled
enforcement (the rule fired anyway and the card kept the countdown).

Scenario (fixed_state rule, delay_seconds=8, target = a real light):
  1. Create the rule; wait past the 60s startup grace so it is armed.
  2. Violate the target (drive it to a trigger state) -> rule enters PENDING,
     a delay timer is scheduled, pending_enforcement_due is populated.
  3. While still inside the delay window, call entity_guard.suppress.
  4. Assert: (a) the target is NOT enforced for the full original delay + margin
     (the scheduled fire was cancelled), (b) pending_enforcement_due is empty
     (card countdown gone), (c) status == suppressed.
  5. Cleanup: unsuppress + delete the rule (finally, even on crash).

Reuses smoke_follow.py helpers (token mint, REST api, create flow).
Exit 0 = all green, 1 = a failure.
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "/workspaces/home-assistant-core/config")
import smoke_follow as sf  # shared helpers, resolved inside the container

# A real actuatable light the engine can drive; same allowed smoke target set.
TARGET = "light.living_room_under_cabinet_lights"
DELAY = 8  # seconds — long enough to suppress mid-countdown
CREATED: list[str] = []
MASTER_WAS: str | None = None  # EG master switch state before the run (to restore)


def create_fixed_state_delay_rule(token, name, target, delay):
    """Create a fixed_state rule with delay_seconds=delay. Returns (entry_id,
    rule_id). rule_id (unique_id) is what entity_guard.suppress wants."""
    flow = sf.api(
        token, "POST", "/api/config/config_entries/flow", {"handler": "entity_guard"}
    )
    fid = flow["flow_id"]
    result = sf.api(
        token,
        "POST",
        f"/api/config/config_entries/flow/{fid}",
        {"rule_name": name, "target_entities": [target], "mode": "fixed_state"},
    )
    while result.get("type") == "form":
        sf.guard_form(result)
        if result.get("step_id") == "state":
            payload = {
                "trigger_states": ["on"],
                "target_state": "off",
                "delay_seconds": delay,
            }
        else:
            payload = {}
        result = sf.api(
            token, "POST", f"/api/config/config_entries/flow/{fid}", payload
        )
    if result.get("type") != "create_entry":
        raise RuntimeError(f"create flow did not finish: {result}")
    entry_id = result["result"]["entry_id"]
    CREATED.append(entry_id)
    return entry_id


def main() -> int:
    token = sf.mint_token()

    # The EG master switch gates ALL rules — if it is off, every rule reports
    # master_disabled and never arms, so the smoke could never see a countdown.
    # Enable it for the run; MASTER_WAS records the original so we restore it.
    master = "switch.entity_guard_hub_master_enabled"
    global MASTER_WAS
    MASTER_WAS = sf.get_state(token, master).get("state")
    if MASTER_WAS != "on":
        sf.api(token, "POST", "/api/services/switch/turn_on", {"entity_id": master})
        time.sleep(2)

    # Known start: target OFF (compliant with target_state=off).
    sf.api(token, "POST", "/api/services/light/turn_off", {"entity_id": TARGET})

    print("Creating fixed_state rule (delay=8s)…")
    # Per-RUN unique name (not just per-rule): append a run token so the slug is
    # unique across re-runs. If a prior run's finally-cleanup DELETE ever failed,
    # its orphan has a DIFFERENT token → cannot collide with / shadow this run's
    # status sensor. Closes the same-name-orphan binding nuance entirely.
    run_token = str(int(time.time()))
    rule_name = f"EG Suppress Pending Smoke {run_token}"
    name_slug = f"eg_suppress_pending_smoke_{run_token}"  # HA slugify of rule_name
    entry_id = create_fixed_state_delay_rule(token, rule_name, TARGET, DELAY)
    print(f"  created entry {entry_id}")

    # entity_guard.suppress resolves by entry_id, unique_id, OR name
    # (_resolve_engine fast-path is entry_id), so pass entry_id directly — the
    # REST config-entries list does not expose unique_id anyway.
    rule_id = entry_id

    # Wait past startup grace (60s) until armed — events are ignored during grace.
    # Bind the status sensor by OUR per-run name-slug AND mode AND target. The
    # run_token makes the slug unique per run, so no orphan (same-name or not) can
    # bind. Also assert EXACTLY ONE match to catch any unexpected duplicate loudly.
    status_e = ""
    deadline = time.time() + 100
    live = {"armed", "conditional", "enforcing", "cooldown", "pending"}
    while time.time() < deadline and not status_e:
        matches = [
            s["entity_id"]
            for s in sf.api(token, "GET", "/api/states")
            if (
                s.get("entity_id", "").startswith(f"sensor.{name_slug}")
                and s["entity_id"].endswith("_status")
                and s.get("attributes", {}).get("mode") == "fixed_state"
                and (s.get("attributes", {}).get("target_entities") or []) == [TARGET]
                and s.get("state") in live
            )
        ]
        if len(matches) > 1:
            sf.check(
                "exactly one status sensor for this run",
                False,
                f"multiple matches: {matches}",
            )
            return 1
        if matches:
            status_e = matches[0]
        else:
            time.sleep(3)
    sf.check("rule armed past startup grace", bool(status_e), f"status={status_e}")
    if not status_e or not rule_id:
        return 1

    # Violate: drive target ON -> rule enters PENDING with an 8s delay timer.
    sf.api(token, "POST", "/api/services/light/turn_on", {"entity_id": TARGET})

    # PRE-SUPPRESS CHECKPOINT (load-bearing): prove there was something to cancel,
    # else the downstream asserts pass trivially on a rule that never armed —
    # a false green. Two hard gates:
    #   1. the violation actually took (target reads ON), and
    #   2. the rule is in PENDING with a populated due (countdown genuinely live).
    # Both must hold BEFORE we suppress; bail loudly otherwise.
    on_ok = False
    deadline = time.time() + 6
    while time.time() < deadline:
        if sf.get_state(token, TARGET).get("state") == "on":
            on_ok = True
            break
        time.sleep(0.5)
    sf.check(
        "pre-suppress: violation took (target ON)",
        on_ok,
        f"target={sf.get_state(token, TARGET).get('state')}",
    )

    pending_ok = False
    deadline = time.time() + 6
    while time.time() < deadline:
        a = sf.get_state(token, status_e).get("attributes", {})
        if sf.get_state(token, status_e).get("state") == "pending" and a.get(
            "pending_enforcement_due"
        ):
            pending_ok = True
            break
        time.sleep(0.5)
    sf.check(
        "pre-suppress: rule in PENDING with live countdown",
        pending_ok,
        f"status={sf.get_state(token, status_e).get('state')} "
        f"due={sf.get_state(token, status_e).get('attributes', {}).get('pending_enforcement_due')}",
    )
    # Hard gate: if there was no live countdown, the suppress asserts below are
    # meaningless — fail the run now rather than report a trivial green.
    if not (on_ok and pending_ok):
        print("SMOKE RESULT: FAILED — no live countdown to cancel (setup did not arm)")
        return 1

    # SUPPRESS mid-countdown.
    sf.api(
        token,
        "POST",
        "/api/services/entity_guard/suppress",
        {"rule_id": rule_id, "duration_minutes": 1},
    )

    # (a) target must NOT be enforced for the full original delay + margin.
    # Note: breaks on the first OFF reading — if some OTHER actor turned the light
    # off in this window it would read as a (false) BUG. Over-strict, not
    # false-green; acceptable for a smoke on a dedicated test target.
    held_off = True
    end = time.time() + DELAY + 4
    while time.time() < end:
        if sf.get_state(token, TARGET).get("state") == "off":
            held_off = False  # enforcement fired (clamped to off) — BUG
            break
        time.sleep(0.5)
    sf.check(
        "suppress cancels pending enforce: target NOT clamped",
        held_off,
        f"target={sf.get_state(token, TARGET).get('state')}",
    )

    # (b) pending_enforcement_due cleared (card countdown gone).
    due = (
        sf.get_state(token, status_e)
        .get("attributes", {})
        .get("pending_enforcement_due")
    )
    sf.check(
        "suppress clears pending_enforcement_due (countdown gone)",
        not due,
        f"due={due}",
    )

    # (c) status == suppressed.
    st = sf.get_state(token, status_e).get("state")
    sf.check("status == suppressed", st == "suppressed", f"status={st}")

    print()
    if sf.FAILS:
        print(f"SMOKE RESULT: {len(sf.FAILS)} FAILED — {', '.join(sf.FAILS)}")
        return 1
    print("SMOKE RESULT: ALL GREEN")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        tok = sf.mint_token()
        for eid in CREATED:
            try:
                sf.api(tok, "DELETE", f"/api/config/config_entries/entry/{eid}")
                print(f"  cleaned up entry {eid}")
            except Exception as e:
                print(f"  [WARN] cleanup failed for {eid}: {e}")
        # Restore the EG master switch if the run had turned it on.
        if MASTER_WAS is not None and MASTER_WAS != "on":
            try:
                sf.api(
                    tok,
                    "POST",
                    "/api/services/switch/turn_off",
                    {"entity_id": "switch.entity_guard_hub_master_enabled"},
                )
                print("  restored master switch to off")
            except Exception as e:
                print(f"  [WARN] master restore failed: {e}")
    sys.exit(rc)
