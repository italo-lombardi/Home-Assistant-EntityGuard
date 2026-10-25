#!/usr/bin/env python3
"""Entity Guard SPLIT ON/OFF DELAY smoke test — run INSIDE serene_booth.

Exercises the dynamic_state per-direction delay feature end-to-end against a live
HA: a dynamic_state rule with delay_on_seconds != delay_off_seconds must schedule
the ON-direction enforcement with the ON delay and the OFF-direction with the OFF
delay. Verified via the pending_enforcement_due countdown length each direction.

Scenario:
  source  = input_boolean (drives on/off)
  target  = a real light that mirrors the source (dynamic_state, invert off)
  delay_on  = DELAY_ON  (e.g. 20s)   delay_off = DELAY_OFF (e.g. 6s)
  1. Create rule; wait past 60s startup grace.
  2. Source OFF→ON: target should be driven ON after ~DELAY_ON. Assert the rule
     goes PENDING and the scheduled due-time is ~DELAY_ON from now (not DELAY_OFF).
  3. Cancel that pending (flip source back), then source stays compliant.
  4. Source ON→OFF with target ON: assert PENDING with due ~DELAY_OFF.
  Each direction asserts the countdown length matches the direction's delay —
  this is what proves the split, not just that *a* delay happened.
  5. Cleanup: delete rule (finally).

Reuses smoke_follow.py helpers. Exit 0 = all green, 1 = failure.
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "/workspaces/home-assistant-core/config")
import smoke_follow as sf  # shared helpers, resolved inside the container

SOURCE = "input_boolean.ea_ctrl_kitchen_1"
TARGET = "light.living_room_under_cabinet_lights"
DELAY_ON = 20
DELAY_OFF = 6
CREATED: list[str] = []
MASTER_WAS: str | None = None  # EG master switch state before the run (to restore)


def create_split_delay_rule(token, name, source, target, delay_on, delay_off):
    """Create a dynamic_state rule with per-direction delays. Returns entry_id.

    Flow: rule(mode=dynamic_state) -> dynamic_state_params (source_entity,
    follow_invert, source_value_from, delay_on_seconds, delay_off_seconds)
    -> extras -> preview -> create.
    """
    flow = sf.api(
        token, "POST", "/api/config/config_entries/flow", {"handler": "entity_guard"}
    )
    fid = flow["flow_id"]
    result = sf.api(
        token,
        "POST",
        f"/api/config/config_entries/flow/{fid}",
        {"rule_name": name, "target_entities": [target], "mode": "dynamic_state"},
    )
    while result.get("type") == "form":
        sf.guard_form(result)
        if result.get("step_id") == "dynamic_state_params":
            payload = {
                "source_entity": source,
                "follow_invert": False,
                "source_value_from": "state",
                "delay_on_seconds": delay_on,
                "delay_off_seconds": delay_off,
            }
        else:
            payload = {}
        result = sf.api(
            token, "POST", f"/api/config/config_entries/flow/{fid}", payload
        )
    if result.get("type") != "create_entry":
        raise RuntimeError(f"split-delay create flow did not finish: {result}")
    entry_id = result["result"]["entry_id"]
    CREATED.append(entry_id)
    return entry_id


def resolve_status(token, name_slug):
    """Find THIS run's status sensor by name-slug + mode + target (unique name)."""
    for s in sf.api(token, "GET", "/api/states"):
        eid = s.get("entity_id", "")
        a = s.get("attributes", {})
        if (
            eid.startswith(f"sensor.{name_slug}")
            and eid.endswith("_status")
            and a.get("mode") == "dynamic_state"
            and (a.get("target_entities") or []) == [TARGET]
        ):
            return eid
    return ""


def pending_delay_seconds(token, status_e):
    """Return the scheduled countdown length (s) for TARGET: due_time - now.
    None if not pending / no due entry."""
    s = sf.get_state(token, status_e)
    if s.get("state") != "pending":
        return None
    due = (s.get("attributes", {}).get("pending_enforcement_due") or {}).get(TARGET)
    if not due:
        return None
    # due is ISO; compute seconds from now using the server's clock via a second
    # read is overkill — parse and diff against local now (both UTC).
    from datetime import datetime

    due_dt = datetime.fromisoformat(due)
    now = datetime.now(due_dt.tzinfo)
    return (due_dt - now).total_seconds()


