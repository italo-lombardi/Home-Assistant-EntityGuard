#!/usr/bin/env python3
"""Smoke test: card-visible status attrs stay live when a rule is inactive.

Covers the stale-attr dedupe fixes: when a rule is DISABLED or SUPPRESSED, the
engine still early-returns without changing the status string, so _set_status
deduplicates the broadcast. A watched SOURCE entity (dynamic_state mode) can
flip while inactive and move the card-visible `follow_desired_on` sensor attr —
the engine must force a broadcast on that dedupe so the status sensor re-writes
and the card sees the fresh value instead of a frozen one.

Two scenarios, each driving the real source input_boolean and asserting
follow_desired_on tracks it while the rule never enforces:

  A. SUPPRESSED + source flip — rule armed, then entity_guard.suppress, then
     flip source; status stays `suppressed`, follow_desired_on follows.
  B. DISABLED + source flip — toggle the rule's enabled switch off, then flip
     source; status stays `disabled`, follow_desired_on follows.

CONDITIONAL+source is covered by the engine unit tests (identical guard route);
not re-smoked here to avoid slow REST padding of unit coverage.

Reuses smoke_follow.py helpers (token mint, REST api, create flow, cleanup).
Usage: docker exec serene_booth python3 \
  /workspaces/home-assistant-core/config/smoke_stale_attr.py
Exit 0 = all green, 1 = a failure.
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "/workspaces/home-assistant-core/config")
import smoke_follow as sf  # shared helpers, resolved inside the container

SOURCE = sf.SOURCE  # input_boolean.ea_ctrl_kitchen_1 — a real, drivable state
TARGET = sf.TARGET  # light.living_room_under_cabinet_lights
MASTER = "switch.entity_guard_hub_master_enabled"
MASTER_WAS: str | None = None


def resolve_rule_entities(token: str, name_slug: str) -> tuple[str, str]:
    """Return (status_sensor, enabled_switch) for THIS run's rule once it is
    live past startup grace. Binds by the per-run name slug + mode + target set
    so a stale orphan from a failed cleanup cannot shadow it. Raises on timeout.
    """
    live = {"armed", "conditional", "enforcing", "cooldown", "pending"}
    deadline = time.time() + 100
    while time.time() < deadline:
        matches = [
            s["entity_id"]
            for s in sf.api(token, "GET", "/api/states")
            if (
                s.get("entity_id", "").startswith(f"sensor.{name_slug}")
                and s["entity_id"].endswith("_status")
                and s.get("attributes", {}).get("mode") == "dynamic_state"
                and (s.get("attributes", {}).get("target_entities") or []) == [TARGET]
                and s.get("state") in live
            )
        ]
        if len(matches) > 1:
            raise RuntimeError(f"multiple status sensors for this run: {matches}")
        if matches:
            status_e = matches[0]
            switch_e = status_e.replace("sensor.", "switch.").replace(
                "_status", "_enabled"
            )
            return status_e, switch_e
        time.sleep(3)
    raise RuntimeError("rule never armed past startup grace")


def main() -> int:
    token = sf.mint_token()

    # Master gates all rules — enable for the run, restore after.
    global MASTER_WAS
    MASTER_WAS = sf.get_state(token, MASTER).get("state")
    if MASTER_WAS != "on":
        sf.api(token, "POST", "/api/services/switch/turn_on", {"entity_id": MASTER})
        time.sleep(2)

    # Known start: source off → follow_desired_on False.
    sf.drive_source(token, "off")

    run_token = str(int(time.time()))
    rule_name = f"EG Stale Attr Smoke {run_token}"
    name_slug = f"eg_stale_attr_smoke_{run_token}"
    print(f"Creating dynamic_state rule '{rule_name}'…")
    entry_id = sf.create_dynamic_state_rule(
        token, rule_name, SOURCE, [TARGET], invert=False
    )
    print(f"  created entry {entry_id}")

    try:
        status_e, switch_e = resolve_rule_entities(token, name_slug)
        sf.check("rule armed past startup grace", bool(status_e), status_e)

        # Baseline: source off, desired off.
        sf.drive_source(token, "off")
        sf.check(
            "baseline: follow_desired_on False (source off)",
            sf.wait_for_attr(token, status_e, "follow_desired_on", False),
            f"{sf.get_state(token, status_e).get('attributes', {}).get('follow_desired_on')}",
        )

        # --- Scenario A: SUPPRESSED + source flip -------------------------
        # entity_guard.suppress resolves by entry_id.
        print("Scenario A: suppress rule, then flip source")
        sf.api(
            token,
            "POST",
            "/api/services/entity_guard/suppress",
            {"rule_id": entry_id, "duration_minutes": 10},
        )
        time.sleep(2)
        st = sf.get_state(token, status_e).get("state")
        sf.check("A: status == suppressed", st == "suppressed", f"status={st}")

        # Flip source ON while suppressed. Status stays suppressed (dedupe), but
        # follow_desired_on must refresh — the fix under test.
        sf.drive_source(token, "on")
        sf.check(
            "A: follow_desired_on refreshes to True while SUPPRESSED",
            sf.wait_for_attr(token, status_e, "follow_desired_on", True),
            "stale-attr bug: desired frozen while suppressed",
        )
        sf.check(
            "A: status still suppressed (no enforce)",
            sf.get_state(token, status_e).get("state") == "suppressed",
            f"status={sf.get_state(token, status_e).get('state')}",
        )

        # Unsuppress for a clean scenario B.
        sf.api(
            token,
            "POST",
            "/api/services/entity_guard/unsuppress",
            {"rule_id": entry_id},
        )
        time.sleep(2)

        # --- Scenario B: DISABLED + source flip ---------------------------
        print("Scenario B: disable rule, then flip source")
        sf.drive_source(token, "off")
        sf.wait_for_attr(token, status_e, "follow_desired_on", False)
        sf.api(token, "POST", "/api/services/switch/turn_off", {"entity_id": switch_e})
        time.sleep(2)
        st = sf.get_state(token, status_e).get("state")
        sf.check("B: status == disabled", st == "disabled", f"status={st}")

        sf.drive_source(token, "on")
        sf.check(
            "B: follow_desired_on refreshes to True while DISABLED",
            sf.wait_for_attr(token, status_e, "follow_desired_on", True),
            "stale-attr bug: desired frozen while disabled",
        )
        sf.check(
            "B: status still disabled (no enforce)",
            sf.get_state(token, status_e).get("state") == "disabled",
            f"status={sf.get_state(token, status_e).get('state')}",
        )

    finally:
        print("Cleanup: source off, delete rule, restore master")
        try:
            sf.drive_source(token, "off")
        except Exception as e:
            print(f"  [WARN] source reset failed: {e}")
        for eid in dict.fromkeys([entry_id, *sf.CREATED]):
            try:
                sf.api(token, "DELETE", f"/api/config/config_entries/entry/{eid}")
            except Exception as e:
                print(f"  [WARN] cleanup failed for {eid}: {e}")
        if MASTER_WAS and MASTER_WAS != "on":
            try:
                sf.api(
                    token,
                    "POST",
                    "/api/services/switch/turn_off",
                    {"entity_id": MASTER},
                )
            except Exception as e:
                print(f"  [WARN] master restore failed: {e}")

    if sf.FAILS:
        print(f"\nSMOKE RESULT: FAILED — {len(sf.FAILS)} check(s): {sf.FAILS}")
        return 1
    print("\nSMOKE RESULT: PASSED — status attrs stay live while inactive")
    return 0


if __name__ == "__main__":
    sys.exit(main())