def wait_for_pending_delay(token, status_e, timeout=8):
    """Poll until the rule is pending with a due entry; return the countdown s."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = pending_delay_seconds(token, status_e)
        if d is not None:
            return d
        time.sleep(0.3)
    return None


def main() -> int:
    token = sf.mint_token()

    # Enable EG master (rules go master_disabled otherwise); restore in finally.
    master = "switch.entity_guard_hub_master_enabled"
    global MASTER_WAS
    MASTER_WAS = sf.get_state(token, master).get("state")
    if MASTER_WAS != "on":
        sf.api(token, "POST", "/api/services/switch/turn_on", {"entity_id": master})
        time.sleep(2)

    # Known start: source OFF, target OFF (compliant — mirror).
    sf.api(token, "POST", "/api/services/input_boolean/turn_off", {"entity_id": SOURCE})
    sf.api(token, "POST", "/api/services/light/turn_off", {"entity_id": TARGET})
    time.sleep(1)

    run_token = str(int(time.time()))
    name = f"EG Split Delay Smoke {run_token}"
    name_slug = f"eg_split_delay_smoke_{run_token}"
    print(f"Creating dynamic_state split-delay rule (on={DELAY_ON}s off={DELAY_OFF}s)…")
    entry_id = create_split_delay_rule(token, name, SOURCE, TARGET, DELAY_ON, DELAY_OFF)
    print(f"  created entry {entry_id}")

    # Wait past startup grace.
    status_e = ""
    deadline = time.time() + 100
    while time.time() < deadline and not status_e:
        e = resolve_status(token, name_slug)
        if e and sf.get_state(token, e).get("state") not in (
            "starting",
            "unknown",
            "unavailable",
            None,
        ):
            status_e = e
        else:
            time.sleep(3)
    sf.check("rule armed past startup grace", bool(status_e), f"status={status_e}")
    if not status_e:
        return 1

    # Sensor exposes both delays.
    attrs = sf.get_state(token, status_e).get("attributes", {})
    sf.check(
        "sensor exposes delay_on_seconds",
        attrs.get("delay_on_seconds") == DELAY_ON,
        f"delay_on_seconds={attrs.get('delay_on_seconds')}",
    )
    sf.check(
        "sensor exposes delay_off_seconds",
        attrs.get("delay_off_seconds") == DELAY_OFF,
        f"delay_off_seconds={attrs.get('delay_off_seconds')}",
    )

    # ON direction: source OFF→ON, target still OFF → desired ON → PENDING ~DELAY_ON.
    sf.api(token, "POST", "/api/services/input_boolean/turn_on", {"entity_id": SOURCE})
    on_delay = wait_for_pending_delay(token, status_e)
    # Accept a window: must be clearly nearer DELAY_ON than DELAY_OFF.
    on_ok = (
        on_delay is not None and abs(on_delay - DELAY_ON) < (DELAY_ON - DELAY_OFF) / 2
    )
    sf.check(
        "ON direction uses delay_on",
        on_ok,
        f"countdown={on_delay}s (expected ~{DELAY_ON}, not {DELAY_OFF})",
    )
    # Let it enforce (or cancel by reverting); wait out the ON delay so target = on.
    sf.check(
        "ON enforcement eventually drives target on",
        sf.wait_for_state(token, TARGET, "on", timeout=DELAY_ON + 8),
        f"target={sf.get_state(token, TARGET).get('state')}",
    )

    # OFF direction: source ON→OFF, target ON → desired OFF → PENDING ~DELAY_OFF.
    sf.api(token, "POST", "/api/services/input_boolean/turn_off", {"entity_id": SOURCE})
    off_delay = wait_for_pending_delay(token, status_e)
    off_ok = (
        off_delay is not None
        and abs(off_delay - DELAY_OFF) < (DELAY_ON - DELAY_OFF) / 2
    )
    sf.check(
        "OFF direction uses delay_off",
        off_ok,
        f"countdown={off_delay}s (expected ~{DELAY_OFF}, not {DELAY_ON})",
    )
    sf.check(
        "OFF enforcement eventually drives target off",
        sf.wait_for_state(token, TARGET, "off", timeout=DELAY_OFF + 8),
        f"target={sf.get_state(token, TARGET).get('state')}",
    )

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
        if MASTER_WAS is not None and MASTER_WAS != "on":
            try:
                sf.api(
                    tok,
                    "POST",
                    "/api/services/switch/turn_off",
                    {"entity_id": "switch.entity_guard_hub_master_enabled"},
                )
            except Exception as e:
                print(f"  [WARN] master restore failed: {e}")
    sys.exit(rc)
